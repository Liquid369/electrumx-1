PIVX Sapling Spending: Technical Specification
==============================================

**Status**: Design Document
**Author**: Technical Specification for ElectrumX + PIVX Sapling Integration
**Date**: 2026-01-03

Executive Summary
-----------------

This document specifies production-grade approaches for spending PIVX Sapling shielded
funds in an ElectrumX-backed wallet, addressing the critical constraint that ElectrumX
cannot provide witnesses (requires full Sapling tree state).

Recommendation
--------------

**Primary Approach**: **Option 2 - Local Incremental Merkle Tree**

**Rationale**:

1. **Privacy**: No reliance on external full node that can correlate spend attempts
2. **Decentralization**: Maintains light wallet independence from specific full nodes
3. **Reliability**: No dependency on full node availability for spending
4. **UX**: Faster, no need to connect to separate service for spending
5. **Architecture**: Clean separation - ElectrumX for chain data, local tree for proofs

**Tradeoff**: Higher initial bandwidth (sync ~2.7M blocks of commitments) and storage
(tree state ~500MB-1GB), but one-time cost with incremental updates afterward.

**Fallback**: Option 1 (full node RPC) as interim solution or for users unwilling
to maintain local tree.

System Architecture
-------------------

Core Components
~~~~~~~~~~~~~~~

::

    ┌─────────────────────────────────────────────────────────────┐
    │                      Wallet Application                      │
    │  ┌────────────────┐  ┌──────────────┐  ┌─────────────────┐ │
    │  │ Note Manager   │  │ Tree Manager │  │  Spend Builder  │ │
    │  │ - Trial decrypt│  │ - Incremental│  │  - Note select  │ │
    │  │ - Balance calc │  │ - Witness gen│  │  - Proof gen    │ │
    │  │ - Nullifier trk│  │ - Checkpoint │  │  - Tx construct │ │
    │  └────────────────┘  └──────────────┘  └─────────────────┘ │
    │         │                    │                   │           │
    │         └────────────────────┴───────────────────┘           │
    │                              │                               │
    │                              ▼                               │
    │                    ┌──────────────────┐                     │
    │                    │  Local Storage   │                     │
    │                    │  - Notes DB      │                     │
    │                    │  - Tree State    │                     │
    │                    │  - Checkpoints   │                     │
    │                    └──────────────────┘                     │
    └──────────────────────────────┬──────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
                    ▼                             ▼
         ┌────────────────────┐      ┌─────────────────────┐
         │   ElectrumX Server │      │  PIVX Core (Optional)│
         │   - Block data     │      │  - Tx broadcast only │
         │   - Commitments    │      │  - Block data backup │
         │   - Nullifiers     │      │                      │
         │   - Balance check  │      │                      │
         └────────────────────┘      └─────────────────────┘

Data Flow: Receiving → Spending
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Receiving Path** (Already Implemented)::

    ElectrumX → get_block_range → Trial Decrypt → Store Notes → Update Balance

**Spending Path** (This Spec)::

    Select Notes → Get Witnesses (Local Tree) → Generate Proof → 
    Build Transaction → Sign → Broadcast (ElectrumX) → Confirm

Wallet Spend Flow (Step-by-Step)
---------------------------------

Complete flow from user initiating spend to confirmation.

Phase 1: Note Selection
~~~~~~~~~~~~~~~~~~~~~~~

Input: ``{recipient_address, amount, memo}``

1. Query local note database for unspent notes::

    SELECT * FROM sapling_notes 
    WHERE spent = FALSE 
    AND confirmations >= MIN_CONFIRMATIONS
    ORDER BY value DESC

2. Select notes totaling >= amount + estimated_fee::

    selected_notes = []
    total = 0
    for note in unspent_notes:
        if note.nullifier in recent_mempool_nullifiers:
            continue  # Skip if possibly spent
        selected_notes.append(note)
        total += note.value
        if total >= amount + fee:
            break

3. Verify anchor validity::

    # All selected notes must use same anchor
    # Anchor must be recent (within last N blocks)
    anchor_height = max(note.height for note in selected_notes)
    current_height = await electrum.get_height()
    
    if current_height - anchor_height > MAX_ANCHOR_AGE:
        raise AnchorTooOldError()

Phase 2: Witness Generation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For each selected note:

1. Retrieve note's commitment position in tree::

    position = note.tree_position  # Stored when note received
    commitment = note.commitment

2. Generate witness from local incremental tree::

    # Tree manager maintains current tree state
    witness = tree_manager.get_witness(
        commitment=commitment,
        position=position,
        anchor_height=anchor_height
    )
    
    # Witness contains:
    # - Merkle path (32 hashes, ~1KB)
    # - Position in tree
    # - Root hash (anchor)

3. Validate witness::

    # Verify witness root matches expected anchor
    computed_root = compute_merkle_root(
        commitment, position, witness.path
    )
    assert computed_root == anchor

Phase 3: Proof Generation
~~~~~~~~~~~~~~~~~~~~~~~~~~

For each spend (input note):

1. Prepare spend parameters::

    spend_params = {
        'value': note.value,
        'rcm': note.randomness,  # From trial decryption
        'diversifier': note.diversifier,
        'spending_key': derive_spending_key(note.diversifier),
        'witness': witness,
        'anchor': anchor,
        'alpha': random_scalar(),  # Randomness for this spend
    }

2. Generate zk-SNARK proof using Sapling parameters::

    # Uses librustzcash or equivalent
    proof = sapling_spend_proof(
        proving_key=SAPLING_SPEND_VK,
        **spend_params
    )
    
    # Proof output:
    # - cv (value commitment)
    # - rk (randomized verification key)  
    # - zkproof (spend proof, 192 bytes)
    # - spend_auth_sig (signature, generated in phase 4)

