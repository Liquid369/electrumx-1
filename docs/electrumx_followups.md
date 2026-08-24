# PIVX ElectrumX Follow-Ups

Status: Server-side items CLOSED (2026-08-24); wallet-side verification tracking remains with the wallet team.

Date: 2026-05-27

Last updated: 2026-06-02

Scope: server-side work needed in the PIVX Sapling ElectrumX repository. Keep these tasks separate from Cake Wallet mobile-client changes. Do not use this document as APK/TestFlight readiness evidence until the matching wallet gates and manual tests pass.

## Wallet-Side Assumptions From Current Pass

- Update from `15_electrumx_update_from_other_agent.md`: another agent reports that the server-side v1 support has been implemented under contract id `pivx.sapling.electrumx.v1`, with primary capability method `blockchain.sapling.capabilities`, v1 block-range envelopes, global output positions, anchor-bound witnesses, block hashes, rollback behavior, and 62 passing server tests. Cake Wallet now accepts `blockchain.sapling.capabilities` and stores/display capability metadata, but this document remains Open until those server claims are verified against the wallet and default nodes.
- Cake Wallet's client-facing Sapling docs were aligned on 2026-06-01 to that reported v1 contract. Legacy method aliases are compatibility-only and are not enough to close default-node readiness without the v1 capability/envelope/global-position/anchor-bound witness behavior.
- Cake Wallet must treat Sapling range RPC failures as hard sync failures. Failed ranges must not be converted to empty ranges, must not advance `lastSyncedHeight`, and must be retried or surfaced to the user.
- Cake Wallet now persists a `nextTreePosition` cursor locally, supports explicit output positions when returned by `get_block_range`, and refuses to start post-activation/birthday Sapling sync without either a trusted persisted cursor or server-advertised global output positions. Legacy inferred cursors from owned notes are not trusted.
- Cake Wallet now supports a proposed v1 `get_block_range` envelope with `complete: true` and rejects incomplete envelopes or mismatched range metadata.
- Cake Wallet probes `blockchain.sapling.capabilities` first, then `blockchain.sapling.get_capabilities` when available, and validates advertised network and Sapling activation height. Legacy servers are treated as block-range-only unless they implement the v1 capability method.
- Cake Wallet automatic node switching now requires the explicit `pivx.sapling.electrumx.v1` release contract predicate before switching to a PIVX candidate node.
- Cake Wallet manual PIVX node testing now fails a reachable Electrum node unless it advertises the v1 release contract with block ranges, canonical global positions, best anchor, anchor-bound witnesses, block hashes, structured errors, and the required v1 methods. PIVX node records now persist capability/version metadata and the node list distinguishes `Sapling v1 ready` from `Sapling legacy only` after probing.
- Cake Wallet dashboard and Connection/Sync status now show shielded sync progress, last shielded scanned height, Sapling RPC readiness, and sanitized shielded sync errors. Durable per-node capability/version badges still require the server to return stable v1 capability metadata.
- Cake Wallet blocks unsafe unsupported routes locally, and shielded sends now require node anchor/witness capability before transaction construction. The server contract still needs to support route validation, witness/anchor retrieval, and nullifier status for enabled routes before external testing.
- Cake Wallet z-to-z send-max, fee-aware note selection, and shielded history rows now have wallet-side mitigations, but server-side v1 witness/anchor/nullifier/history evidence remains required before any external APK/TestFlight.
- Mainnet is the first external-test policy unless product direction changes. Testnet support must remain wired. The ElectrumX agent reports PIVX Core v5.6.1 activation heights of mainnet `2700500` and testnet `201`, and the wallet constants now use those values; keep the release gate open until the release owner records independent Core-source evidence and device tests.

## Current Server-Side Release Blockers

These items are still open as of 2026-06-01 and block closing the matching mobile release gates:

