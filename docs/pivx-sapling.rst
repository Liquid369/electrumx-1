PIVX Sapling Support
====================

ElectrumX provides full support for PIVX Sapling shielded transactions, enabling
light wallets like Cake Wallet to sync and manage shielded balances without
running a full node.

Overview
--------

PIVX activated Sapling (privacy protocol) at:

* **Mainnet**: Block 2,700,500
* **Testnet**: Block 201

These heights are pinned to PIVX Core ``v5.6.1`` tag ``af60f19`` in
``src/chainparams.cpp``.  Sapling activation corresponds to
``Consensus::UPGRADE_V5_0``:

* mainnet ``consensus.vUpgrades[Consensus::UPGRADE_V5_0].nActivationHeight = 2700500``
* testnet ``consensus.vUpgrades[Consensus::UPGRADE_V5_0].nActivationHeight = 201``

The implementation indexes all Sapling outputs (commitments, with canonical
global tree positions), spends (nullifiers), and the consensus Sapling
anchors (``finalsaplingroot``) carried in PIVX v8+ block headers.  Witness
computation is delegated to an external canonical helper binary and every
witness is validated against those consensus anchors (see
``blockchain.sapling.get_witness``).

Transaction Parsing
~~~~~~~~~~~~~~~~~~~

``DeserializerPIVX`` follows PIVX Core's serialization exactly:

* Sapling-version transactions (``nVersion >= 3``) carry an
  ``Optional<SaplingTxData>``: a one-byte presence flag followed, when
  non-zero, by ``valueBalance``, the shielded spend vector, and the
  shielded output vector.  PIVX has no ``nExpiryHeight`` field.
* ``bindingSig`` (64 bytes) is serialized **unconditionally** inside
  ``SaplingTxData`` (unlike Zcash, which serializes it only for
  non-empty shielded vectors): a transparent v3 transaction with empty
  spend/output vectors still carries a 64-byte all-zero signature.
  Assuming the Zcash rule computes wrong txids from the first v3
  transparent tx (mainnet regression fixture: ``2d356c83...ff4369`` at
  height 2,981,155).
* Special transaction types (PIVX v6.0+) append an
  ``Optional<vector<uint8>>`` ``extraPayload``: a one-byte presence flag
  followed by a compact-size length and the payload bytes.  PIVX v6.0+
  special transactions parse correctly, including any shielded
  components they carry.

Supported Operations
-------------------

Receiving Shielded Funds
~~~~~~~~~~~~~~~~~~~~~~~~~

Light wallets can scan for incoming shielded transactions using trial decryption:

1. Call ``blockchain.sapling.get_active_heights`` to learn which
   blocks contain Sapling activity (skip the rest)
2. Call ``blockchain.sapling.get_block_range`` to fetch those compact
   blocks
3. Trial decrypt outputs using viewing keys
4. Detect owned notes and calculate balance

Detecting Spent Notes
~~~~~~~~~~~~~~~~~~~~

To detect when shielded notes are spent:

1. Call ``blockchain.sapling.get_block_range`` to get nullifiers
2. Check nullifiers against owned notes
3. Update balance when matches found

Spending Shielded Funds
~~~~~~~~~~~~~~~~~~~~~~~

**Status**: server-assisted witness generation is implemented.

When the ``pivx_sapling_witness`` helper binary (see
``contrib/pivx_sapling_witness``) is configured, the server serves
anchor-bound canonical Sapling witnesses via
``blockchain.sapling.get_witness`` and a canonical best anchor via
``blockchain.sapling.get_best_anchor``.  Witness roots and requested
anchors are validated against the consensus ``finalsaplingroot`` values
indexed from block headers, and the server fails closed on any mismatch.

**Two client approaches are documented**:

1. **Server Witnesses** (Implemented)

   - Wallet calls ``get_witness`` per note and generates proofs locally
   - No local tree, no initial tree sync
   - Privacy tradeoff: server learns which note positions are spent
     (never keys or amounts)

2. **Local Incremental Merkle Tree** (Privacy-preserving alternative)

   - Wallet maintains own Sapling tree by syncing commitments
   - Generates witnesses and proofs locally
   - Full privacy; verify the local tree against
     ``blockchain.sapling.get_tree_state`` consensus roots
   - Initial sync: 1-2 hours, ~1GB storage

**For complete implementation details**, including:

- System architecture and data flows
- Step-by-step spend transaction construction
- Incremental Merkle tree algorithms
- Reorg handling and safety protocols
- Security and privacy analysis
- Phased implementation plan

See the comprehensive technical specification:
:doc:`pivx-sapling-spending`

API Reference
-------------

PIVX Sapling ElectrumX v1 Contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cake Wallet should probe ``blockchain.sapling.capabilities`` before enabling
Sapling sync/send routes.  A production-ready server returns (abridged):

.. code-block:: json

   {
     "success": true,
     "contract": "pivx.sapling.electrumx.v1",
     "version": 1,
     "network": "mainnet",
     "sapling_activation_height": 2700500,
     "max_block_range": 100,
     "range_response": "envelope",
     "hex_byte_order": "display",
     "consensus_anchors": true,
     "features": {
       "global_output_positions": true,
       "block_hashes": true,
       "structured_errors": true,
       "canonical_witnesses": true,
       "consistent_db_height": true,
       "supports_mempool": true,
       "supports_mempool_subscribe": true
     },
     "witness_response": "canonical_path",
     "witness_path_length": 32,
     "witness_path_order": "leaf_to_root",
     "witness_path_encoding": "sapling_node_to_bytes_hex",
     "release_contract_ready": true,
     "index_status": {
       "ready": true,
       "state": "ready",
       "db_height": 5057600,
       "daemon_height": 5057600,
       "lag": 0,
       "sapling_output_count": 12345,
       "retryable": false,
       "consistent_db_height": true
     }
   }