3. Compute nullifier::

    nullifier = compute_nullifier(
        note.commitment,
        position,
        spending_key
    )

Phase 4: Output Note Generation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For recipient output and change:

1. Generate output note::

    output_note = {
        'recipient': recipient_payment_address,
        'value': amount,
        'memo': memo,
        'rcm': random_scalar(),  # Output randomness
        'diversifier': recipient.diversifier,
        'pk_d': recipient.pk_d,
    }

2. Generate output proof::

    output_proof = sapling_output_proof(
        proving_key=SAPLING_OUTPUT_VK,
        value=output_note.value,
        rcm=output_note.rcm,
        esk=random_scalar(),  # Ephemeral key randomness
        payment_address=output_note.recipient
    )
    
    # Output:
    # - cv (value commitment)
    # - cmu (note commitment)
    # - ephemeral_key
    # - enc_ciphertext (encrypted note for recipient)
    # - out_ciphertext (recovery data)
    # - zkproof (output proof, 192 bytes)

3. Generate change note if needed::

    change_value = sum(note.value for note in selected_notes) - amount - fee
    
    if change_value > 0:
        change_note = generate_output_note(
            recipient=own_payment_address,
            value=change_value,
            memo="Change"
        )

Phase 5: Transaction Construction
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. Build Sapling transaction structure::

    tx = Transaction()
    
    # Transparent inputs (if any, for fees)
    # Usually none for pure shielded spend
    
    # Sapling spends (inputs)
    for note, proof, nullifier in zip(selected_notes, spend_proofs, nullifiers):
        tx.add_sapling_spend(
            cv=proof.cv,
            anchor=anchor,
            nullifier=nullifier,
            rk=proof.rk,
            zkproof=proof.zkproof,
            spend_auth_sig=None  # Generated in phase 6
        )
    
    # Sapling outputs
    for output in [recipient_output, change_output]:
        tx.add_sapling_output(
            cv=output.cv,
            cmu=output.cmu,
            ephemeral_key=output.epk,
            enc_ciphertext=output.enc_ciphertext,
            out_ciphertext=output.out_ciphertext,
            zkproof=output.zkproof
        )
    
    # Binding signature (proves value balance)
    tx.binding_sig = None  # Generated in phase 6

2. Compute transaction ID::

    # TXID excludes signatures (like Bitcoin SegWit)
    txid = hash_transaction(tx, exclude_sigs=True)

Phase 6: Signing
~~~~~~~~~~~~~~~~~

1. Generate spend authorization signatures::

    for i, note in enumerate(selected_notes):
        sighash = compute_sighash(tx, i, SIGHASH_ALL)
        spend_auth_sig = sign_with_spend_key(
            message=sighash,
            spending_key=note.spending_key,
            randomness=spend_proofs[i].alpha
        )
        tx.spends[i].spend_auth_sig = spend_auth_sig

2. Generate binding signature::

    # Proves sum(input_values) = sum(output_values) + fee
    binding_sig = generate_binding_signature(
        tx=tx,
        balance_randomness=sum_balance_randomness(
            spend_proofs, output_proofs
        )
    )
    tx.binding_sig = binding_sig

3. Finalize transaction::

    tx.finalize()
    tx_hex = tx.serialize().hex()

Phase 7: Broadcast & Monitoring
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. Broadcast via ElectrumX::

    txid = await electrum.request(
        'blockchain.transaction.broadcast',
        tx_hex
    )

2. Mark notes as pending spent::

    for note in selected_notes:
        db.mark_note_pending_spent(
            note_id=note.id,
            spending_txid=txid,
            nullifier=note.nullifier
        )

3. Monitor for confirmation::

    while True:
        status = await electrum.request(
            'blockchain.transaction.get',
            txid,
            verbose=True
        )
        
        if status.get('confirmations', 0) >= MIN_CONFIRMATIONS:
            # Mark notes as spent
            for note in selected_notes:
                db.mark_note_spent(note.id, txid)
            break
        
        await asyncio.sleep(30)

4. Watch for conflicts (double-spend / reorg)::

    # Monitor nullifiers
    for nullifier in spent_nullifiers:
        conflict = await electrum.request(
            'blockchain.nullifier.get_spend',
            nullifier
        )
        
        if conflict.txid != our_txid:
            # Reorg or conflict detected
            handle_spend_conflict(nullifier, conflict)

Option 1: Full Node Witness/Proof Path
---------------------------------------

Architecture
~~~~~~~~~~~~

Wallet delegates witness generation and potentially proof generation to a
trusted PIVX Core full node.

**Trust Model**: User must trust full node operator with:

- Knowledge of which notes are being spent (timing correlation)
- Potentially spending key material if proof generation is delegated

PIVX Core RPC Analysis
~~~~~~~~~~~~~~~~~~~~~~

**Uncertainty**: PIVX Core RPC interface for Sapling is not fully documented
in this context. The following is based on Zcash RPC patterns and PIVX wallet RPCs.

**Known/Expected RPCs**::

    # Shield/unshield operations (high-level wallet functions)
    shieldsendmany
    z_sendmany
    z_getbalance
    z_gettotalbalance
    z_listreceivedbyaddress
    
    # Lower-level operations (may or may not exist)
    z_getwitness <commitment> <height>  # Unknown if exists
    z_createrawtransaction              # Unknown if exists  
    z_signrawtransaction                # Unknown if exists

