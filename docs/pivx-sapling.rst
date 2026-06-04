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

The implementation indexes all Sapling outputs (commitments), spends (nullifiers),
and maintains the commitment tree state required for light wallet operations.

Supported Operations
-------------------

Receiving Shielded Funds
~~~~~~~~~~~~~~~~~~~~~~~~~

Light wallets can scan for incoming shielded transactions using trial decryption:

1. Call ``blockchain.sapling.get_block_range`` to fetch compact blocks
2. Trial decrypt outputs using viewing keys
3. Detect owned notes and calculate balance

Detecting Spent Notes
~~~~~~~~~~~~~~~~~~~~

To detect when shielded notes are spent:

1. Call ``blockchain.sapling.get_block_range`` to get nullifiers
2. Check nullifiers against owned notes
3. Update balance when matches found

Spending Shielded Funds
~~~~~~~~~~~~~~~~~~~~~~~

**Status**: Design phase - full technical specification available

Witness/proof generation requires the full Sapling commitment tree. This is
a fundamental architectural challenge for light wallets.

**Two approaches are documented**:

1. **Local Incremental Merkle Tree** (Recommended)
   
   - Wallet maintains own Sapling tree by syncing commitments
   - Generates witnesses and proofs locally
   - Full privacy, no external dependencies
   - Initial sync: 1-2 hours, ~1GB storage
   - See: :doc:`pivx-sapling-spending` for complete specification

2. **Full Node Witness/Proof Service** (Fallback)
   
   - Delegate witness generation to trusted PIVX Core node
   - Lower storage requirements
   - Privacy tradeoff: node learns spending patterns
   - Requires trusted full node access

**For complete implementation details**, including:

- System architecture and data flows
- Step-by-step spend transaction construction
- Incremental Merkle tree algorithms
- Reorg handling and safety protocols
- Security and privacy analysis
- Phased implementation plan (15-20 weeks)

See the comprehensive technical specification:
:doc:`pivx-sapling-spending`

API Reference
-------------

PIVX Sapling ElectrumX v1 Contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cake Wallet should probe ``blockchain.sapling.capabilities`` before enabling
Sapling sync/send routes.  A production-ready server returns:

.. code-block:: json

   {
     "success": true,
     "contract": "pivx.sapling.electrumx.v1",
     "version": 1,
     "network": "mainnet",
     "sapling_activation_height": 2700500,
     "max_block_range": 100,
     "range_response": "envelope",
     "features": {
       "global_output_positions": true,
       "block_hashes": true,
       "structured_errors": true
     },
     "release_contract_ready": true
   }

The capability probe also advertises supported methods, aliases, range response
format details, and structured range error types.  Cake Wallet treats legacy
servers without this v1 release contract as compatibility-only.

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
   * - ``blockchain.sapling.get_nullifier_status``
     - ``blockchain.sapling.check_nullifier``, ``sapling.get_nullifier_status``
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

``blockchain.nullifier.get_spend`` remains registered as a legacy lookup route.
It is not advertised as a strict alias for ``get_nullifier_status`` because its
unspent response is ``null`` rather than ``{"spent": false}``.

blockchain.sapling.get_block_range
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get compact block data for scanning. **Primary API for sync.**

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
             "ciphertext": "b76937c4...",
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
``error`` object, so a failed range never looks complete.

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

blockchain.sapling.get_outputs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get Sapling outputs for trial decryption. Alternative to ``get_block_range``
when only outputs are needed (no nullifiers).

**Parameters:**

* ``start_height`` (int): Starting block height
* ``end_height`` (int): Ending block height
* ``limit`` (int, optional): Max outputs (default 1000)

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
     "more": false
   }

blockchain.sapling.get_nullifiers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get spent nullifiers in height range. Useful for detecting spent notes
without scanning outputs.

**Parameters:**

* ``start_height`` (int): Starting block height
* ``end_height`` (int): Ending block height

**Returns:**

.. code-block:: json

   {
     "nullifiers": [
       {
         "nullifier": "...",
         "txid": "...",
         "height": 5057530
       }
     ],
     "count": 1
   }

