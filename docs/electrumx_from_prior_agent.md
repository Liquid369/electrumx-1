> **SUPERSEDED (2026-08-24).** Historical status report from the initial
> integration pass, kept for context only.  Several statements no longer
> match the shipped v1 contract (witnesses remain bound to the
> requested/current anchor but are now canonical Pedersen-tree paths
> validated fail-closed against consensus header-root anchors,
> superseding the non-canonical design described here; the witness
> response carries
> `cmu`/`tree_size`/`txid`/`output_index`; the test count is 117+).
> The authoritative contract is `docs/pivx-sapling.rst` and
> `docs/pivx-sapling-spending.rst`; the current wallet handoff is
> `docs/electrumx_to_cake_wallet.md`.

 # PIVX Sapling ElectrumX Support Status

## Summary

Implemented production-oriented PIVX Sapling ElectrumX support for Cake Wallet, including Sapling parsing, indexing, RPC contract discovery, block-range scanning, nullifier/commitment lookup, anchor/tree/witness APIs, and reorg-safe rollback behavior.

## Core Contract

Primary contract identifier:

`pivx.sapling.electrumx.v1`

Capability probe:

`blockchain.sapling.capabilities`

The capability response advertises:
- supported methods and aliases
- Sapling activation height
- max block range
- range response format
- structured error types

## Block Range API

Method:

`blockchain.sapling.get_block_range`

Response is an envelope, not a bare list:

- `success`
- `complete`
- `empty`
- `start_height`
- `end_height`
- `height_count`
- `block_count`
- `sapling_tx_count`
- `block_hashes`
- `blocks`
- `error`

Important behavior:
- Empty successful ranges return `success: true`, `complete: true`, `empty: true`.
- Failed/partial ranges return `success: false`, `complete: false`, structured `error`.
- A failed range can never look complete.
- `block_hashes` includes every scanned height, even blocks with no Sapling transactions, so clients can detect stale scanned state.

## Sapling Indexing

Implemented canonical global Sapling output positions.

Positions are assigned in:
1. block height order
2. PIVX Core transaction order within block
3. vShieldOutput order within transaction

Empty blocks do not consume positions.

Persisted indexes include:
- nullifier to spending tx
- commitment to creating tx/output/position
- global position to commitment
- anchor to height
- indexed root to tree size/height

## Witness API

Method:

`blockchain.sapling.get_witness`

Returns anchor-bound witness data:

- `anchor`
- `root`
- `anchor_height`
- `position`
- `path`
- `commitment`

Witness paths verify against the requested indexed anchor/root.

## Reorg Handling

PIVX ElectrumX keeps:

`Pivx.REORG_LIMIT = 100`

Cake Wallet should rescan the last 100 inclusive heights unless policy changes.

Sapling rollback removes reverted:
- nullifiers
- commitments
- global positions
- anchors
- indexed roots

Rollback rewinds `sapling_output_count` to the lowest removed position, so new-branch outputs reuse reverted positions while earlier outputs stay stable.

## Client Reorg Detection

Cake Wallet should persist block hashes per scanned height.

On resume:

`start = max(SAPLING_START_HEIGHT, last_scanned_height - 99)`

Then compare local hashes against `get_block_range(...).block_hashes`.

Any mismatch means local Sapling scanned state is stale and should be rewound to the last matching height.

## Activation Heights

Confirmed against PIVX Core v5.6.1:

- Mainnet Sapling activation: `2700500`
- Testnet Sapling activation: `201`

Source:
- PIVX Core release tag `v5.6.1`
- release commit `af60f19`
- `src/chainparams.cpp`

## Tests Added / Covered

Tests cover:
- Sapling parser structure and real block parsing
- rollback policy and activation heights
- reorg removing Sapling outputs/spends/anchors/roots/positions
- nullifier respent on different branch
- full rollback-boundary rescan
- returned block hashes for stale-state detection
- range rejection beyond max block range
- empty successful ranges
- daemon failure
- unsupported daemon method
- invalid range
- partial/index-incomplete responses
- stable positions across restart
- empty blocks not consuming positions
- canonical block response ordering
- witness path verification against requested anchor

## Verification

Passing:

`PYTHONPATH=. pytest tests/lib/test_pivx_sapling.py tests/lib/test_pivx_sapling_real.py tests/server/test_pivx_sapling_reorg.py -q`

Result:

`62 passed`

## Cake Wallet Follow-Up

2026-05-28 wallet-side follow-up applied:

- Cake Wallet now probes `blockchain.sapling.capabilities` before the older `blockchain.sapling.get_capabilities` fallback.
- The wallet stores advertised capability/version/network/activation metadata on PIVX node records and displays cached Sapling readiness/version status in the node list.
- Dart/Rust testnet Sapling activation constants now use `201`, matching this report.

This remains incoming evidence, not a release gate closure, until Cake Wallet is manually tested against the reported v1 server/default nodes and the release owner records independent PIVX Core source evidence.