**Verification Strategy**::

    # Must grep PIVX Core source to confirm:
    git clone https://github.com/PIVX-Project/PIVX
    cd PIVX/src
    grep -r "z_getwitness" .
    grep -r "sapling.*rpc" wallet/
    grep -r "CRPCCommand.*sapling" .

Implementation Path A: High-Level Wallet RPCs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If PIVX Core only exposes high-level wallet functions:

1. **Send spending key or viewing key to node**::

    # Import spending key into node wallet
    pivxd.importprivkey(spending_key)
    
    # Or import viewing key for balance checking
    pivxd.importviewingkey(viewing_key)

2. **Use shieldsendmany**::

    result = pivxd.shieldsendmany(
        from_address=sapling_address,
        amounts=[{
            'address': recipient_address,
            'amount': amount,
            'memo': memo
        }],
        minconf=10,
        fee=fee
    )
    
    # Returns: operation_id (async) or txid

3. **Monitor operation**::

    status = pivxd.z_getoperationstatus([operation_id])
    while status['status'] != 'success':
        await asyncio.sleep(1)
        status = pivxd.z_getoperationstatus([operation_id])
    
    txid = status['result']['txid']

**Issues with this approach**:

- Requires sending spending keys to full node (major privacy/security issue)
- Node wallet manages keys, not application wallet
- No fine-grained control over note selection, fees, change
- Cannot integrate with wallet's key management

Implementation Path B: Low-Level Primitives (If Available)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If PIVX Core exposes low-level primitives:

1. **Get witness from node**::

    witness = pivxd.z_getwitness(
        commitment=note.commitment.hex(),
        anchor_height=anchor_height
    )
    
    # Returns: {
    #   'path': [...],
    #   'position': int,
    #   'anchor': hex
    # }

2. **Generate proof locally** (wallet-side)::

    # Use librustzcash or language bindings
    proof = generate_spend_proof(
        note=note,
        witness=witness,
        spending_key=local_spending_key
    )

3. **Build raw transaction locally**::

    tx = build_sapling_transaction(
        spends=[...],
        outputs=[...],
        binding_sig=...
    )

4. **Broadcast via node or ElectrumX**::

    txid = pivxd.sendrawtransaction(tx.hex())
    # OR
    txid = electrumx.broadcast(tx.hex())

**Advantages**:

- Spending keys never leave wallet
- Fine-grained control
- Can use ElectrumX for broadcast

**Disadvantages**:

- Still leaks note spending patterns to full node
- Node must be trusted to provide correct witnesses
- Malicious node can provide invalid witness (spend fails)

Implementation Path C: Local Proof + Node Witness (Hybrid)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Compromise approach:

1. Wallet syncs commitments from ElectrumX (already implemented)
2. Wallet maintains partial tree (only paths for owned notes)
3. For spending, wallet requests missing tree nodes from full node
4. Wallet generates witnesses and proofs locally
5. Wallet broadcasts via ElectrumX

**Protocol**::

    # Request tree subtree from node
    subtree = pivxd.z_getsubtree(
        start_position=note.position - 1000,
        end_position=note.position + 1000
    )
    
    # Wallet computes witness locally
    witness = wallet.compute_witness(note, subtree)
    
    # Generate proof locally
    proof = wallet.generate_proof(note, witness)
    
    # Build and broadcast
    tx = wallet.build_transaction(proofs, outputs)
    txid = electrumx.broadcast(tx)

**Privacy**: Node learns approximate note positions, but not which specific
notes or amounts.

Recommendation for Option 1
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**If implementing Option 1**:

1. **Phase 1**: High-level wallet RPCs (Path A) as quick PoC, with clear
   warning to users about trust requirements

2. **Phase 2**: Low-level primitives (Path B) if RPCs exist, or contribute
   them to PIVX Core if missing

3. **Long-term**: Migrate to Option 2 (local tree)

**Do NOT deploy Path A to production** without explicit user consent and
understanding of privacy/security implications.

Option 2: Local Incremental Merkle Tree
----------------------------------------

Core Algorithm: Incremental Merkle Tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Sapling uses a Pedersen-hashed Merkle tree:

- Tree depth: 32 levels
- Leaves: note commitments (cmu)
- Internal nodes: Pedersen hash of left + right children
- Root: anchor (used in spend proofs)

**Incremental property**: Can efficiently append leaves and update witnesses
without recomputing entire tree.

Data Structures
~~~~~~~~~~~~~~~

**Tree State**::

    struct SaplingTree {
        // Current leaves
        leaves: Vec<[u8; 32]>,           // All commitments
        
        // Cached subtree roots (for efficiency)
        // Level i contains roots of full subtrees of size 2^i
        cached_roots: HashMap<(u32, u64), [u8; 32]>,
        
        // Current tree size
        size: u64,
        
        // Current root
        root: [u8; 32],
    }

**Witness State**::

    struct SaplingWitness {
        // Merkle path from leaf to root
        path: Vec<[u8; 32]>,  // 32 hashes
        
        // Position of leaf in tree
        position: u64,
        
        // Root hash (anchor)
        root: [u8; 32],
        
        // Height this witness is valid for
        anchor_height: u32,
    }

**Note Storage**::

    struct StoredNote {
        // Note data
        commitment: [u8; 32],
        value: u64,
        diversifier: [u8; 11],
        rcm: [u8; 32],
        memo: String,
        
        // Blockchain position
        txid: [u8; 32],
        output_index: u32,
        height: u32,
        
        // Tree position
        tree_position: u64,
        
        // Spend status
        spent: bool,
        spent_txid: Option<[u8; 32]>,
        spent_height: Option<u32>,
        
        // Witness cache
        witness: Option<SaplingWitness>,
    }