blockchain.nullifier.get_spend
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Legacy lookup for the transaction that spent a specific nullifier.  Prefer
``blockchain.sapling.get_nullifier_status`` for Cake Wallet v1 status checks.

**Parameters:**

* ``nullifier_hex`` (string): 32-byte nullifier as hex

**Returns:**

.. code-block:: json

   {
     "txid": "...",
     "height": 5057530,
     "spend_index": 0
   }

Returns ``null`` when the nullifier is not indexed as spent.

blockchain.sapling.get_witness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Return an anchor-bound witness for a Sapling output position or commitment.

**Parameters:**

* ``position`` (int or 32-byte commitment hex): Global Sapling output position,
  or a commitment whose global position is indexed.
* ``anchor_hex`` (string, optional): 32-byte Sapling root/anchor.  If supplied,
  the witness is generated against that exact root.

**Returns:**

.. code-block:: json

   {
     "anchor": "...",
     "root": "...",
     "anchor_height": 5057529,
     "position": 1234,
     "note_position": 1234,
     "path": [
       {
         "position": "left",
         "hash": "..."
       }
     ],
     "commitment": "..."
   }

Clients must verify that the path recomputes ``root`` from ``commitment`` and
``note_position`` before using the witness for spending.

blockchain.sapling.get_tree_state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get commitment tree state at height.

**Parameters:**

* ``height`` (int): Block height

**Returns:**

.. code-block:: json

   {
     "height": 5057529,
     "block_hash": "...",
     "tree_size": 12345,
     "commitment_count": 12345,
     "nullifier_count": 123,
     "latest_anchor": "...",
     "latest_anchor_height": 5057529
   }

blockchain.transaction.get_sapling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get Sapling data for specific transaction.

**Parameters:**

* ``txid`` (string): Transaction ID as hex

**Returns:**

.. code-block:: json

   {
     "txid": "...",
     "outputs": [...],
     "spends": [...],
     "binding_sig": "..."
   }

Setup and Configuration
----------------------

Enabling PIVX Sapling Support
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Set the coin in your ElectrumX configuration:

.. code-block:: bash

   COIN=PIVXSapling

The ``PIVXSapling`` coin class automatically:

* Uses ``PIVXSaplingBlockProcessor`` for indexing
* Uses ``PIVXSaplingElectrumX`` session class
* Indexes Sapling data from activation height

Storage Requirements
~~~~~~~~~~~~~~~~~~~

Sapling indexing adds approximately:

* **Per output**: ~100 bytes (commitment, ephemeral key, ciphertexts)
* **Per spend**: ~100 bytes (nullifier, cv, anchor, rk)
* **Merkle tree**: ~32 bytes per commitment

For PIVX with millions of blocks, expect additional database usage of several GB.

Sync Time
~~~~~~~~~

Initial sync from genesis:

* **Block processing**: ~same as standard ElectrumX
* **Sapling indexing**: Adds ~10-20% overhead after activation block
* **Tree building**: One-time overhead during first sync

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

* Removed Sapling outputs delete commitment and position indexes.
* Removed Sapling spends delete nullifier and anchor indexes.
* Sapling roots/anchors at or after the backed-up height are deleted.
* A nullifier removed by reorg may be indexed again if it is spent on a
  different branch.
* Global Sapling output positions are rolled back to the first removed
  position, so the replacement branch receives canonical positions for the new
  chain.

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
           blocks = await client.request(
               'blockchain.sapling.get_block_range',
               5057529, 5057529
           )
           print(f"Blocks: {len(blocks)}")
           if blocks:
               print(f"TXs in block: {len(blocks[0]['txs'])}")
   
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

Connection Refused
~~~~~~~~~~~~~~~~~

ElectrumX now allows clients to call any method without requiring
``server.version`` first. This was added for Cake Wallet compatibility.

Empty Results
~~~~~~~~~~~~

* **Before activation**: Blocks before 2,700,500 have no Sapling data
* **No shielded txs**: Many blocks have zero Sapling transactions
* **Check range**: Ensure height range is valid and within chain bounds

Performance Issues
~~~~~~~~~~~~~~~~~

* **Reduce batch size**: Use smaller ranges (500 instead of 1000)
* **Parallel requests**: Make multiple requests concurrently
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