The capability probe also advertises supported methods, aliases, range
response format details, and the structured range and witness error types.
``hex_byte_order: "display"`` and ``consensus_anchors: true`` are part of
the v1 contract (see `Hex Byte Order`_ below).
``features.canonical_witnesses`` and ``witness_response`` reflect whether
the ``pivx_sapling_witness`` helper is configured on the server;
``release_contract_ready`` is only true when all features are available and
the index has caught up to the daemon tip.  Cake Wallet treats legacy
servers without this v1 release contract as compatibility-only.

``features.consistent_db_height: true`` declares that ``db_height`` is a
**committed watermark**: the new heights of a flush are published only
after the write batch carrying that flush's rows has committed, so for
any height ``X <= db_height`` every Sapling read path (``get_block_range``,
``get_outputs``, ``get_active_heights``, ``get_tree_state``,
``get_commitment_info``, witness/anchor lookups) already serves X's final
committed data — there is no instant at which ``db_height >= X`` while X
reads empty or incomplete, and no empty-then-populated flip for a
just-indexed block.  On reorg the guarantee is directional the other
way: the lowered ``db_height`` is published *before* the rolled-back
rows can disappear (the watermark never promises reverted heights), and
the purge plus the lowered persisted state commit in one atomic batch.
``get_active_heights`` and ``get_outputs`` — the two feeds that carry
no per-height block hashes for the client to cross-check — additionally
re-validate against a monotonic reorg counter after reading: a request
that straddles a reorg (even one whose replacement branch regrew to the
same height) returns a retryable ``index_incomplete`` error instead of
a silently incomplete result.  ``get_block_range`` responses carry
``block_hashes``, which the standard reorg rescan policy cross-checks.
A client may therefore scan exactly up to ``db_height`` and advance its
cursor without a safety margin (keeping the standard reorg rescan
policy); on servers lacking this flag, stay a few blocks behind
``db_height`` or re-verify recently scanned blocks.

Production v1 method surface:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Canonical method
     - Registered aliases
   * - ``blockchain.sapling.capabilities``
     - ``blockchain.sapling.get_capabilities``, ``server.sapling.capabilities``, ``sapling.capabilities``, ``get_capabilities``
   * - ``blockchain.sapling.get_block_range``
     - ``blockchain.sapling.get_blocks``, ``sapling.get_block_range``, ``get_block_range``
   * - ``blockchain.sapling.get_active_heights``
     - ``sapling.get_active_heights``
   * - ``blockchain.sapling.get_mempool``
     - ``sapling.get_mempool``
   * - ``blockchain.sapling.get_outputs``
     - (no aliases)
   * - ``blockchain.sapling.mempool.subscribe``
     - ``sapling.mempool.subscribe``
   * - ``blockchain.sapling.mempool.unsubscribe``
     - ``sapling.mempool.unsubscribe``
   * - ``blockchain.sapling.get_nullifier_status``
     - ``blockchain.sapling.check_nullifier``, ``sapling.get_nullifier_status``
   * - ``blockchain.sapling.check_nullifiers``
     - (no aliases)
   * - ``blockchain.sapling.get_commitment_info``
     - ``blockchain.sapling.get_commitment``, ``blockchain.commitment.get_info``, ``sapling.get_commitment_info``
   * - ``blockchain.sapling.get_best_anchor``
     - ``blockchain.sapling.best_anchor``, ``sapling.get_best_anchor``
   * - ``blockchain.sapling.get_anchor_height``
     - ``blockchain.anchor.get_height``, ``sapling.get_anchor_height``
   * - ``blockchain.sapling.get_tree_state``
     - ``blockchain.sapling.get_treestate``, ``sapling.get_tree_state``
   * - ``blockchain.sapling.get_witness``
     - ``sapling.get_witness``
   * - ``blockchain.sapling.get_witnesses``
     - (no aliases)

``blockchain.nullifier.get_spend`` remains registered as a legacy lookup route.
It is not advertised as a strict alias for ``get_nullifier_status`` because its
unspent response is ``null`` rather than ``{"spent": false}``.
``blockchain.sapling.get_outputs`` is advertised in the v1 method list;
``blockchain.transaction.get_sapling`` is served as an auxiliary method
outside it.
``blockchain.sapling.get_nullifiers`` has been **removed**: clients derive
spend information from the ``spends`` arrays in ``get_block_range``.

Hex Byte Order
~~~~~~~~~~~~~~

All 32-byte uint256-like values in Sapling RPC parameters and responses --
``cmu``, ``nullifier``, ``anchor``/``root``, ``cv``, ``rk``, ``epk``,
transaction ids, and block hashes -- are hex strings in PIVX Core RPC
*display* byte order (``uint256::GetHex()``, i.e. byte-reversed relative to
serialization order).  Capabilities advertise this as
``hex_byte_order: "display"``.

The one exception is witness ``path`` elements: they are raw canonical
Sapling node encodings (little-endian ``sapling::Node::to_bytes()``) and
are **not** byte-reversed.  Capabilities advertise this as
``witness_path_encoding: "sapling_node_to_bytes_hex"``.

Ciphertexts (``ciphertext``/``enc_ciphertext``/``out_ciphertext``) are
natural-order byte vectors.

blockchain.sapling.get_block_range
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get compact block data for scanning. **Primary API for sync.**