Sync Protocol: Building the Tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Initial Sync** (from Sapling activation):

1. **Fetch commitments in batches**::

    start_height = 2_700_500  # PIVX Sapling activation
    current_height = await electrumx.get_height()
    batch_size = 1000
    
    tree = SaplingTree.new()
    
    for height in range(start_height, current_height, batch_size):
        end = min(height + batch_size - 1, current_height)
        
        # Fetch compact blocks
        blocks = await electrumx.request(
            'blockchain.sapling.get_block_range',
            height, end
        )
        
        for block in blocks:
            for tx in block['txs']:
                for output in tx['outputs']:
                    # Append commitment to tree
                    commitment = bytes.fromhex(output['cmu'])
                    position = tree.append(commitment)
                    
                    # Try to decrypt (check if ours)
                    note = try_decrypt(
                        commitment,
                        output['epk'],
                        output['ciphertext']
                    )
                    
                    if note:
                        # Store our note with tree position
                        db.store_note(
                            note=note,
                            height=block['height'],
                            txid=tx['txid'],
                            tree_position=position
                        )
        
        # Checkpoint every 10k blocks
        if height % 10000 == 0:
            tree.save_checkpoint(height)

**Incremental Sync** (catching up):

1. **Resume from last synced height**::

    last_height = db.get_last_synced_height()
    tree = load_tree_checkpoint(last_height)
    
    # Sync new blocks
    current_height = await electrumx.get_height()
    
    for height in range(last_height + 1, current_height + 1):
        block = await electrumx.request(
            'blockchain.sapling.get_block_range',
            height, height
        )
        
        # Process block
        for tx in block[0]['txs']:
            for output in tx['outputs']:
                commitment = bytes.fromhex(output['cmu'])
                tree.append(commitment)
                
                # Check if ours and store

2. **Update witnesses for existing notes**::

    # After adding new commitments, update witnesses
    for note in db.get_unspent_notes():
        # Witness update is O(log n) with incremental tree
        note.witness = tree.get_witness(
            note.tree_position,
            height
        )
        db.update_note_witness(note)

Witness Generation Algorithm
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Computing witness for a note at position P**::

    def get_witness(tree, position, anchor_height):
        # Get commitment at position
        commitment = tree.leaves[position]
        
        # Build Merkle path from leaf to root
        path = []
        current_position = position
        
        for level in range(32):  # Tree depth
            # Determine if we're left or right child
            is_right = current_position % 2 == 1
            sibling_position = current_position - 1 if is_right else current_position + 1
            
            # Get sibling hash
            if sibling_position < tree.size:
                sibling = compute_node_hash(tree, sibling_position, level)
            else:
                sibling = EMPTY_ROOT[level]  # Default empty node
            
            path.append(sibling)
            current_position //= 2
        
        # Compute root
        root = commitment
        for i, sibling in enumerate(path):
            if position & (1 << i):
                # We're right child
                root = pedersen_hash(sibling, root)
            else:
                # We're left child  
                root = pedersen_hash(root, sibling)
        
        return SaplingWitness(
            path=path,
            position=position,
            root=root,
            anchor_height=anchor_height
        )

