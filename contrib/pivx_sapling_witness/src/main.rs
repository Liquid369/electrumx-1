use ff::PrimeField;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use zcash_primitives::sapling::{merkle_hash, Node, SAPLING_COMMITMENT_TREE_DEPTH_U8};

const TREE_DEPTH: usize = SAPLING_COMMITMENT_TREE_DEPTH_U8 as usize;

/// Optional on-disk cache of the full Merkle level structure. When set,
/// repeat invocations verify the cached prefix with a rolling SHA-256 chain,
/// append only new leaves, and answer current-anchor witness/root requests
/// with sibling lookups instead of recomputing ~2n Pedersen hashes per call.
const STATE_FILE_ENV: &str = "PIVX_SAPLING_WITNESS_STATE_FILE";
const STATE_MAGIC: &[u8; 8] = b"PIVXWTS1";

#[derive(Deserialize)]
struct Commitment {
    cmu: String,
    height: u32,
}

#[derive(Deserialize)]
struct Request {
    mode: Option<String>,
    commitments: Option<Vec<Commitment>>,
    commitment: Option<String>,
    position: Option<u64>,
    anchor: Option<String>,
    // Accepted for protocol compatibility; anchor heights are derived from
    // the last leaf of the selected tree so responses are stable across
    // chain-tip changes that do not alter the Sapling tree.
    #[allow(dead_code)]
    current_height: Option<u32>,
    path: Option<Vec<String>>,
}

#[derive(Serialize)]
struct Response {
    success: bool,
    error: Option<String>,
    root: Option<String>,
    anchor: Option<String>,
    anchor_height: Option<u32>,
    tree_size: Option<usize>,
    path: Option<Vec<String>>,
    position: Option<u64>,
}

fn parse_32_hex(value: &str, label: &str) -> Result<[u8; 32], String> {
    let bytes = hex::decode(value).map_err(|e| format!("{label} hex decode failed: {e}"))?;
    bytes
        .try_into()
        .map_err(|_| format!("{label} must be 32 bytes"))
}

fn parse_node(value: &str, label: &str) -> Result<Node, String> {
    let bytes = parse_32_hex(value, label)?;
    node_from_bytes(bytes, label)
}

fn node_from_bytes(bytes: [u8; 32], label: &str) -> Result<Node, String> {
    Option::<bls12_381::Scalar>::from(bls12_381::Scalar::from_repr(bytes))
        .map(Node::from_scalar)
        .ok_or_else(|| format!("{label} is not a canonical Sapling node"))
}

fn node_hex(node: &Node) -> String {
    hex::encode(node_bytes(node))
}

fn node_bytes(node: &Node) -> [u8; 32] {
    let scalar: bls12_381::Scalar = (*node).into();
    scalar.to_repr()
}

fn combine(level: u8, lhs: &Node, rhs: &Node) -> Node {
    let repr = merkle_hash(level.into(), &node_bytes(lhs), &node_bytes(rhs));
    node_from_bytes(repr, "computed parent").expect("Sapling parent hash is canonical")
}

fn empty_nodes() -> &'static [Node; TREE_DEPTH + 1] {
    static EMPTIES: OnceLock<[Node; TREE_DEPTH + 1]> = OnceLock::new();
    EMPTIES.get_or_init(|| {
        let mut nodes = [Node::from_scalar(bls12_381::Scalar::one()); TREE_DEPTH + 1];
        for level in 1..=TREE_DEPTH {
            let child = nodes[level - 1];
            nodes[level] = combine((level - 1) as u8, &child, &child);
        }
        nodes
    })
}

fn reduce_level(level_nodes: &[Node], level: usize) -> Vec<Node> {
    let empty = empty_nodes()[level];
    level_nodes
        .par_chunks(2)
        .map(|pair| {
            let left = pair[0];
            let right = pair.get(1).copied().unwrap_or(empty);
            combine(level as u8, &left, &right)
        })
        .collect()
}

fn witness_path(nodes: &[Node], position: usize) -> Result<Vec<Node>, String> {
    if position >= nodes.len() {
        return Err("position outside selected Sapling tree".to_string());
    }

    let mut level_nodes = nodes.to_vec();
    let mut index = position;
    let mut path = Vec::with_capacity(TREE_DEPTH);

    for level in 0..TREE_DEPTH {
        let sibling_index = index ^ 1;
        let sibling = level_nodes
            .get(sibling_index)
            .copied()
            .unwrap_or(empty_nodes()[level]);
        path.push(sibling);

        level_nodes = reduce_level(&level_nodes, level);
        index >>= 1;
    }

    Ok(path)
}