The method serves **only the server's indexed chain**: block hashes are
taken from the server's own index and the raw blocks are fetched from the
daemon by those exact hashes, so a response can never mix in blocks from a
diverging daemon tip.  Ranges extending above the indexed tip are rejected
with an ``index_incomplete`` error that includes ``indexed_height`` so
clients can clamp and retry.

**Parameters:**

* ``start_height`` (int): Starting block height (inclusive)
* ``end_height`` (int): Ending block height (inclusive)
* Maximum range: 100 blocks per request

**Returns:**

.. code-block:: json

   {
     "success": true,
     "complete": true,
     "empty": false,
     "contract": "pivx.sapling.electrumx.v1",
     "start_height": 5057529,
     "end_height": 5057529,
     "height_count": 1,
     "block_count": 1,
     "sapling_tx_count": 1,
     "block_hashes": [
       {
         "height": 5057529,
         "block_hash": "86165f..."
       }
     ],
     "blocks": [
       {
         "height": 5057529,
         "hash": "86165f...",
         "block_hash": "86165f...",
         "time": 1756978980,
         "outputs": [
           {
             "position": 1234,
             "global_position": 1234,
             "txid": "b1fd0e7f...",
             "tx_index": 3,
             "output_index": 0,
             "cmu": "a3a5aca5...",
             "epk": "28e5a699...",
             "ephemeral_key": "28e5a699...",
             "ciphertext": "b76937c4...",
             "enc_ciphertext": "b76937c4...",
             "cv": "...",
             "out_ciphertext": "..."
           }
         ],
         "txs": [
           {
             "txid": "b1fd0e7f...",
             "outputs": ["..."],
             "spends": [
               {
                 "nullifier": "...",
                 "cv": "...",
                 "anchor": "...",
                 "rk": "...",
                 "spend_index": 0
               }
             ]
           }
         ]
       }
     ],
     "error": null
   }

``success`` and ``complete`` are true only when every requested height was
scanned.  Empty successful ranges return ``success=true``, ``complete=true``,
``empty=true``, and ``error=null``.  Daemon, index, method, and invalid-range
failures return ``success=false``, ``complete=false``, and a structured
``error`` object, so a failed range never looks complete.  The structured
range error types are ``invalid_range``, ``daemon_error``,
``backend_timeout``, ``index_not_ready``, ``missing_block``,
``index_incomplete``, ``index_error``, ``unsupported_method``, and
``server_error``.  (The earlier ``missing_block_hash``, ``partial_index``,
and ``pruned_range`` error types no longer exist.)

Every output's ``position``/``global_position`` is looked up in the
server's commitment index; if any commitment in the range is missing from
the index the whole range fails with ``index_incomplete`` (or
``index_error`` on a database fault) rather than returning outputs without
verified positions.  All 32-byte values are display byte order (see
`Hex Byte Order`_).

``block_hashes`` contains every scanned height, including heights without
Sapling transactions.  Cake Wallet and other clients should persist these hashes
with scanned state and compare them during rollback-window rescans to detect
stale local state after reorgs.

**Usage Example:**

.. code-block:: python

   # Sync 100 blocks at a time
   start = 2700500  # Sapling activation
   batch_size = 100

   while start < current_height:
       end = min(start + batch_size - 1, current_height)
       response = await electrum.request(
           'blockchain.sapling.get_block_range',
           start, end
       )

       if not response['success']:
           raise RuntimeError(response['error'])

       # Process blocks
       for block in response['blocks']:
           for tx in block['txs']:
               # Trial decrypt outputs
               for output in tx['outputs']:
                   try_decrypt(output['cmu'], output['epk'],
                              output['ciphertext'])

               # Check nullifiers
               for spend in tx['spends']:
                   check_nullifier(spend['nullifier'])

       start = end + 1

blockchain.sapling.get_active_heights
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Return the heights in ``[start_height, end_height]`` that contain at
least one Sapling spend or output -- exactly the set of blocks
``get_block_range`` reports non-empty.  PIVX shielded activity is
sparse, so a restoring wallet should fetch this first and call
``get_block_range`` only for the returned heights instead of scanning
every 100-block window.

Feature-detect via ``capabilities.supports_active_height_index`` and
fall back to plain range scanning when absent.

**Params:** ``start_height`` (required), ``end_height`` (optional,
defaults to ``start_height``), ``limit`` (optional, default 10000,
capped at ``capabilities.active_heights_max_limit`` = 50000).

**Result:**

.. code-block:: python

   {
       'heights': [2700501, 2700734],  # ascending, plain ints
       'start': 2700500,
       'end': 3200000,        # last height actually covered
       'complete': True,
       'db_height': 5552849,  # the server's indexed tip
   }

Semantics:

- The range is clamped to the indexed tip: ``end`` is
  ``min(end_height, db_height)`` and requests entirely above the tip
  return ``heights: [], complete: true`` -- unlike ``get_block_range``
  this is not an error.  Exception: while the index trails the daemon
  past its tolerance (lag > 2), a request extending above the tip
  returns the retryable ``index_not_ready`` error instead (ranges
  wholly at or below ``db_height`` are always served).
- If more than ``limit`` heights match, the first ``limit`` are
  returned with ``complete: false`` and ``end`` set to the last
  returned height; resume the scan at ``end + 1``.  Paging is
  deterministic.
- If the index trails the daemon past its tolerance the response
  carries the same retryable ``index_not_ready`` error object
  ``get_block_range`` uses (with ``complete: false``); partial results
  are never served in that state.
- Heights are plain JSON integers; ``hex_byte_order`` does not apply.

