use ff::PrimeField;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::io::{self, Read};
use std::sync::OnceLock;
use zcash_primitives::sapling::{merkle_hash, Node, SAPLING_COMMITMENT_TREE_DEPTH_U8};

const TREE_DEPTH: usize = SAPLING_COMMITMENT_TREE_DEPTH_U8 as usize;

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

fn reduce_level(level_nodes: Vec<Node>, level: usize) -> Vec<Node> {
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

fn root_from_nodes(nodes: &[Node]) -> Node {
    if nodes.is_empty() {
        return empty_nodes()[TREE_DEPTH];
    }
    let mut level_nodes = nodes.to_vec();
    for level in 0..TREE_DEPTH {
        level_nodes = reduce_level(level_nodes, level);
    }
    level_nodes[0]
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

        level_nodes = reduce_level(level_nodes, level);
        index >>= 1;
    }

    Ok(path)
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

fn find_anchor(
    nodes: &[Node],
    heights: &[u32],
    target_anchor: Option<&str>,
) -> Result<(usize, u32, Node), String> {
    if nodes.is_empty() {
        return Err("Sapling tree has no commitments".to_string());
    }

    // The anchor height is always the height of the last leaf included in the
    // selected tree, never the chain tip, so the same tree state reports the
    // same anchor height from every call regardless of how many later
    // non-Sapling blocks exist. Best-anchor and witness responses must agree
    // on this value for anchor-bound witness validation to pass client-side.
    if let Some(anchor_hex) = target_anchor {
        let anchor = parse_32_hex(anchor_hex, "anchor")?;
        let current_root = root_from_nodes(nodes);
        if node_bytes(&current_root) == anchor {
            return Ok((nodes.len(), heights[nodes.len() - 1], current_root));
        }

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
    } else {
        let root = root_from_nodes(nodes);
        Ok((nodes.len(), heights[nodes.len() - 1], root))
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

    let mut nodes = Vec::with_capacity(commitments.len());
    let mut heights = Vec::with_capacity(commitments.len());
    for (index, commitment) in commitments.iter().enumerate() {
        nodes.push(parse_node(&commitment.cmu, &format!("commitment[{index}]"))?);
        heights.push(commitment.height);
    }

    let (tree_size, anchor_height, root) = find_anchor(
        &nodes,
        &heights,
        req.anchor.as_deref(),
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

    let mut nodes = Vec::with_capacity(commitments.len());
    let mut heights = Vec::with_capacity(commitments.len());
    for (index, commitment) in commitments.iter().enumerate() {
        nodes.push(parse_node(&commitment.cmu, &format!("commitment[{index}]"))?);
        heights.push(commitment.height);
    }

    let (tree_size, anchor_height, root) = find_anchor(
        &nodes,
        &heights,
        req.anchor.as_deref(),
    )?;

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