fn rolling_chain_hash(leaves: &[[u8; 32]]) -> [u8; 32] {
    let mut hash = [0u8; 32];
    for leaf in leaves {
        let mut hasher = Sha256::new();
        hasher.update(hash);
        hasher.update(leaf);
        hash = hasher.finalize().into();
    }
    hash
}

/// Full padded Merkle level structure for the current Sapling tree.
///
/// `levels[0]` holds the leaves; `levels[k + 1]` is `reduce_level` of
/// `levels[k]`, so `levels[TREE_DEPTH]` holds the root. Sibling lookups for
/// the current tree therefore need no hashing at all, and appending a leaf
/// only recomputes the rightmost node of each level.
struct TreeState {
    count: u64,
    chain_hash: [u8; 32],
    levels: Vec<Vec<Node>>,
}

impl TreeState {
    fn build(leaf_bytes: &[[u8; 32]]) -> Result<TreeState, String> {
        let leaves = leaf_bytes
            .iter()
            .enumerate()
            .map(|(index, bytes)| node_from_bytes(*bytes, &format!("commitment[{index}]")))
            .collect::<Result<Vec<Node>, String>>()?;
        let mut levels = Vec::with_capacity(TREE_DEPTH + 1);
        levels.push(leaves);
        for level in 0..TREE_DEPTH {
            let next = if levels[level].is_empty() {
                Vec::new()
            } else {
                reduce_level(&levels[level], level)
            };
            levels.push(next);
        }
        Ok(TreeState {
            count: leaf_bytes.len() as u64,
            chain_hash: rolling_chain_hash(leaf_bytes),
            levels,
        })
    }

    fn append(&mut self, leaf: Node, leaf_bytes: &[u8; 32]) -> Result<(), String> {
        if self.count >= 1u64 << TREE_DEPTH {
            return Err("Sapling commitment tree is full".to_string());
        }
        self.levels[0].push(leaf);
        let mut index = self.levels[0].len() - 1;
        for level in 0..TREE_DEPTH {
            let parent_index = index / 2;
            let left = self.levels[level][2 * parent_index];
            let right = self.levels[level]
                .get(2 * parent_index + 1)
                .copied()
                .unwrap_or(empty_nodes()[level]);
            let parent = combine(level as u8, &left, &right);
            if parent_index == self.levels[level + 1].len() {
                self.levels[level + 1].push(parent);
            } else {
                self.levels[level + 1][parent_index] = parent;
            }
            index = parent_index;
        }
        self.count += 1;
        let mut hasher = Sha256::new();
        hasher.update(self.chain_hash);
        hasher.update(leaf_bytes);
        self.chain_hash = hasher.finalize().into();
        Ok(())
    }

    fn root(&self) -> Node {
        if self.count == 0 {
            return empty_nodes()[TREE_DEPTH];
        }
        self.levels[TREE_DEPTH][0]
    }

    fn leaf(&self, position: usize) -> Option<Node> {
        self.levels[0].get(position).copied()
    }

    fn witness(&self, position: usize) -> Result<Vec<Node>, String> {
        if position as u64 >= self.count {
            return Err("position outside selected Sapling tree".to_string());
        }
        let mut index = position;
        let mut path = Vec::with_capacity(TREE_DEPTH);
        for level in 0..TREE_DEPTH {
            let sibling = self.levels[level]
                .get(index ^ 1)
                .copied()
                .unwrap_or(empty_nodes()[level]);
            path.push(sibling);
            index >>= 1;
        }
        Ok(path)
    }

