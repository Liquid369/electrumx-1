# ElectrumX → Cake Wallet handoff (PIVX Sapling v1 contract)

Date: 2026-07-11
Server repo: `Liquid369/electrumx-1`, branch `master`, commit `8eca3fc`
(modern spesmilo/electrumx fork; the old `PIVX-Project/electrumx`
lib/server-layout fork is deprecated, reference only).
Authoritative contract docs: `docs/pivx-sapling.rst` and
`docs/pivx-sapling-spending.rst` in the server repo (rewritten and
code-verified on this date after a three-round audit plus a live
mainnet resync fix).

## Status

The server passed a 3-round adversarial audit (two independent
reviewers per round). A live mainnet resync exposed one further
consensus bug (PIVX serializes SaplingTxData's 64-byte `bindingSig`
unconditionally, unlike Zcash) — fixed in `8eca3fc` with the real
mainnet tx `2d356c83…ff4369` as a regression fixture. The reference
node is resyncing from genesis; wallet integration testing can start
once it reaches the daemon tip. Activation heights confirmed from PIVX
Core v5.6.1: mainnet `2700500`, testnet `201`.

## Breaking changes the wallet MUST adapt to

1. **Hex byte order is now `display` everywhere.** Every 32-byte
   uint256-like value in params AND responses — `cmu`, `nullifier`,
   `anchor`/`root`, `cv`, `rk`, `epk`/`ephemeral_key`, txids, block
   hashes — uses PIVX Core RPC display order (`uint256::GetHex`).
   Earlier server builds emitted raw serialization order for several of
   these. Feature-detect via `capabilities.hex_byte_order == 'display'`.
   The ONLY exception: witness `path` elements are raw Sapling node
   encodings (`witness_path_encoding: 'sapling_node_to_bytes_hex'`),
   fed directly to the prover.
   Ciphertexts (`enc_ciphertext`, `out_ciphertext`) are natural-order
   byte vectors, unchanged.

2. **`blockchain.sapling.get_nullifiers` is removed.** Calls return a
   structured `unsupported_method` envelope. Derive spend information
   from the `spends` arrays in `get_block_range` (each entry:
   `nullifier`, `cv`, `anchor`, `rk`, `spend_index`, display order).
   Single/batch status lookups remain: `get_nullifier_status` /
   `check_nullifiers` (batch limit 10000).

3. **Ranges above the server's indexed tip are rejected, not empty.**
   `get_block_range` returns `success:false, complete:false` with
   `error.type == 'index_incomplete'` and `error.indexed_height`.
   Treat as retryable (matches the wallet's existing "failed ranges are
   hard failures" rule). Max range is 100 blocks. `block_hashes` still
   covers every scanned height for reorg detection. Removed error
   types: `missing_block_hash`, `partial_index`, `pruned_range`;
   current list is in `capabilities.range_error_types`.

4. **`get_tree_state` changed shape.** Served entirely from the
   server's index (no daemon calls, no scans). Success fields:
   `anchor`/`root`/`latest_anchor` (display hex of the consensus
   `finalsaplingroot` from the block header), `tree_size`,
   `commitment_count` (== tree_size), `anchor_first_height`,
   `block_hash`, `indexed_height`, `sapling_activation_height`.
   `nullifier_count` no longer exists. Structured errors:
   `index_incomplete` (above tip), `invalid_range` (below activation),
   `index_error`.

5. **Version negotiation is enforced again.** The wallet may send
   Sapling-prefixed methods (`blockchain.sapling.*`, `sapling.*`,
   `blockchain.nullifier/commitment/anchor.*`) plus `server.features`,
   `server.ping`, `server.banner`, `get_capabilities`,
   `get_block_range` before `server.version`. Any other method before
   `server.version` disconnects the session. Send `server.version`
   early as normal Electrum clients do.

## Witnesses: now real, consensus-validated, fail-closed

- `get_witness` (and `get_witnesses`, batch ≤ 100) returns canonical
  Sapling witnesses computed by a Rust helper using real
  `zcash_primitives` Pedersen hashing (depth 32).
- Every witness/best-anchor response is validated server-side against
  consensus anchors indexed from block headers (root existence AND
  tree-size match) and **fails closed** — the wallet will never receive
  an internally-consistent-but-unspendable witness silently.
- Requested anchors (display hex) must be consensus
  `finalsaplingroot` values of the indexed chain; junk anchors are
  rejected before any computation.
- Response fields: `anchor`/`root` (display), `anchor_height`,
  `tree_size`, `position`/`global_position`, `cmu` (display), `txid`,
  `output_index`, `path` (32 raw nodes, `path_order: 'leaf_to_root'`).
  `position` may be passed as an int or a display-hex cmu.
- `capabilities.canonical_witnesses` is `true` only when the server
  operator deployed the helper binary; without it, witness calls
  return structured `witness_backend_unavailable` and
  `get_best_anchor` returns `canonical_anchor_unavailable`. Gate
  shielded sends on this flag (it replaces the older
  `anchor_bound_witnesses` concept).
- Performance note: witness calls are cost-accounted proportionally to
  tree size and concurrency-limited server-side. The wallet's existing
  design (build the commitment tree locally from `get_block_range`
  global positions, verify against `get_tree_state` anchors) remains
  the preferred primary path; server witnesses are a
  verification/fallback source. Sapling RPCs carry a server-side ~8s
  timeout surfaced as `backend_timeout` structured errors — retryable.

## Unchanged / confirmed

- Contract id `pivx.sapling.electrumx.v1`; probe
  `blockchain.sapling.capabilities` (aliases unchanged).
- `get_block_range` envelope semantics: `success`/`complete`/`empty`
  distinction, structured errors, ascending heights, canonical
  block/tx/vShieldOutput ordering, explicit global output positions on
  every output (now guaranteed — the server errors rather than serving
  an output without its position).
- `check_nullifiers` envelope, `get_commitment_info`,
  `get_anchor_height`, `blockchain.transaction.get_sapling`.
- `get_outputs` (trial-decryption feed) now advertised in `methods`;
  range ≤ 100 blocks, ≤ indexed tip, output `limit` ≤ 10000.
- Reorg policy: `REORG_LIMIT = 100`; rescan `max(activation,
  last_scanned - 99)` comparing persisted per-height hashes against
  `block_hashes` — unchanged.
- Responses are always served from the server's indexed chain (never a
  diverging daemon tip), so `block_hashes` are self-consistent with
  positions and anchors within a response.

## Wallet-side action list

1. Normalize all 32-byte hex handling to display order, gated on
   `hex_byte_order`.
2. Replace any `get_nullifiers` usage with `get_block_range` spends.
3. Update `SaplingRpcCapabilities` parsing: `canonical_witnesses`,
   `consensus_anchors`, `hex_byte_order`, `index_status`,
   `release_contract_ready`, revised `range_error_types`; drop
   expectations of removed fields.
4. Treat `index_incomplete`/`index_not_ready`/`backend_timeout` as
   retryable; never advance `lastSyncedHeight` past them.
5. Adapt `get_tree_state` field names; use `tree_size` +
   `anchor` to bound/verify the local commitment tree.
6. Ensure `server.version` is sent early; only the whitelisted probes
   may precede it.
7. Re-run the wallet's manual node test suite against the reference
   node once synced, then update node-list readiness classification.