The index is maintained atomically with the rest of the Sapling index
and is rebuilt automatically (one-time, from the existing
commitment/nullifier indexes) when a server synced before this method
existed restarts -- no resync is required.

blockchain.sapling.get_mempool
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Sapling components of every unconfirmed mempool transaction, for 0-conf
shielded visibility (modeled on Zcash lightwalletd's
``GetMempoolStream``).  The client trial-decrypts the outputs locally
with its viewing key; the server never learns which notes belong to the
wallet.  Feature-detect via ``features.supports_mempool`` and poll on
the existing shielded-sync cadence.

**Params:** none.  Read-only, idempotent snapshot.

**Result:**

.. code-block:: python

   {
       'txs': [
           {
               'txid': '<hex>',
               'first_seen': 1690000000,   # unix time first processed
               'outputs': [
                   {'cmu': '...', 'epk': '...', 'ephemeral_key': '...',
                    'cv': '...', 'ciphertext': '...',
                    'enc_ciphertext': '...', 'out_ciphertext': '...'},
               ],
               'spends': [
                   {'nullifier': '...', 'cv': '...', 'anchor': '...',
                    'rk': '...'},
               ],
           },
       ],
       'truncated': False,
   }

Invariants:

- Byte order is identical to ``get_block_range``: 32-byte fields
  (``cmu``, ``epk``, ``cv``, ``nullifier``, ``anchor``, ``rk``) in
  display order, ciphertexts as raw wire bytes.  The same tx later
  mined carries byte-for-byte identical ``cmu``/``epk``/ciphertext
  values in ``get_block_range``; clients dedup by txid + ``cmu``.
- Mempool outputs have **no tree position and no anchor validation** —
  ``position``/``global_position``/``index`` fields are deliberately
  absent (never zero-filled).  Render decrypted notes as "incoming,
  not spendable" until the tx appears at a real height.
- Mempool contents never affect ``db_height``, ``daemon_height``, tree
  state, or the Sapling index.
- Bounded: at most 1000 txs / 10000 outputs per response;
  ``truncated: true`` when the cap was hit.
- Not ready (no snapshot yet, or the server trails its daemon beyond
  the routine 1-2 block processing window): ``{'txs': [], 'truncated':
  false, 'error': {'type': 'mempool_not_ready', 'message': '...',
  'retryable': true}}`` — distinguish from a genuinely empty mempool,
  which has no ``error``.

blockchain.sapling.mempool.subscribe
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Push variant of ``get_mempool``.  Subscribing returns the current
snapshot (exactly the ``get_mempool`` envelope); thereafter the server
pushes the same envelope as a JSON-RPC notification (method
``blockchain.sapling.mempool.subscribe``, single parameter) whenever
the set of Sapling-carrying mempool transactions changes — a tx
arriving, being mined, or being evicted — or when snapshot
availability flips (a ``mempool_not_ready`` push is always followed by
a corrective push once the server recovers, even if the tx set never
changed).

Notifications are **full state replacements**: apply the latest one;
there is no delta protocol and nothing to miss.  The subscription
lasts for the session lifetime or until
``blockchain.sapling.mempool.unsubscribe`` (returns whether a
subscription was active).  Feature-detect via
``features.supports_mempool_subscribe``; fall back to polling
``get_mempool`` when absent.

blockchain.sapling.get_outputs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get Sapling outputs for trial decryption. Alternative to ``get_block_range``
when only outputs are needed (no nullifiers).

Like ``get_block_range``, this serves only the indexed chain: raw blocks
are fetched by the hashes in the server's own index, and ranges extending
above the indexed tip are rejected.

**Parameters:**

* ``start_height`` (int): Starting block height (inclusive)
* ``end_height`` (int): Ending block height (inclusive; max 100 blocks per
  request, must not exceed the indexed tip)
* ``limit`` (int, optional): Max outputs (default 1000, capped at 10000)

**Returns:**

.. code-block:: json

   {
     "outputs": [
       {
         "txid": "...",
         "index": 0,
         "height": 5057529,
         "cmu": "...",
         "epk": "...",
         "enc_ciphertext": "..."
       }
     ],
     "count": 1,
     "start_height": 5057529,
     "end_height": 5057529,
     "more": false
   }

``cmu`` and ``epk`` are display byte order; ``enc_ciphertext`` is a
natural-order byte vector.

blockchain.sapling.get_nullifier_status
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Check whether a single Sapling nullifier is indexed as spent.  This is the
preferred v1 status method for Cake Wallet live helper validation.
``blockchain.sapling.check_nullifiers`` accepts a list of up to 10,000
nullifiers (or ``{"nullifiers": [...]}``) and returns an envelope
``{"success": true, "contract": ..., "results": {nullifier: status}}``
with the same status objects keyed by nullifier under ``results``.

**Parameters:**

* ``nullifier_hex`` (string): 32-byte nullifier as hex, display byte order

**Unknown/unspent response:**

.. code-block:: json

   {
     "spent": false,
     "tx_hash": null,
     "txid": null,
     "height": null,
     "spend_index": null
   }

**Spent response:**

.. code-block:: json

   {
     "spent": true,
     "tx_hash": "...",
     "txid": "...",
     "height": 5057530,
     "spend_index": 0
   }

blockchain.sapling.get_commitment_info
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Return indexed transaction and global-position metadata for one Sapling
commitment.  Unknown commitments return a structured absent response, not
``null``.

**Parameters:**

* ``commitment_hex`` (string): 32-byte commitment as hex, display byte order

**Unknown response:**

.. code-block:: json

   {
     "exists": false,
     "txid": null,
     "output_index": null,
     "height": null,
     "position": null
   }

