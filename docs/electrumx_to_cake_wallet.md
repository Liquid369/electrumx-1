# ElectrumX → Cake Wallet handoff (PIVX Sapling v1 contract)

Last updated: 2026-08-24
Server repo: `Liquid369/electrumx-1`, branch `master`
(modern spesmilo/electrumx fork; the old `PIVX-Project/electrumx`
lib/server-layout fork is deprecated, reference only).
Authoritative contract docs: `docs/pivx-sapling.rst` and
`docs/pivx-sapling-spending.rst` in the server repo — this file is the
wallet-facing summary of breaking changes and new capabilities.

## Status

Deployed and serving on both default nodes
(`electrum01.chainster.org`, `electrum02.chainster.org`), fully synced.
The server passed a 3-round adversarial audit plus per-feature Codex
audits; 117+ Sapling tests.  Activation heights confirmed from PIVX
Core v5.6.1: mainnet `2700500`, testnet `201`.

## Contract summary (what to feature-detect)

Probe `blockchain.sapling.capabilities`.  Current advertisements:

- `contract: "pivx.sapling.electrumx.v1"`, `version: 1`,
  `release_contract_ready: true` when everything below holds.
- `hex_byte_order: "display"` — every 32-byte value in params and
  responses (cmu, nullifier, anchor/root, cv, rk, epk, txids, block
  hashes) uses PIVX Core display order (`uint256::GetHex`).  ONLY
  exception: witness `path` elements are raw Sapling node encodings.
  Ciphertexts are natural-order byte vectors, never reversed.
  Client rule: 32 bytes of hex → reverse before crypto
  (`from_bytes(reverse(hex_decode(x)))`); anything else as-is.
- `supports_active_height_index: true` + `active_heights_max_limit`.
- `features`:
  - `global_output_positions`, `block_hashes`, `structured_errors` —
    v1 baseline.
  - `canonical_witnesses` — true only when the operator deployed the
    Rust witness helper; gate shielded sends on it.
  - `consistent_db_height` — db_height is a committed watermark (see
    below).
  - `supports_mempool`, `supports_mempool_subscribe` — 0-conf feed.

## Sync flow (recommended)

1. `blockchain.sapling.get_active_heights(start, end, limit)` — the
   heights in range with ≥1 Sapling tx (exactly the blocks
   `get_block_range` reports non-empty).  Clamps to the indexed tip:
   above-tip requests return `heights: [], complete: true` — not an
   error — except while the server trails its daemon past tolerance
   (lag > 2), when they return retryable `index_not_ready`.  Truncation: first `limit` heights, `complete: false`,
   `end` = last covered height; resume at `end + 1`.  Cuts a fresh
   restore from ~28,500 range calls to a handful.
2. `blockchain.sapling.get_block_range(a, b)` (≤ 100 blocks) only for
   the active heights.  Envelope semantics unchanged; every output
   carries its explicit global position; `block_hashes` covers every
   scanned height for reorg detection.
3. Confirmations from `index_status.daemon_height`; shield-scan
   ceiling from `db_height`.

## db_height is a committed watermark (`consistent_db_height`)

For any `X <= db_height`, every Sapling read path already serves X's
final committed data — no "empty now, populated later" window.  The
wallet may scan exactly up to `db_height` and advance its cursor with
no safety margin (drop the stay-behind workaround when the flag is
present).  Mid-request reorgs on the two feeds without block hashes
(`get_active_heights`, `get_outputs`) fail with retryable
`index_incomplete` instead of silently omitting data; keep the
standard reorg rescan policy (`REORG_LIMIT = 100`, compare persisted
per-height hashes against `block_hashes`).

## 0-conf shielded visibility (mempool)

- `blockchain.sapling.get_mempool` (poll): `{txs: [{txid, first_seen,
  outputs, spends}], truncated}` — outputs/spends byte-identical to the
  same tx's later `get_block_range` appearance (dedup by txid + cmu).
  Deliberately NO position/index fields: render decrypted notes
  "incoming, not spendable" until mined.  Caps 1000 txs / 10000
  outputs, `truncated: true` when hit.  `mempool_not_ready` error
  envelope (retryable) when no snapshot is available — distinct from a
  healthy empty mempool (no `error` key).
- `blockchain.sapling.mempool.subscribe` (push): returns the current
  snapshot, then pushes the same envelope as a notification whenever
  the Sapling mempool set changes (or availability flips).  Full state
  replacement — apply the latest, nothing to diff.
  `blockchain.sapling.mempool.unsubscribe` to stop.  Spends (nullifiers) are included, so
  own outgoing sends can show pending instantly.

## Breaking changes vs pre-audit servers (unchanged from July)

1. Hex byte order is display everywhere (feature-detect, see above).
2. `blockchain.sapling.get_nullifiers` is removed → structured
   `unsupported_method`; derive spends from `get_block_range` `spends`
   arrays; `get_nullifier_status` / `check_nullifiers` (batch ≤ 10000)
   remain.
3. Ranges above the indexed tip are rejected (`index_incomplete` with
   `error.indexed_height`), not empty — except `get_active_heights`,
   which clamps (see above).
4. `get_tree_state` serves from the index: `anchor`/`root`/
   `latest_anchor` (display), `tree_size`, `commitment_count`,
   `anchor_first_height`, `block_hash`, `indexed_height`,
   `sapling_activation_height`; `nullifier_count` no longer exists.
5. Request order is free: no method requires `server.version` first
   (un-negotiated sessions run at the server's minimum protocol and
   higher-protocol-only methods appear only after negotiation, so
   still negotiate early as normal).  `server.version` is idempotent —
   repeat it as a liveness check any number of times; it always
   returns the negotiated result and never errors or drops the
   session.  JSON-RPC ids are echoed verbatim (string in → string
   out); drop any client-side id coercion.  `consistent_db_height` is
   also mirrored inside `index_status` for convenience.

## Witnesses (unchanged from July)

Canonical Sapling witnesses (real `zcash_primitives` Pedersen tree,
depth 32) validated fail-closed against consensus header-root anchors;
`get_witness`/`get_witnesses` (batch ≤ 100); response includes
`anchor`/`root`, `anchor_height`, `tree_size`,
`position`/`global_position`, `cmu`, `txid`, `output_index`, 32 raw
`path` nodes (`path_order: 'leaf_to_root'`).  Gate on
`features.canonical_witnesses`; without the helper binary witness
calls return `witness_backend_unavailable` and `get_best_anchor`
returns `canonical_anchor_unavailable`.  Building the commitment tree
locally from `get_block_range` positions remains the preferred primary
path.  Sapling RPCs carry a ~8s server timeout surfaced as retryable
`backend_timeout`.

## Retryable error types (never advance sync state past them)

`index_incomplete`, `index_not_ready`, `backend_timeout`,
`mempool_not_ready`.

## Wallet-side action list (delta since July handoff)

1. Adopt `get_active_heights` for restore/catch-up scanning, gated on
   `supports_active_height_index`.
2. Gate the removal of the scan-behind-tip safety margin on
   `features.consistent_db_height`.
3. Add the mempool poll (and optionally subscribe) path, gated on
   `supports_mempool` / `supports_mempool_subscribe`, for 0-conf
   receive/send display; dedup mempool vs confirmed by txid + cmu.
4. Treat `mempool_not_ready` as retryable alongside the existing
   retryable set.