    fn load(path: &Path) -> Option<TreeState> {
        let data = std::fs::read(path).ok()?;
        if data.len() < 8 + 8 + 32 || &data[..8] != STATE_MAGIC {
            return None;
        }
        let count = u64::from_le_bytes(data[8..16].try_into().ok()?);
        let mut chain_hash = [0u8; 32];
        chain_hash.copy_from_slice(&data[16..48]);
        let mut offset = 48;
        let mut levels = Vec::with_capacity(TREE_DEPTH + 1);
        let mut expected_len = count as usize;
        for _level in 0..=TREE_DEPTH {
            if data.len() < offset + 8 {
                return None;
            }
            let len = u64::from_le_bytes(data[offset..offset + 8].try_into().ok()?) as usize;
            offset += 8;
            if len != expected_len || data.len() < offset + len * 32 {
                return None;
            }
            let mut nodes = Vec::with_capacity(len);
            for index in 0..len {
                let mut bytes = [0u8; 32];
                bytes.copy_from_slice(&data[offset + index * 32..offset + index * 32 + 32]);
                nodes.push(node_from_bytes(bytes, "cached node").ok()?);
            }
            offset += len * 32;
            levels.push(nodes);
            expected_len = if expected_len == 0 {
                0
            } else {
                expected_len.div_ceil(2)
            };
        }
        if offset != data.len() {
            return None;
        }
        Some(TreeState {
            count,
            chain_hash,
            levels,
        })
    }

    fn save(&self, path: &Path) {
        let total_nodes: usize = self.levels.iter().map(Vec::len).sum();
        let mut buf = Vec::with_capacity(48 + (TREE_DEPTH + 1) * 8 + total_nodes * 32);
        buf.extend_from_slice(STATE_MAGIC);
        buf.extend_from_slice(&self.count.to_le_bytes());
        buf.extend_from_slice(&self.chain_hash);
        for level in &self.levels {
            buf.extend_from_slice(&(level.len() as u64).to_le_bytes());
            for node in level {
                buf.extend_from_slice(&node_bytes(node));
            }
        }
        let temp = path.with_extension(format!("tmp{}", std::process::id()));
        // Best-effort cache write: failures only cost the next call a rebuild.
        if std::fs::write(&temp, &buf).is_ok() && std::fs::rename(&temp, path).is_err() {
            let _ = std::fs::remove_file(&temp);
        }
    }
}

struct ResolvedTree {
    state: TreeState,
    heights: Vec<u32>,
    state_path: Option<PathBuf>,
    dirty: bool,
}

impl ResolvedTree {
    fn persist(&self) {
        if self.dirty {
            if let Some(path) = self.state_path.as_deref() {
                self.state.save(path);
            }
        }
    }

    fn parse_all_nodes(&self) -> Vec<Node> {
        // Leaves already passed canonical parsing during build/append.
        self.state.levels[0].clone()
    }
}

fn resolve_tree(commitments: &[Commitment]) -> Result<ResolvedTree, String> {
    let mut leaf_bytes = Vec::with_capacity(commitments.len());
    let mut heights = Vec::with_capacity(commitments.len());
    for (index, commitment) in commitments.iter().enumerate() {
        leaf_bytes.push(parse_32_hex(
            &commitment.cmu,
            &format!("commitment[{index}]"),
        )?);
        heights.push(commitment.height);
    }

    let state_path = std::env::var_os(STATE_FILE_ENV).map(PathBuf::from);
    let mut cached = None;
    if let Some(path) = state_path.as_deref() {
        if let Some(state) = TreeState::load(path) {
            let count = state.count as usize;
            if count <= leaf_bytes.len()
                && state.chain_hash == rolling_chain_hash(&leaf_bytes[..count])
            {
                cached = Some(state);
            }
        }
    }

    let mut dirty = false;
    let state = match cached {
        Some(mut state) => {
            for index in (state.count as usize)..leaf_bytes.len() {
                let node =
                    node_from_bytes(leaf_bytes[index], &format!("commitment[{index}]"))?;
                state.append(node, &leaf_bytes[index])?;
                dirty = true;
            }
            state
        }
        None => {
            dirty = true;
            TreeState::build(&leaf_bytes)?
        }
    };

    Ok(ResolvedTree {
        state,
        heights,
        state_path,
        dirty,
    })
}

/// Append-only Sapling commitment-tree frontier.
///
/// Appending a leaf costs amortized O(1) Merkle hashes and a frontier root
/// costs at most `TREE_DEPTH` hashes, so replaying every end-of-height tree
/// state to locate a historical anchor is O(leaves + boundaries * depth)
/// instead of recomputing a full O(leaves) root at every height boundary.
struct Frontier {
    parents: [Option<Node>; TREE_DEPTH],
}

impl Frontier {
    fn new() -> Self {
        Frontier {
            parents: [None; TREE_DEPTH],
        }
    }