**Known response:**

.. code-block:: json

   {
     "exists": true,
     "txid": "...",
     "output_index": 0,
     "height": 5057529,
     "position": 1234
   }

blockchain.sapling.get_best_anchor
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Return the canonical best Sapling anchor available to light-wallet send
logic.  The anchor is computed by the witness helper over the indexed
commitment stream and then **validated against the consensus anchor
index** (the ``finalsaplingroot`` values recorded from block headers,
including a tree-size cross-check).  The server fails closed: if the
helper is unavailable, the index has not caught up, or validation fails,
a structured unavailable response is returned rather than an anchor that
a canonical witness could not bind to.  The result is cached and only
recomputed when the indexed tree contents change.

**Returns with a validated anchor:**

.. code-block:: json

   {
     "available": true,
     "anchor": "...",
     "root": "...",
     "height": 5057600,
     "anchor_height": 5057529,
     "tree_size": 12345,
     "block_hash": "..."
   }

``anchor``/``root`` are display byte order.  ``height`` is the indexed
tip; ``anchor_height`` is the height of the last Sapling output included
in the tree, so it is stable across later blocks without Sapling
activity.

**Returns when no canonical anchor can be served:**

.. code-block:: json

   {
     "available": false,
     "anchor": null,
     "root": null,
     "height": 5057600,
     "anchor_height": null,
     "tree_size": 12345,
     "block_hash": "...",
     "error": {
       "type": "canonical_anchor_unavailable",
       "message": "..."
     }
   }

When the index trails the daemon past its tolerance the unavailable
response instead carries the full retryable ``index_not_ready`` error
object (``type``, ``retryable``, ``db_height``, ``daemon_height``,
``lag``, ...), and ``block_hash`` is ``null`` in that state.

blockchain.nullifier.get_spend
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Legacy lookup for the transaction that spent a specific nullifier.  Prefer
``blockchain.sapling.get_nullifier_status`` for Cake Wallet v1 status checks.

**Parameters:**

* ``nullifier_hex`` (string): 32-byte nullifier as hex, display byte order

**Returns:**

.. code-block:: json

   {
     "txid": "...",
     "height": 5057530,
     "spend_index": 0
   }

Returns ``null`` when the nullifier is not indexed as spent.  While
the index trails the daemon past its tolerance it instead returns a
**truthy** envelope with ``txid: null`` and a retryable
``index_not_ready`` ``error`` object -- test ``txid``, never bare
truthiness (or use ``get_nullifier_status``).

blockchain.sapling.get_witness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Return an anchor-bound canonical Sapling witness for an output position or
commitment.