**Optimization**: Cache intermediate nodes::

    # When appending commitment, cache every 2^k root
    def append(tree, commitment):
        position = tree.size
        tree.leaves.append(commitment)
        
        # Update cached roots
        current = commitment
        for level in range(32):
            if position & (1 << level):
                # Complete a subtree at this level
                left = tree.cached_roots.get((level, position - (1 << level)))
                tree.cached_roots[(level, position)] = pedersen_hash(left, current)
            current = tree.cached_roots.get((level, position // (1 << (level + 1))))
        
        tree.size += 1
        tree.root = current

Storage Optimization: Pruning
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Problem**: Full tree of 10M commitments = 320MB+ just for leaves

**Solution**: Only store commitments around owned notes + checkpoints::

    # Keep full tree only for recent N blocks (e.g., 1000)
    RECENT_BLOCKS = 1000
    
    # For older blocks, keep:
    # - Commitments for owned notes
    # - Cached subtree roots at checkpoints
    # - Anchor roots every K blocks
    
    def prune_tree(tree, current_height):
        prune_before = current_height - RECENT_BLOCKS
        
        for height in range(activation, prune_before):
            if height % CHECKPOINT_INTERVAL != 0:
                # Delete individual commitments
                # Keep only checkpoint roots
                tree.prune_leaves_at_height(height)

**Recovery**: If pruned, re-fetch from ElectrumX::

    def rebuild_witness_after_prune(note):
        # Fetch commitments around note's height
        blocks = await electrumx.request(
            'blockchain.sapling.get_block_range',
            note.height - 100,
            note.height + 100
        )
        
        # Rebuild local tree section
        # Compute witness

Reorg Handling
~~~~~~~~~~~~~~

**Detection**::

    # ElectrumX provides block hashes
    current_hash = await electrumx.request('blockchain.block.header', height)
    stored_hash = db.get_block_hash(height)
    
    if current_hash != stored_hash:
        # Reorg detected
        handle_reorg(height)

**Rollback**::

    def handle_reorg(reorg_height):
        # 1. Rollback tree to checkpoint before reorg
        checkpoint_height = (reorg_height // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
        tree = load_tree_checkpoint(checkpoint_height)
        
        # 2. Replay blocks from checkpoint to current
        current_height = await electrumx.get_height()
        for height in range(checkpoint_height + 1, current_height + 1):
            # Re-sync block
            blocks = await electrumx.request(
                'blockchain.sapling.get_block_range',
                height, height
            )
            for block in blocks:
                for tx in block['txs']:
                    for output in tx['outputs']:
                        tree.append(bytes.fromhex(output['cmu']))
        
        # 3. Update witnesses for all notes
        for note in db.get_all_notes():
            note.witness = tree.get_witness(note.tree_position, current_height)
            db.update_note(note)
        
        # 4. Check if any of our spends were reverted
        for pending_spend in db.get_pending_spends():
            tx_status = await electrumx.get_transaction(pending_spend.txid)
            if not tx_status:
                # Spend reverted, mark notes as unspent
                db.mark_notes_unspent(pending_spend.note_ids)

Checkpoint Format
~~~~~~~~~~~~~~~~~

**On-disk checkpoint** (every 10k blocks)::

    struct TreeCheckpoint {
        height: u32,
        block_hash: [u8; 32],
        tree_size: u64,
        tree_root: [u8; 32],
        
        // Cached subtree roots (for reconstruction)
        // Map: (level, left_position) -> root_hash
        cached_roots: HashMap<(u32, u64), [u8; 32]>,
        
        // Compressed: only store roots at boundaries
        // Can reconstruct full tree by re-scanning from ElectrumX
    }
    
    // File: checkpoints/<height>.checkpoint
    // Size: ~100KB-1MB per checkpoint depending on caching strategy

Performance Analysis
~~~~~~~~~~~~~~~~~~~~

**Initial Sync** (worst case: full chain from activation):

- Blocks to sync: ~2.4M (from 2.7M to 5.1M current)
- Batch size: 1000 blocks
- Requests: ~2,400
- Data per block: ~1KB (compact, no tx bodies)
- Total download: ~2.4GB
- Time estimate: ~1-2 hours on mobile, ~10-20 min on desktop
- Tree computation: O(n log n) for n commitments, ~5-10 min
- Storage: ~500MB-1GB for full tree + checkpoints

**Incremental Sync** (daily, ~1440 blocks):

- Requests: 2 (batches of 1000)
- Data: ~1.4MB
- Time: <10 seconds
- Witness updates: ~1 second per owned note

**Spending** (witness generation for 1 note):

- Computation: O(log n) = 32 iterations
- Time: <100ms
- No network calls required

Mobile Optimizations
~~~~~~~~~~~~~~~~~~~~

For resource-constrained mobile:

1. **Lazy tree building**: Don't build full tree, only for owned notes::

    # Store only positions, fetch surrounding commitments on-demand
    for note in owned_notes:
        # When spending, fetch ±1000 commitments around note
        rebuild_local_path(note)

2. **Cloud checkpoint service**: Pre-computed checkpoints hosted::

    # Download checkpoint instead of re-scanning
    checkpoint = download_checkpoint(height=5_000_000)
    tree = restore_from_checkpoint(checkpoint)

3. **Pruning**: Aggressive pruning, only keep recent 100 blocks full::

    # For spending old notes, re-fetch temporarily

4. **Background sync**: Use background threads/workers::

    # Sync during idle time, not blocking UI

ElectrumX Changes (If Any)
---------------------------

**Already Implemented** (Sufficient for Option 2):

- ``blockchain.sapling.get_block_range``: Provides commitments and nullifiers
- ``blockchain.nullifier.get_spend``: Check nullifier status
- ``blockchain.transaction.broadcast``: Broadcast signed transaction

**No Additional Changes Required** for Option 2.

**Optional Enhancements**:

1. **Tree checkpoint service** (nice-to-have)::

    blockchain.sapling.get_tree_checkpoint(height)
    
    # Returns:
    {
        'height': 5000000,
        'tree_size': 12345678,
        'root': 'abcd...',
        'checkpoint_data': 'compressed_checkpoint'
    }
    
    # Allows fast bootstrap without re-scanning

2. **Batched commitment queries** (optimization)::

    blockchain.sapling.get_commitments_range(start_pos, end_pos)
    
    # Returns commitments by tree position, not block height
    # Useful for reconstructing specific tree section

3. **Anchor validity check** (safety)::

    blockchain.sapling.is_anchor_valid(anchor_hex)
    
    # Returns: {'valid': bool, 'height': int}
    # Validates anchor exists in chain

**Implementation Priority**: Not critical for initial release. Wallet can
work entirely with existing ``get_block_range`` API.

Data Storage & Reorg Handling
------------------------------

Database Schema
~~~~~~~~~~~~~~~

**Notes Table**::

    CREATE TABLE sapling_notes (
        id INTEGER PRIMARY KEY,
        
        -- Note identification
        commitment BLOB(32) NOT NULL UNIQUE,
        nullifier BLOB(32) NOT NULL UNIQUE,
        
        -- Note data (from decryption)
        value INTEGER NOT NULL,
        diversifier BLOB(11) NOT NULL,
        rcm BLOB(32) NOT NULL,  -- Randomness
        memo TEXT,
        
        -- Blockchain position
        txid BLOB(32) NOT NULL,
        output_index INTEGER NOT NULL,
        height INTEGER NOT NULL,
        block_hash BLOB(32) NOT NULL,
        
        -- Tree position
        tree_position INTEGER NOT NULL,
        
        -- Spend status
        spent BOOLEAN DEFAULT FALSE,
        spent_txid BLOB(32),
        spent_height INTEGER,
        nullifier_height INTEGER,  -- When nullifier appeared
        
        -- Cached witness (optional, can regenerate)
        witness_data BLOB,  -- Serialized witness
        witness_height INTEGER,  -- Height witness is valid for
        
        -- Timestamps
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        spent_at TIMESTAMP,
        
        UNIQUE(txid, output_index)
    );
    
    CREATE INDEX idx_notes_spent ON sapling_notes(spent);
    CREATE INDEX idx_notes_height ON sapling_notes(height);
    CREATE INDEX idx_notes_nullifier ON sapling_notes(nullifier);
    CREATE INDEX idx_notes_tree_position ON sapling_notes(tree_position);

**Tree State Table**::

    CREATE TABLE sapling_tree_state (
        height INTEGER PRIMARY KEY,
        block_hash BLOB(32) NOT NULL,
        tree_size INTEGER NOT NULL,
        tree_root BLOB(32) NOT NULL,
        
        -- Serialized tree checkpoint
        checkpoint_data BLOB,
        
        synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

**Nullifier Watch Table**::

    CREATE TABLE watched_nullifiers (
        nullifier BLOB(32) PRIMARY KEY,
        note_id INTEGER REFERENCES sapling_notes(id),
        first_seen_height INTEGER,
        last_checked_height INTEGER,
        spent BOOLEAN DEFAULT FALSE
    );

Reorg Safety Protocol
~~~~~~~~~~~~~~~~~~~~~

**Invariants**:

1. Never mark note as permanently spent until MIN_CONFIRMATIONS
2. Always maintain tree checkpoint before spending notes
3. Check anchor validity before generating proofs
4. Re-check nullifiers after reorg

**Reorg Detection**::

    async def check_for_reorg():
        # Check recent blocks for hash mismatches
        for height in range(current_height - 100, current_height):
            stored_hash = db.get_block_hash(height)
            chain_hash = await electrumx.get_block_hash(height)
            
            if stored_hash != chain_hash:
                return height  # Reorg point
        
        return None

**Reorg Recovery**::

    async def recover_from_reorg(reorg_height):
        # 1. Rollback database state
        db.execute('BEGIN TRANSACTION')
        
        # Unspend notes spent after reorg point
        db.execute('''
            UPDATE sapling_notes 
            SET spent = FALSE, spent_txid = NULL, spent_height = NULL
            WHERE spent_height > ?
        ''', (reorg_height,))
        
        # Delete tree state after reorg
        db.execute('DELETE FROM sapling_tree_state WHERE height > ?', (reorg_height,))
        
        # Delete notes received after reorg (they may not exist anymore)
        db.execute('DELETE FROM sapling_notes WHERE height > ?', (reorg_height,))
        
        db.execute('COMMIT')
        
        # 2. Rebuild tree from checkpoint
        checkpoint_height = (reorg_height // 10000) * 10000
        tree = load_checkpoint(checkpoint_height)
        
        # 3. Re-sync from checkpoint to current
        await sync_tree_incremental(tree, checkpoint_height + 1)
        
        # 4. Re-check all pending spends
        for pending_spend in db.get_pending_spends():
            status = await electrumx.get_transaction(pending_spend.txid)
            if not status or status['confirmations'] < 1:
                # Spend was invalidated
                db.mark_spend_invalid(pending_spend.id)
                notify_user(f"Spend {pending_spend.txid} was invalidated by reorg")

Anchor Invalidation
~~~~~~~~~~~~~~~~~~~

**Problem**: If spending with anchor from block H, and chain reorgs before H,
anchor becomes invalid.

**Solution**::

    def select_anchor_height(notes):
        # Use anchor from oldest note, minus safety margin
        oldest_note_height = min(note.height for note in notes)
        
        # Anchor must be old enough to survive reorgs
        anchor_height = oldest_note_height - ANCHOR_SAFETY_MARGIN
        
        # But not too old (nodes may reject)
        current_height = get_current_height()
        max_age = 100  # Consensus rule
        
        if current_height - anchor_height > max_age:
            raise AnchorTooOldError("Notes too old, anchor invalid")
        
        return anchor_height
    
    ANCHOR_SAFETY_MARGIN = 10  # Use anchor 10 blocks old

**Verification before spend**::

    def verify_anchor_valid(anchor, anchor_height):
        # Get current root at that height
        current_root = get_tree_root_at_height(anchor_height)
        
        if current_root != anchor:
            raise AnchorInvalidError("Anchor changed due to reorg")

Security & Privacy Analysis
---------------------------

Option 1 (Full Node) Security Issues
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Privacy Leaks**:

1. **Note spending patterns**: Full node learns when notes are spent
2. **Amount correlation**: Can correlate request timing with spend amounts
3. **IP address**: Node operator knows requester's IP (use Tor)
4. **Note linkage**: Repeated requests for same notes reveals ownership

**Security Risks**:

1. **Spending key exposure** (if using high-level RPCs): Catastrophic
2. **Malicious witness**: Node provides invalid witness, spend fails, fee lost
3. **DoS**: Node refuses witness requests, preventing spending
4. **Surveillance**: Node operated by adversary logging all requests

**Mitigations**:

- Only use self-hosted full node or highly trusted node
- Use Tor for all node connections
- Never send spending keys to node
- Verify witness validity before generating proof
- Have fallback nodes

Option 2 (Local Tree) Security Issues
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Privacy Leaks**:

1. **ElectrumX request patterns**: Batch sync patterns may leak wallet size
2. **Broadcast timing**: Transaction broadcast timing leaks spending activity

**Security Risks**:

1. **Commitment omission**: ElectrumX omits commitments, tree incomplete, witness invalid
2. **Reorg attacks**: Malicious server feeds fake reorg, invalidates spends
3. **Eclipse attack**: Isolated from honest servers, fed fake chain data

**Mitigations**:

1. **Multiple ElectrumX sources**: Cross-check commitment data from multiple servers::

    commitments_a = await server_a.get_block_range(height, height)
    commitments_b = await server_b.get_block_range(height, height)
    
    if commitments_a != commitments_b:
        raise SecurityError("Servers disagree on commitments")

2. **Checkpoint validation**: Validate tree roots against known checkpoints::

    # Use checkpoints from PIVX community or code
    TRUSTED_CHECKPOINTS = {
        5000000: 'abcd1234...',  # Tree root at height 5M
        5100000: 'ef567890...',
    }
    
    computed_root = tree.get_root_at_height(5000000)
    if computed_root != TRUSTED_CHECKPOINTS[5000000]:
        raise SecurityError("Tree root mismatch")

3. **Full node verification** (optional): Periodically verify against trusted full node::

    # Every N syncs, validate tree root against full node
    node_root = await full_node.get_tree_root(height)
    local_root = tree.root
    
    if node_root != local_root:
        raise SecurityError("Tree diverged from full node")

4. **Batch sync with randomization**: Randomize batch sizes and timing to reduce fingerprinting

Common Risks (Both Options)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Double-spend detection**: Malicious server hides nullifier::

    # Mitigation: Check multiple servers
    for server in [server_a, server_b, server_c]:
        spent = await server.nullifier.get_spend(nullifier)
        if spent:
            raise AlreadySpentError()

**Fee handling**: Transparent fee reveals shielded activity::

    # Option A: Pay fee from shielded pool (requires protocol support)
    # Option B: Use small transparent input for fee (leaks timing)
    # Option C: Overshield to transparent, then spend shielded (2 tx overhead)

**Timing correlation**: Wallet sync followed by spend reveals shielded balance use::

    # Mitigation: Constant background sync, delay spends randomly

Phased Implementation Plan
---------------------------

Phase 0: Prerequisites (Already Complete)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- [x] ElectrumX ``get_block_range`` API
- [x] Commitment and nullifier indexing
- [x] Note trial decryption
- [x] Balance calculation

Phase 1: Local Tree Foundation (4-6 weeks)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Week 1-2: Tree Data Structures**

- Implement incremental Merkle tree (Pedersen hash)
- Implement witness generation algorithm
- Implement tree checkpoint serialization
- Unit tests for tree operations

**Week 3-4: Tree Sync Protocol**

- Implement initial sync from activation height
- Implement incremental sync (resume from checkpoint)
- Implement pruning logic
- Integration tests with ElectrumX

**Week 5-6: Storage Layer**

- Implement note database schema
- Implement tree checkpoint storage
- Implement reorg rollback logic
- Stress tests with large trees (10M+ commitments)

**Deliverable**: Wallet can sync Sapling tree and generate witnesses for owned notes.

Phase 2: Proof Generation Integration (3-4 weeks)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Week 1-2: Cryptographic Primitives**

- Integrate librustzcash or language bindings for:
  - Spend proof generation
  - Output proof generation
  - Binding signature
- Implement parameter loading (spend/output verification keys)
- Unit tests for proof generation

**Week 3: Transaction Building**

- Implement Sapling transaction structure serialization
- Implement note selection algorithm
- Implement change note generation
- Transaction building tests

**Week 4: Signing and Broadcasting**

- Implement spend authorization signatures
- Implement binding signature
- Integrate with ElectrumX broadcast
- End-to-end spend test (testnet)

**Deliverable**: Wallet can generate valid Sapling spend transactions and broadcast them.

Phase 3: Reorg Handling & Edge Cases (2-3 weeks)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Week 1: Reorg Detection**

- Implement block hash monitoring
- Implement tree rollback to checkpoint
- Implement note spend status recovery
- Reorg simulation tests

**Week 2: Anchor Management**

- Implement anchor age validation
- Implement anchor selection strategy
- Implement anchor invalidation detection
- Edge case tests (old notes, expired anchors)

**Week 3: Error Handling**

- Implement spend conflict detection
- Implement retry logic for failed spends
- Implement user notifications
- Error recovery tests

**Deliverable**: Wallet handles reorgs and edge cases safely.

Phase 4: Privacy & Performance (2-3 weeks)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Week 1: Privacy Enhancements**

- Implement multi-server validation
- Implement checkpoint verification
- Implement request timing randomization
- Privacy audit

**Week 2: Performance Optimization**

- Implement tree pruning optimizations
- Implement witness caching
- Implement parallel sync
- Performance benchmarks (mobile, desktop)

**Week 3: Mobile Optimizations**

- Implement lazy tree building
- Implement background sync
- Implement checkpoint download service
- Mobile device testing (iOS, Android)

**Deliverable**: Production-ready wallet with privacy and performance optimizations.

Phase 5: User Experience & Polish (2 weeks)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Week 1: UI/UX**

- Implement spend confirmation flow
- Implement balance updates
- Implement transaction history
- Implement sync progress indicators

**Week 2: Documentation & Testing**

- Write user documentation
- Write developer documentation  
- Conduct security audit
- Beta testing program

**Deliverable**: User-ready wallet application.

Optional: Phase 6: Advanced Features (Ongoing)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- Multi-note spends (privacy pool)
- Payment disclosures (proof of payment)
- Hardware wallet integration
- Multi-sig shielded addresses (if protocol supports)

Total Timeline: **15-20 weeks** for complete production-ready implementation.

Acceptance Tests / Validation Plan
-----------------------------------

Unit Tests
~~~~~~~~~~

**Tree Operations**::

    test_tree_append_single()
    test_tree_append_batch()
    test_tree_root_computation()
    test_witness_generation()
    test_witness_validation()
    test_tree_checkpoint_save_load()
    test_tree_pruning()

**Proof Generation**::

    test_spend_proof_generation()
    test_output_proof_generation()
    test_binding_signature()
    test_spend_auth_signature()

**Transaction Building**::

    test_note_selection()
    test_transaction_serialization()
    test_transaction_signing()
    test_change_calculation()

Integration Tests
~~~~~~~~~~~~~~~~~

**Sync Tests**::

    test_initial_sync_from_activation()
    test_incremental_sync()
    test_sync_resume_after_crash()
    test_sync_with_reorg()

**Spend Tests**::

    test_single_note_spend()
    test_multi_note_spend()
    test_spend_with_change()
    test_spend_max_balance()

**Reorg Tests**::

    test_reorg_detection()
    test_tree_rollback()
    test_spend_invalidation_after_reorg()
    test_note_reappearance_after_reorg()

End-to-End Tests (Testnet)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Test Vectors**:

1. **Receive and Spend**::

    # Setup: Receive shielded funds on testnet
    # Action: Spend to another shielded address
    # Verify: Transaction confirms, balance updates, recipient receives

2. **Multiple Notes**::

    # Setup: Receive 3 separate shielded payments
    # Action: Spend combining 2 notes
    # Verify: Correct note selection, change handling

3. **Anchor Age**::

    # Setup: Receive shielded funds
    # Action: Wait N blocks, then spend
    # Verify: Anchor still valid, or error if expired

4. **Reorg During Spend**::

    # Setup: Submit spend transaction
    # Simulate: Reorg while tx is pending
    # Verify: Wallet detects conflict, updates status

5. **Privacy Test**::

    # Setup: Analyze server logs
    # Verify: No IP correlation with spends
    # Verify: Batch sync patterns don't leak balance

Security Audit Checklist
~~~~~~~~~~~~~~~~~~~~~~~~~

- [ ] No spending keys logged
- [ ] No commitments leaked to unintended servers
- [ ] Tor integration working
- [ ] Multi-server validation active
- [ ] Reorg handling safe (no double-spends)
- [ ] Anchor validation prevents invalid spends
- [ ] Witness verification prevents fake proofs
- [ ] Database encryption at rest
- [ ] Secure random number generation for proofs
- [ ] No timing side channels in proof generation

Performance Benchmarks
~~~~~~~~~~~~~~~~~~~~~~~

**Target Metrics**:

- Initial sync: < 2 hours on mobile, < 30 min on desktop
- Incremental sync: < 30 seconds
- Witness generation: < 1 second per note
- Proof generation: < 10 seconds per spend
- Transaction building: < 5 seconds
- Memory usage: < 500MB on mobile, < 2GB on desktop
- Storage: < 2GB total

**Benchmark Tests**::

    bench_initial_sync_2M_blocks()
    bench_incremental_sync_1000_blocks()
    bench_witness_generation_single_note()
    bench_proof_generation_single_spend()
    bench_transaction_build_3_notes()

Mainnet Deployment Criteria
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Required Before Mainnet Launch**:

1. All unit tests passing
2. All integration tests passing  
3. At least 3 successful testnet end-to-end spends
4. Security audit complete (external if possible)
5. Performance benchmarks meet targets
6. Reorg handling validated in testnet
7. User documentation complete
8. Beta testing with 10+ users

**Launch Checklist**:

- [ ] Code reviewed by 2+ engineers
- [ ] Security audit findings addressed
- [ ] Privacy analysis documented
- [ ] Known limitations documented
- [ ] Emergency shutdown mechanism implemented
- [ ] User support channel established
- [ ] Monitoring and alerting configured

Recommended Reading
-------------------

**Essential Papers**:

- Zcash Sapling Protocol Specification: https://zips.z.cash/protocol/protocol.pdf
- Incremental Merkle Trees: Section 4.8 of Sapling spec
- Bellman Proving System: https://github.com/zkcrypto/bellman

**Implementation References**:

- librustzcash: https://github.com/zcash/librustzcash
- Zcash lightwalletd: https://github.com/zcash/lightwalletd  
- Zcash wallet SDK: https://github.com/zcash/ZcashLightClientKit

**PIVX Specific**:

- PIVX Core source: https://github.com/PIVX-Project/PIVX
  - src/sapling/ - Sapling implementation
  - src/wallet/rpcwallet.cpp - Wallet RPCs
  - src/rpc/blockchain.cpp - Blockchain RPCs

**Verification Steps**::

    # Clone PIVX and verify Sapling RPCs
    git clone https://github.com/PIVX-Project/PIVX
    cd PIVX
    grep -r "sapling" src/rpc/
    grep -r "z_" src/wallet/rpcwallet.cpp | grep -i witness
    grep -r "shield" src/wallet/

Conclusion
----------

**Recommended Path**: Implement **Option 2 (Local Incremental Merkle Tree)**
following the phased plan above.

**Justification**:

- Privacy-preserving (no external witness requests)
- Self-contained (no full node dependency)
- Performant (witness generation <1s)
- Scalable (incremental updates efficient)
- Maintainable (clear separation of concerns)

**Tradeoffs Accepted**:

- Initial sync time (1-2 hours, one-time)
- Storage overhead (~1GB, acceptable for desktop/modern mobile)
- Implementation complexity (mitigated by phased approach)

**Success Criteria**:

Wallet users can spend shielded PIVX funds with:

- < 10 second end-to-end spend time
- No privacy leaks to servers
- Safe reorg handling
- < 2GB storage requirement
- Works on mobile and desktop

This specification provides a complete, production-grade path to Sapling
spending in ElectrumX-backed PIVX wallets.