    fn append(&mut self, leaf: Node) -> Result<(), String> {
        let mut carried = leaf;
        for level in 0..TREE_DEPTH {
            match self.parents[level].take() {
                None => {
                    self.parents[level] = Some(carried);
                    return Ok(());
                }
                Some(left) => carried = combine(level as u8, &left, &carried),
            }
        }
        Err("Sapling commitment tree is full".to_string())
    }

    fn root(&self) -> Node {
        let empties = empty_nodes();
        let mut carried: Option<Node> = None;
        for level in 0..TREE_DEPTH {
            carried = match (self.parents[level], carried) {
                (Some(left), Some(right)) => Some(combine(level as u8, &left, &right)),
                (Some(left), None) => Some(combine(level as u8, &left, &empties[level])),
                (None, Some(left)) => Some(combine(level as u8, &left, &empties[level])),
                (None, None) => None,
            };
        }
        carried.unwrap_or(empties[TREE_DEPTH])
    }
}

/// Locate a historical anchor by replaying every end-of-height tree state.
///
/// The anchor height is always the height of the last leaf included in the
/// selected tree, never the chain tip, so the same tree state reports the
/// same anchor height from every call regardless of how many later
/// non-Sapling blocks exist. Best-anchor and witness responses must agree
/// on this value for anchor-bound witness validation to pass client-side.
fn find_historical_anchor(
    nodes: &[Node],
    heights: &[u32],
    anchor: [u8; 32],
) -> Result<(usize, u32, Node), String> {
    let mut frontier = Frontier::new();
    for (index, node) in nodes.iter().enumerate() {
        frontier.append(*node)?;
        let end_of_height = heights
            .get(index + 1)
            .map_or(true, |next_height| *next_height != heights[index]);
        if !end_of_height {
            continue;
        }
        let root = frontier.root();
        if node_bytes(&root) == anchor {
            return Ok((index + 1, heights[index], root));
        }
    }

    Err("anchor not found in indexed canonical Sapling tree".to_string())
}

fn parse_target_anchor(req: &Request) -> Result<Option<[u8; 32]>, String> {
    match req.anchor.as_deref() {
        Some(anchor_hex) => Ok(Some(parse_32_hex(anchor_hex, "anchor")?)),
        None => Ok(None),
    }
}

fn witness(req: &Request) -> Result<Response, String> {
    let commitments = req
        .commitments
        .as_ref()
        .ok_or_else(|| "commitments are required".to_string())?;
    let position = req
        .position
        .ok_or_else(|| "position is required".to_string())?;
    let position_usize = usize::try_from(position).map_err(|_| "position is too large")?;
    let target = parse_target_anchor(req)?;

    let resolved = resolve_tree(commitments)?;
    if resolved.state.count == 0 {
        return Err("Sapling tree has no commitments".to_string());
    }
    resolved.persist();

    let current_root = resolved.state.root();
    if target.is_none() || target == Some(node_bytes(&current_root)) {
        // Current-anchor fast path: pure sibling lookups from the cached
        // level structure, no Pedersen hashing beyond the integrity check.
        let tree_size = resolved.state.count as usize;
        if position_usize >= tree_size {
            return Err("commitment position is after selected anchor".to_string());
        }
        let path = resolved.state.witness(position_usize)?;
        let leaf = resolved
            .state
            .leaf(position_usize)
            .ok_or_else(|| "position outside selected Sapling tree".to_string())?;
        let check_root = root_from_path(leaf, &path, position)?;
        if node_bytes(&check_root) != node_bytes(&current_root) {
            return Err("constructed witness does not reconstruct selected anchor".to_string());
        }
        return Ok(Response {
            success: true,
            error: None,
            root: Some(node_hex(&current_root)),
            anchor: Some(node_hex(&current_root)),
            anchor_height: Some(resolved.heights[tree_size - 1]),
            tree_size: Some(tree_size),
            path: Some(path.iter().map(node_hex).collect()),
            position: Some(position),
        });
    }

    // Historical anchor: replay end-of-height states to find the matching
    // tree prefix, then build the witness over that prefix.
    let nodes = resolved.parse_all_nodes();
    let (tree_size, anchor_height, root) = find_historical_anchor(
        &nodes,
        &resolved.heights,
        target.expect("target is present in the historical branch"),
    )?;
    if position_usize >= tree_size {
        return Err("commitment position is after selected anchor".to_string());
    }

    let selected_nodes = &nodes[..tree_size];
    let path = witness_path(selected_nodes, position_usize)?;
    let check_root = root_from_path(selected_nodes[position_usize], &path, position)?;
    if node_bytes(&check_root) != node_bytes(&root) {
        return Err("constructed witness does not reconstruct selected anchor".to_string());
    }

    Ok(Response {
        success: true,
        error: None,
        root: Some(node_hex(&root)),
        anchor: Some(node_hex(&root)),
        anchor_height: Some(anchor_height),
        tree_size: Some(tree_size),
        path: Some(path.iter().map(node_hex).collect()),
        position: Some(position),
    })
}