**Current production status**: implemented, backed by the
``pivx_sapling_witness`` helper binary (``contrib/pivx_sapling_witness``,
built on librustzcash's Sapling primitives).  Without the helper the
method fails with ``witness_backend_unavailable``; capabilities advertise
availability as ``features.canonical_witnesses`` and
``witness_response: "canonical_path"``.

**Parameters:**

* ``position`` (int or 32-byte commitment hex in display byte order):
  Global Sapling output position, or a commitment whose global position is
  indexed.
* ``anchor_hex`` (string, optional): 32-byte Sapling root/anchor, display
  byte order.  Requested anchors must be consensus anchors of the indexed
  chain: they are validated against the consensus anchor index *before*
  the helper runs, and unknown anchors fail with ``index_incomplete``.
  When omitted, the witness is bound to the current tree's anchor.

**Returns:**

.. code-block:: json

   {
     "commitment": "...",
     "cmu": "...",
     "position": 1234,
     "global_position": 1234,
     "height": 5057529,
     "txid": "...",
     "output_index": 0,
     "anchor": "...",
     "root": "...",
     "anchor_height": 5057529,
     "tree_size": 12345,
     "path": ["...", "..."],
     "witness": ["...", "..."],
     "path_order": "leaf_to_root",
     "path_encoding": "sapling_node_to_bytes_hex",
     "path_length": 32
   }

``path`` contains exactly 32 leaf-to-root sibling nodes encoded as
canonical 32-byte little-endian PIVX Sapling ``sapling::Node::to_bytes()``
values (**not** display byte-reversed); ``cmu``, ``txid``, and
``anchor``/``root`` are display byte order.

**Consensus validation (fail closed)**: the helper's witness root must be
a consensus ``finalsaplingroot`` indexed from block headers, and its tree
size must match the value recorded for that anchor.  Any mismatch fails
the request with ``index_incomplete`` instead of returning an
internally-consistent but unspendable witness.  Structured witness error
types are ``witness_backend_unavailable``, ``witness_backend_timeout``,
``witness_backend_error``, ``commitment_not_found``,
``anchor_not_found``, ``anchor_mismatch``, ``index_incomplete``, and
``backend_timeout`` (the outer per-request timeout; all surfaced as
``RPCError`` messages prefixed ``<type>:``).

``blockchain.sapling.get_witnesses`` accepts a list of up to 100
positions/commitments plus an optional shared ``anchor_hex`` and returns a
list of witness responses.

**Cost and concurrency**: witness requests are the most expensive Sapling
calls, and session cost accounting scales with the size of the indexed
tree.  Helper subprocesses are bounded by a server-wide concurrency
semaphore.  Note the known performance ceiling: each helper call ships
the server's full commitment stream as JSON to a per-request subprocess.
This is mitigated by the helper's on-disk incremental Merkle level cache
(``PIVX_SAPLING_WITNESS_STATE_FILE``), a server-side commitment stream
cache that is rebuilt off the event loop and invalidated on reorgs, the
semaphore, and cost accounting -- adequate for the current PIVX shielded
pool.  If the pool grows large, the documented upgrade path is a
persistent helper process with an incremental protocol.

blockchain.sapling.get_tree_state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get commitment tree state at a height.  The response is served entirely
from the server's index: the anchor is the consensus
``finalsaplingroot`` read from the indexed block header at that height,
and the tree size is found by binary search over the position index.
This lets a client bound and verify a locally built commitment tree
without trusting helper-computed values.

**Parameters:**

* ``height`` (int, optional): Block height; defaults to the indexed tip.
  Must be between the Sapling activation height and the indexed tip.

**Returns:**

.. code-block:: json

   {
     "success": true,
     "contract": "pivx.sapling.electrumx.v1",
     "height": 5057529,
     "block_hash": "...",
     "anchor": "...",
     "root": "...",
     "latest_anchor": "...",
     "anchor_first_height": 5057529,
     "tree_size": 12345,
     "commitment_count": 12345,
     "indexed_height": 5057600,
     "sapling_activation_height": 2700500
   }

``anchor``/``root``/``latest_anchor`` all carry the same display-order
consensus root; ``anchor_first_height`` is the first height that root
appeared at.  Failures return ``success=false`` with a structured
``error`` object: ``index_incomplete`` (height above the indexed tip,
with ``indexed_height``; or root not yet indexed), ``invalid_range``
(height below Sapling activation, with ``sapling_activation_height``),
or ``index_error`` (no Sapling root in the indexed header).

blockchain.transaction.get_sapling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get Sapling data for a specific transaction (auxiliary method).

**Parameters:**

* ``txid`` (string): Transaction ID as hex
* ``verbose`` (bool, optional): Include full spend/output component data

**Returns:**

.. code-block:: json

   {
     "txid": "...",
     "is_sapling": true,
     "value_balance": 0,
     "spend_count": 1,
     "output_count": 2
   }

With ``verbose=true`` the response also includes ``spends`` (each with
``nullifier``, ``anchor``, ``cv``, ``rk``) and ``outputs`` (each with
``cmu``, ``epk``, ``enc_ciphertext``, ``cv``), all 32-byte values in
display byte order.  ``value_balance`` and the counts are omitted for
non-Sapling transactions (``is_sapling: false``).

Setup and Configuration
----------------------

Enabling PIVX Sapling Support
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Set the coin in your ElectrumX configuration:

.. code-block:: bash

   COIN=PIVX          # mainnet
   # COIN=PIVX NET=testnet

The ``Pivx`` coin class automatically:

* Uses ``PIVXSaplingBlockProcessor`` for indexing
* Uses ``PIVXSaplingElectrumX`` session class
* Uses ``DeserializerPIVX`` for transaction parsing
* Indexes Sapling data from activation height

.. warning:: **Operator upgrade note -- resync required.**  The on-disk
   Sapling index format is versioned (``DB.SAPLING_INDEX_VERSION = 1``)
   and the version is stamped into the UTXO DB state row at every flush.
   A database that is synced past Sapling activation but does not carry
   the current index version (for example, one built by an earlier
   iteration of this branch) **refuses to open**::

      DB is synced past Sapling activation with Sapling index version
      None but this software requires version 1; resync from genesis to
      rebuild the Sapling index

   There is no in-place upgrade: delete the database directory and
   resync from genesis.  Databases that have not yet reached Sapling
   activation are unaffected.

Database Schema
~~~~~~~~~~~~~~~

The Sapling index lives in the UTXO database under five key prefixes
(all keys and values in raw serialization byte order; display-order
conversion happens only at the RPC boundary):

* ``b'N' + nullifier (32)`` ->
  ``tx_hash (32) + height (4, BE) + spend_index (2, BE)``
* ``b'C' + commitment (32)`` ->
  ``tx_hash (32) + output_index (2, BE) + height (4, BE) + position (8, BE)``
* ``b'P' + position (8, BE)`` ->
  ``tx_hash (32) + output_index (2, BE) + height (4, BE) + commitment (32)``
* ``b'A' + root (32)`` ->
  ``first_seen_height (4, BE) + tree_size (8, BE)``
* ``b'S' + height (4, BE)`` -> ``b''`` (the height carries at least one
  Sapling spend or output; backs ``get_active_heights`` and is
  backfilled once at startup on databases synced before it existed)

``b'A'`` entries index the consensus ``finalsaplingroot`` from PIVX v8+
block headers (header bytes 80:112, raw little-endian serialization
order), recorded the first time each root appears together with the
number of note commitments in the tree when that root formed
(first-seen semantics).  They are the consensus source of truth against
which witness and best-anchor responses are validated.

Global output positions are assigned at block-advance time in canonical
block/transaction/``vShieldOutput`` order and flushed atomically with
the UTXO state.  The persisted output count only advances at UTXO
flushes, so a crash can never leave the counter ahead of the flushed
index.  Tree sizes at historical heights are answered by binary search
over the position index.

Earlier iterations of this branch kept a synthetic ``double_sha256``
root table under ``b'R'``, computed witnesses inside the database layer,
recomputed the tree per height at flush time, and scanned whole
keyspaces to answer per-height queries (``iter_nullifiers_by_height``
and friends, tree-state scans).  All of that is **removed**; the current
index is append-only and query-by-key.

Enabling Canonical Witnesses
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Witness and best-anchor computation requires the ``pivx_sapling_witness``
helper binary.  Build it and point the server at it:

.. code-block:: bash

   cd contrib/pivx_sapling_witness
   cargo build --release

   # Explicit path (otherwise pivx_sapling_witness is looked up on PATH)
   PIVX_SAPLING_WITNESS_HELPER=/path/to/pivx_sapling_witness

Optional tuning:

.. code-block:: bash

   # Helper subprocess timeout in seconds (default 8, minimum 1)
   PIVX_SAPLING_WITNESS_HELPER_TIMEOUT=8

   # Helper's incremental Merkle level cache; defaults to
   # <DB_DIRECTORY>/pivx_sapling_witness_state.bin
   PIVX_SAPLING_WITNESS_STATE_FILE=/path/to/state.bin

   # Per-request Sapling RPC timeout in seconds (default 8)
   PIVX_SAPLING_RPC_TIMEOUT=8

   # Log Sapling requests slower than this many seconds (default 1)
   PIVX_SAPLING_SLOW_LOG_SECONDS=1

Without the helper the server still indexes and serves everything except
witnesses: ``get_witness`` fails with ``witness_backend_unavailable`` and
``get_best_anchor`` returns a structured unavailable response, while
capabilities report ``canonical_witnesses: false``.

Storage Requirements
~~~~~~~~~~~~~~~~~~~

Sapling indexing adds, per shielded item, in the UTXO database:

* **Per spend**: one nullifier record (~70 bytes)
* **Per output**: one commitment record and one position record
  (~160 bytes combined)
* **Per anchor**: one record per distinct ``finalsaplingroot`` (~45 bytes)

The commitment tree itself is not stored in the database: witnesses are
computed by the helper, whose optional state file caches the full Merkle
level structure (roughly 64 bytes per commitment).  Given the size of the
PIVX shielded pool, the Sapling index is small next to the base UTXO and
history databases.

Sync Time
~~~~~~~~~

Initial sync from genesis:

* **Block processing**: ~same as standard ElectrumX
* **Sapling indexing**: Adds ~10-20% overhead after activation block
  (append-only key/value writes; no tree computation happens at flush)
* **Witness tree**: built by the helper on the first witness/anchor call
  and cached in its state file thereafter

After initial sync, incremental updates are very fast.

Performance Tuning
~~~~~~~~~~~~~~~~~

For optimal sync performance:

.. code-block:: bash

   # Increase cache sizes
   CACHE_MB=2000

   # Use fast storage
   DB_DIRECTORY=/path/to/nvme/storage

   # Increase daemon timeout for large batches
   DAEMON_TIMEOUT=300

Client Integration
------------------

Sync Strategy
~~~~~~~~~~~~~

Recommended approach for light wallets:

1. **Initial Sync**:

   * Start from Sapling activation (block 2,700,500)
   * Fetch at most 100-block batches using ``get_block_range``
   * Trial decrypt all outputs
   * Track all nullifiers for owned notes
   * Store wallet state to disk after each batch

2. **Incremental Sync**:

   * Resume from last synced height
   * Fetch new blocks since last sync
   * Update balance and transaction history

3. **Periodic Re-sync / Reorg Handling**:

   * Re-scan recent blocks from
     ``max(SAPLING_START_HEIGHT, last_scanned_height - 99)``.
   * PIVX ElectrumX keeps ``REORG_LIMIT = 100`` for mainnet, so the server
     retains enough undo/raw-block state for at least 100-block rollback.
   * Compare returned ``block_hashes`` against locally stored scanned hashes.
     Any mismatch means local Sapling notes, nullifier status, tree state, and
     witnesses from that height forward must be rolled back and rescanned.
   * Verify nullifier status hasn't changed

Server-side reorg behavior:

* Removed Sapling outputs delete their commitment and position index
  entries.
* Removed Sapling spends delete their nullifier index entries.
* Anchors (``b'A'`` roots) first seen at or above the first reverted
  height are deleted; roots first seen earlier remain valid.
* Active-height entries (``b'S'``) at or above the first reverted
  height are deleted in the same batch.
* A nullifier removed by reorg may be indexed again if it is spent on a
  different branch.
* Global Sapling output positions are rolled back to the first removed
  position, so the replacement branch receives canonical positions for the new
  chain.
* The server-side witness caches (commitment stream and best anchor) are
  keyed on tree contents and invalidated when a reorg replaces them.

Example: Cake Wallet Integration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   class PIVXSaplingWallet:
       def __init__(self, electrum_server):
           self.server = electrum_server
           self.activation = 2700500
           self.batch_size = 100

       async def sync(self):
           """Full wallet sync"""
           current = await self.server.request(
               'blockchain.headers.subscribe'
           )
           tip = current['height']

           # Start from activation or last sync
           start = max(self.last_synced + 1, self.activation)

           while start <= tip:
               end = min(start + self.batch_size - 1, tip)

               # Get compact blocks
               response = await self.server.request(
                   'blockchain.sapling.get_block_range',
                   start, end
               )
               if not response['success'] or not response['complete']:
                   raise RuntimeError(response['error'])

               # Process each block
               for block in response['blocks']:
                   self.process_block(block)

               # Save progress
               self.last_synced = end
               await self.save_state()

               start = end + 1

       def process_block(self, block):
           """Process single block"""
           for tx in block['txs']:
               # Trial decrypt outputs
               for output in tx['outputs']:
                   note = self.try_decrypt(
                       output['cmu'],
                       output['epk'],
                       output['ciphertext']
                   )
                   if note:
                       self.add_note(note, tx['txid'],
                                    block['height'])

               # Check if our notes were spent
               for spend in tx['spends']:
                   if spend['nullifier'] in self.our_nullifiers:
                       self.mark_spent(spend['nullifier'])

Operator Notes: Request Throttling
----------------------------------

ElectrumX's session cost limiter (``COST_SOFT_LIMIT`` /
``COST_HARD_LIMIT`` / ``REQUEST_SLEEP`` environment settings) must be
sized for wallet refresh *bursts*: the PIVX client fetches balances
per address plus several Sapling calls per refresh, so one refresh is
tens of requests, not one.  Past the soft limit every request sleeps
``cost_fraction * REQUEST_SLEEP`` — with tight limits a refreshing
wallet degrades to ~1 request/second and its keep-alive times out,
which the user sees as the node flapping offline.  Two couplings are
easy to miss: per-session cost decay and the dead-session group-cost
refund both scale with ``COST_HARD_LIMIT`` (``hard/10000`` and
``hard/5000`` per second respectively), so a low hard limit also means
slow recovery and a reconnect spiral (each reconnect inherits retained
group cost); and every JSON-RPC *error* costs 100 points, so noisy
clients burn budget fast.