| Server-side area | Blocks mobile findings | Required proof |
| --- | --- | --- |
| Capability/version probe | `PIVX-NET-002`, `PIVX-NET-003` | `blockchain.sapling.capabilities` (or release-equivalent alias) returns network, activation height, supported methods, response limits, and version metadata. |
| Complete range envelope | `PIVX-REC-002` | Empty successful ranges are distinguishable from daemon/index/method failures, and failures produce structured errors. |
| Canonical global output positions | `PIVX-REC-001`, `PIVX-SEND-001` | Every returned Sapling output includes its global commitment tree position in PIVX Core canonical order. |
| Anchor-bound witnesses | `PIVX-SEND-001`, `PIVX-SEND-006` | Witness responses include anchor/root and height and can be verified against the selected transaction anchor. |
| Reorg/hash data | `PIVX-REC-005`, `PIVX-BAL-009` | Returned blocks include hashes and enough rollback behavior to support at least the chosen reorg depth. |
| Canonical network constants | `PIVX-NET-001`, `PIVX-NET-002` | Mainnet/testnet activation heights are confirmed from the selected PIVX Core release commit and match wallet constants. |

Until these are implemented and manually verified against the wallet, mobile-side `Needs Verification` findings tied to Sapling sync/send correctness must stay open.

Server update to verify, 2026-05-28: `15_electrumx_update_from_other_agent.md` reports the capability probe as `blockchain.sapling.capabilities`, not `blockchain.sapling.get_capabilities`, and reports all rows in this table implemented in the ElectrumX fork. Treat that as incoming evidence, not closure, until Cake Wallet is tested against that server and default-node deployments expose the same v1 metadata.

Default-node probe, 2026-05-31: the currently shipped Chainster nodes in `assets/pivx_electrum_server_list.yml` are reachable (`electrum02.chainster.org` reports `ElectrumX 1.19.0`; `electrum01.chainster.org` reports `ElectrumX 1.16.0`). `electrum02` has partial legacy Sapling RPC support: `blockchain.sapling.get_block_range` returns a bare list with block hash/time and Sapling tx output fields, and `blockchain.sapling.get_witness` is registered enough to return `commitment not found` for a dummy commitment. It does not yet expose `blockchain.sapling.capabilities`, `blockchain.sapling.get_capabilities`, v1 envelopes, global output positions, best-anchor, nullifier-status, or commitment-info methods. `electrum01` still returns `unknown method` for sampled Sapling methods. This means the full server-side v1 implementation reported in `15_electrumx_update_from_other_agent.md` is not yet deployed on the bundled defaults, or the bundled defaults are not fully upgraded v1 nodes. Keep this document open until default-node deployments expose the v1 metadata and Cake Wallet is tested against them.

Client documentation alignment, 2026-06-01: `cw_pivx/SAPLING.md` and the `PIVXSaplingElectrumX` header now document the reported `pivx.sapling.electrumx.v1` contract, including `blockchain.sapling.capabilities`, `get_block_range` v1 envelopes, `block_hashes`, canonical global output positions, anchor-bound witness metadata, and structured failure semantics. This is a documentation/client-alignment update only; it is not release evidence until the wallet is exercised against deployed v1/default nodes.

Wallet-side v1 readiness update, 2026-06-02: Cake Wallet now classifies a PIVX node as release-ready only when `SaplingRpcCapabilities.supportsV1ReleaseContract` passes for the reported `pivx.sapling.electrumx.v1` contract. Legacy block-range fallback remains compatibility-only and is not enough for node-list readiness, manual node testing, or automatic switching. Focused local wallet tests cover complete v1 metadata, incomplete advertised v1 metadata, legacy fallback, and null malformed capability responses, but this remains wallet-side evidence only until deployed v1/default nodes are tested.

## Required ElectrumX Work

- Define a versioned PIVX Sapling RPC v1 capability probe, currently reported as `blockchain.sapling.capabilities`, returning server version, PIVX Core version, network, Sapling activation height, supported methods, max range size, response ordering guarantee, block-hash support, structured-error support, and whether explicit global output positions are included.
- Extend `blockchain.sapling.get_block_range` so each successful response is unambiguously complete. The reported v1 envelope includes success/completion/empty state, requested start/end range metadata, height/block/Sapling transaction counts, `block_hashes`, blocks, and structured errors for failed or partial ranges. Empty success must be distinguishable from method failure, and returned range metadata must exactly match the requested range.
- Add explicit global Sapling output positions for every returned output. The server should index and return positions in canonical PIVX Core block transaction order and Sapling output order.
- Include block hash and timestamp for every returned block, and define behavior for reorgs and orphaned hashes.
- Implement or alias the final method names used by Cake Wallet: nullifier status, commitment info, block range, best anchor, anchor height, tree state, and witness.
- Make witness responses anchor-bound. Include anchor/root, anchor height, position, path, and commitment; clients must be able to verify the witness reconstructs exactly the requested anchor.
- Return structured RPC errors for unsupported methods, daemon failures, invalid ranges, pruned data, and partial/index-incomplete responses. Do not return `null` for these cases.
- Add server tests for empty successful ranges, failed daemon ranges, response ordering, explicit output positions, nullifier status, anchor/witness consistency, and reorg rollback behavior.