fn root(req: &Request) -> Result<Response, String> {
    let commitments = req
        .commitments
        .as_ref()
        .ok_or_else(|| "commitments are required".to_string())?;
    let target = parse_target_anchor(req)?;

    let resolved = resolve_tree(commitments)?;
    if resolved.state.count == 0 {
        return Err("Sapling tree has no commitments".to_string());
    }
    resolved.persist();

    let current_root = resolved.state.root();
    let (tree_size, anchor_height, root) =
        if target.is_none() || target == Some(node_bytes(&current_root)) {
            let tree_size = resolved.state.count as usize;
            (tree_size, resolved.heights[tree_size - 1], current_root)
        } else {
            find_historical_anchor(
                &resolved.parse_all_nodes(),
                &resolved.heights,
                target.expect("target is present in the historical branch"),
            )?
        };

    Ok(Response {
        success: true,
        error: None,
        root: Some(node_hex(&root)),
        anchor: Some(node_hex(&root)),
        anchor_height: Some(anchor_height),
        tree_size: Some(tree_size),
        path: None,
        position: None,
    })
}

fn verify(req: &Request) -> Result<Response, String> {
    let commitment = parse_node(
        req.commitment
            .as_deref()
            .ok_or_else(|| "commitment is required".to_string())?,
        "commitment",
    )?;
    let position = req
        .position
        .ok_or_else(|| "position is required".to_string())?;
    let path_values = req
        .path
        .as_ref()
        .ok_or_else(|| "path is required".to_string())?;
    let mut path = Vec::with_capacity(path_values.len());
    for (index, value) in path_values.iter().enumerate() {
        path.push(parse_node(value, &format!("path[{index}]"))?);
    }

    let root = root_from_path(commitment, &path, position)?;
    if let Some(anchor_hex) = req.anchor.as_deref() {
        let anchor = parse_32_hex(anchor_hex, "anchor")?;
        if node_bytes(&root) != anchor {
            return Err("path root does not match anchor".to_string());
        }
    }

    Ok(Response {
        success: true,
        error: None,
        root: Some(node_hex(&root)),
        anchor: Some(node_hex(&root)),
        anchor_height: None,
        tree_size: None,
        path: None,
        position: Some(position),
    })
}

fn root_from_path(leaf: Node, path: &[Node], position: u64) -> Result<Node, String> {
    if path.len() != usize::from(SAPLING_COMMITMENT_TREE_DEPTH_U8) {
        return Err("path must contain exactly 32 Sapling nodes".to_string());
    }
    let mut root = leaf;
    for (level, sibling) in path.iter().enumerate() {
        if (position >> level) & 1 == 0 {
            root = combine(level as u8, &root, sibling);
        } else {
            root = combine(level as u8, sibling, &root);
        }
    }
    Ok(root)
}

fn main() {
    let mut input = String::new();
    if let Err(e) = io::stdin().read_to_string(&mut input) {
        print_response(Err(format!("failed to read stdin: {e}")));
        return;
    }

    let request = serde_json::from_str::<Request>(&input)
        .map_err(|e| format!("invalid JSON request: {e}"));
    let result = request.and_then(|req| {
        match req.mode.as_deref().unwrap_or("witness") {
            "witness" => witness(&req),
            "root" => root(&req),
            "verify" => verify(&req),
            other => Err(format!("unsupported mode {other}")),
        }
    });
    print_response(result);
}

fn print_response(result: Result<Response, String>) {
    let response = match result {
        Ok(response) => response,
        Err(error) => Response {
            success: false,
            error: Some(error),
            root: None,
            anchor: None,
            anchor_height: None,
            tree_size: None,
            path: None,
            position: None,
        },
    };
    println!(
        "{}",
        serde_json::to_string(&response).expect("response serializes")
    );
    if !response.success {
        std::process::exit(1);
    }
}