Recommended for public wallet-facing nodes::

    COST_SOFT_LIMIT=10000
    COST_HARD_LIMIT=100000
    REQUEST_SLEEP=1000

A full refresh burst (~50-200 cost) then never throttles, session
decay is ~10/sec, and genuine abuse still ramps to 1s/request sleeps
and disconnects at the hard limit.  Verify at startup: the log prints
``session cost hard limit`` / ``soft limit``.

Testing
-------

Test Server
~~~~~~~~~~~

For development and testing:

* **Server**: electrum02.chainster.org
* **Ports**: 50001 (TCP), 50002 (SSL), 50003 (WSS)

Test the connection:

.. code-block:: bash

   # Using electrum-client
   pip install electrum-client

   python3 << EOF
   import asyncio
   from electrum_client import ElectrumClient

   async def test():
       async with ElectrumClient(
           'electrum02.chainster.org', 50002, ssl=True
       ) as client:
           # Get server version
           result = await client.server_version()
           print(f"Server: {result}")

           # Get current height
           result = await client.request(
               'blockchain.headers.subscribe'
           )
           print(f"Height: {result['height']}")

           # Get Sapling block
           response = await client.request(
               'blockchain.sapling.get_block_range',
               5057529, 5057529
           )
           assert response['success'], response['error']
           print(f"Blocks: {response['block_count']}")
           if response['blocks']:
               print(f"TXs in block: {len(response['blocks'][0]['txs'])}")

   asyncio.run(test())
   EOF