## Prompt For ElectrumX Agent: RPC Contract

Use this prompt in the PIVX Sapling ElectrumX repository:

```text
Audit and implement the production PIVX Sapling ElectrumX v1 RPC contract for Cake Wallet. Start with capability discovery and `get_block_range` correctness. The server must distinguish empty successful ranges from failures, must return structured errors for daemon/index/method failures, and must never let a failed range look complete. Add or document method aliases for Cake Wallet's client needs: capability probe, block range, nullifier status, commitment info, best anchor, anchor height, tree state, and witness. Include tests for success-empty, daemon failure, unsupported method, invalid range, and partial/index-incomplete responses.
```

## Prompt For ElectrumX Agent: Global Positions And Witnesses

Use this prompt in the PIVX Sapling ElectrumX repository:

```text
Implement canonical global Sapling output position indexing for PIVX ElectrumX. `get_block_range` must return every Sapling output in PIVX Core canonical block transaction/output order with an explicit global position. Add witness responses that are anchor-bound and include anchor/root, anchor height, note position, path, and commitment. Add tests proving positions remain stable across restart, empty blocks do not consume positions, block response ordering is canonical, and witness paths verify against the requested anchor.
```

## Prompt For ElectrumX Agent: Reorg And Policy

Use this prompt in the PIVX Sapling ElectrumX repository:

```text
Define and test PIVX Sapling reorg handling for the ElectrumX index used by Cake Wallet. Support rollback to at least 100 blocks unless product policy changes. Ensure returned block hashes let clients detect stale scanned state. Confirm and document mainnet/testnet Sapling activation heights from the canonical PIVX Core 5.6.1 source or chosen release commit. Add tests covering a reorg that removes Sapling outputs, a reorg that spends a nullifier on a different branch, and client rescan from the rollback boundary.
```


## Server-Side Closure, 2026-08-24

Every row of the "Current Server-Side Release Blockers" table is
implemented, audited (three adversarial review rounds plus per-feature
Codex audits), and deployed on both default nodes
(`electrum01.chainster.org` = 167.86.127.25 and
`electrum02.chainster.org` = 23.239.31.148, both fully synced and
serving the v1 contract):

- Capability/version probe: `blockchain.sapling.capabilities` with
  `release_contract_ready`, network, activation heights, limits,
  methods/aliases.
- Complete range envelope: v1 envelope with `success`/`complete`/
  `empty`, structured errors, exact range metadata.
- Canonical global output positions: explicit on every output,
  restart-stable, advance-time assigned.
- Witnesses: canonical Pedersen-tree paths (Rust helper) validated
  fail-closed against consensus header-root anchors — witnesses remain
  anchor-bound (requested/current anchor), but the earlier
  non-canonical design is superseded; gate on
  `features.canonical_witnesses`.
- Reorg/hash data: `block_hashes` every scanned height,
  `REORG_LIMIT = 100`, reorg-counter guards on the hash-less feeds.
- Canonical network constants: mainnet `2700500` / testnet `201`
  (PIVX Core v5.6.1).

Since this log was last updated the contract also gained:
`get_active_heights` (sparse restore scans),
`features.consistent_db_height` (committed watermark — scan exactly to
`db_height`), `get_mempool` + `mempool.subscribe`/`unsubscribe`
(0-conf shielded visibility), and display byte order everywhere
(`hex_byte_order: "display"`).  `blockchain.sapling.get_nullifiers`
was removed.  Authoritative docs: `docs/pivx-sapling.rst`,
`docs/pivx-sapling-spending.rst`, `docs/electrumx_to_cake_wallet.md`.
