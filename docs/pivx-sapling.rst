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

**Note**: Witness/proof generation requires the full Sapling tree and is not
yet implemented in ElectrumX. For spending, wallets should:

* Use the full PIVX node for witness generation, OR
* Maintain local incremental Merkle tree by syncing from genesis

API Reference
-------------

blockchain.sapling.get_block_range
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get compact block data for scanning. **Primary API for sync.**

**Parameters:**

* ``start_height`` (int): Starting block height (inclusive)
* ``end_height`` (int): Ending block height (inclusive)
* Maximum range: 1000 blocks per request

**Returns:**

.. code-block:: json

   [
     {
       "height": 5057529,
       "hash": "86165f...",
       "time": 1756978980,
       "txs": [
         {
           "txid": "b1fd0e7f...",
           "outputs": [
             {
               "cmu": "a3a5aca5...",
               "epk": "28e5a699...",
               "ciphertext": "b76937c4...",
               "cv": "...",
               "out_ciphertext": "..."
             }
           ],
           "spends": [
             {
               "nullifier": "...",
               "cv": "...",
               "anchor": "...",
               "rk": "..."
             }
           ]
         }
       ]
     }
   ]

**Usage Example:**

.. code-block:: python

   # Sync 1000 blocks at a time
   start = 2700500  # Sapling activation
   batch_size = 1000
   
   while start < current_height:
       end = min(start + batch_size - 1, current_height)
       blocks = await electrum.request(
           'blockchain.sapling.get_block_range',
           start, end
       )
       
       # Process blocks
       for block in blocks:
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

Check if a specific nullifier has been spent.

**Parameters:**

* ``nullifier_hex`` (string): 32-byte nullifier as hex

**Returns:**

.. code-block:: json

   {
     "spent": true,
     "txid": "...",
     "height": 5057530
   }

blockchain.sapling.get_tree_state
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Get commitment tree state at height.

**Parameters:**

* ``height`` (int): Block height

**Returns:**

.. code-block:: json

   {
     "height": 5057529,
     "hash": "...",
     "tree_size": 12345,
     "sapling_tree": "..."
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
   * Fetch 1000-block batches using ``get_block_range``
   * Trial decrypt all outputs
   * Track all nullifiers for owned notes
   * Store wallet state to disk after each batch

2. **Incremental Sync**:
   
   * Resume from last synced height
   * Fetch new blocks since last sync
   * Update balance and transaction history

3. **Periodic Re-sync**:
   
   * Re-scan recent blocks (last 100) for reorgs
   * Verify nullifier status hasn't changed

Example: Cake Wallet Integration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   class PIVXSaplingWallet:
       def __init__(self, electrum_server):
           self.server = electrum_server
           self.activation = 2700500
           self.batch_size = 1000
       
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
               blocks = await self.server.request(
                   'blockchain.sapling.get_block_range',
                   start, end
               )
               
               # Process each block
               for block in blocks:
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