Verification
~~~~~~~~~~~~

Verify Sapling data integrity:

.. code-block:: bash

   # Check if server has Sapling support
   electrum-client blockchain.sapling.get_block_range 2700500 2700500

   # Should return block with Sapling activation

Troubleshooting
--------------

No Shielded Balance Showing
~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. **Check activation height**: Ensure scanning from block 2,700,500+
2. **Verify server sync**: Server must be fully synced past activation
3. **Check method names**: Use exact method names (``blockchain.sapling.get_block_range``)
4. **Test connection**: Verify server is responding to Sapling methods

server.version Semantics
~~~~~~~~~~~~~~~~~~~~~~~~

PIVX Sapling sessions are request-order independent: no method requires
``server.version`` to have been sent first.  Sessions that never
negotiate are served at the server's minimum protocol version --
methods that only exist at higher protocol versions register only
after negotiating them, and the operator's ``DROP_CLIENT`` filter can
only apply once a client identifies -- so clients should still
negotiate early as usual.  ``server.version`` is
**idempotent**: repeated calls on a live session -- e.g. periodic
liveness checks -- always return the already-negotiated
``[server_version, protocol_version]`` result; repeat arguments are
ignored (the first negotiation wins) and the call never errors or
drops the connection.  JSON-RPC request ids are echoed verbatim with
their original type (a string id returns a string id).

Empty Results
~~~~~~~~~~~~

* **Before activation**: Blocks before 2,700,500 have no Sapling data
* **No shielded txs**: Many blocks have zero Sapling transactions
* **Check range**: Ensure height range is valid and at or below the
  server's ``indexed_height`` (ranges above it fail with
  ``index_incomplete``)

Performance Issues
~~~~~~~~~~~~~~~~~

* **Respect the range cap**: ``get_block_range`` serves at most 100
  blocks per request; use smaller ranges if responses are slow
* **Parallel requests**: Make multiple requests concurrently (session
  cost accounting still applies)
* **Witness calls are expensive**: each ``get_witness`` invocation feeds
  the full commitment stream to a helper subprocess; keep the helper
  state file enabled and batch related requests via ``get_witnesses`` (a round-trip
  convenience only -- each position still runs its own helper
  invocation)
* **Request timeouts**: Sapling requests are bounded by
  ``PIVX_SAPLING_RPC_TIMEOUT`` (default 8s).  Treat any
  ``backend_timeout`` as retryable: inside ``get_block_range`` it is a
  structured envelope error with ``retryable: true``; on every other
  method (and on the outer per-request timeout) it surfaces as a plain
  ``RPCError`` whose message starts ``backend_timeout:`` -- key on the
  message prefix
* **Server load**: Server may be under heavy load, try different server

Additional Resources
-------------------

* **PIVX Core**: https://github.com/PIVX-Project/PIVX
* **Sapling Protocol**: https://z.cash/technology/zksnarks/
* **Zcash Lightwalletd**: https://github.com/zcash/lightwalletd
* **ElectrumX Docs**: https://electrumx-spesmilo.readthedocs.io

Support
-------

For issues specific to PIVX Sapling support:

* Open issue at: https://github.com/spesmilo/electrumx
* Include: coin type, block height, error message, logs
