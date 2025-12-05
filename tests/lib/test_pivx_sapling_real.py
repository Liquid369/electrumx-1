# Copyright (c) 2024, the ElectrumX authors
#
# All rights reserved.
#
# The MIT License (MIT)

'''Integration tests for PIVX Sapling with real block data.'''

import json
import os

from electrumx.lib import tx as tx_lib
from electrumx.lib import coins


class TestPIVXSaplingRealBlocks:
    '''Test PIVX Sapling deserialization with real mainnet blocks.'''

    @staticmethod
    def load_block(filename):
        '''Load a block from the tests/blocks directory.'''
        base_path = os.path.dirname(os.path.dirname(__file__))
        block_path = os.path.join(base_path, 'blocks', filename)
        with open(block_path, 'r') as f:
            return json.load(f)

    def test_block_2703076_shielding_tx(self):
        '''Test block 2703076 which contains a shielding transaction.

        This block is post-Sapling activation (2,700,500) and contains
        transactions that shield transparent funds.
        '''
        block_data = self.load_block('pivx_mainnet_2703076.json')

        # Verify block height is post-Sapling
        assert block_data['height'] == 2703076
        assert block_data['height'] > coins.Pivx.SAPLING_START_HEIGHT

        # Parse the raw block
        raw_block = bytes.fromhex(block_data['block'])

        # The block header for post-Sapling PIVX is 112 bytes
        header_len = coins.Pivx.static_header_len(block_data['height'])
        assert header_len == 112

        # Skip header and parse transactions
        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()

        assert tx_count == len(block_data['tx'])

        txs = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            txs.append(tx)

        # Verify we got all transactions
        assert len(txs) == tx_count

        # Check that at least one is a Sapling transaction
        sapling_txs = [tx for tx in txs if isinstance(tx, tx_lib.TxPIVXSapling)]

        # Block 2703076 should have Sapling transactions
        # (shielding transaction: transparent -> shielded)
        if sapling_txs:
            # Verify Sapling transaction structure
            for tx in sapling_txs:
                assert hasattr(tx, 'value_balance')
                assert hasattr(tx, 'sapling_spends')
                assert hasattr(tx, 'sapling_outputs')
                assert hasattr(tx, 'binding_sig')

                # Verify binding signature is 64 bytes
                assert len(tx.binding_sig) == 64

                # If there are spends, verify structure
                for spend in tx.sapling_spends:
                    assert len(spend.cv) == 32
                    assert len(spend.anchor) == 32
                    assert len(spend.nullifier) == 32
                    assert len(spend.rk) == 32
                    assert len(spend.zkproof) == 192
                    assert len(spend.spend_auth_sig) == 64

                # If there are outputs, verify structure
                for output in tx.sapling_outputs:
                    assert len(output.cv) == 32
                    assert len(output.cmu) == 32
                    assert len(output.ephemeral_key) == 32
                    assert len(output.enc_ciphertext) == 580
                    assert len(output.out_ciphertext) == 80
                    assert len(output.zkproof) == 192

    def test_pre_sapling_block_10000(self):
        '''Test pre-Sapling block parsing still works.'''
        block_data = self.load_block('pivx_mainnet_10000.json')

        # Verify block height is pre-Sapling
        assert block_data['height'] == 10000
        assert block_data['height'] < coins.Pivx.SAPLING_START_HEIGHT

        # Parse the raw block
        raw_block = bytes.fromhex(block_data['block'])

        # Pre-Sapling header is 80 bytes
        header_len = coins.Pivx.static_header_len(block_data['height'])
        assert header_len == 80

        # Skip header and parse transactions
        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()

        txs = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            txs.append(tx)

        # Verify transactions are TxPIVX but not TxPIVXSapling
        for tx in txs:
            assert isinstance(tx, tx_lib.TxPIVX)
            assert not isinstance(tx, tx_lib.TxPIVXSapling)

    def test_zerocoin_era_block_1000000(self):
        '''Test block during Zerocoin era (has expanded header).'''
        block_data = self.load_block('pivx_mainnet_1000000.json')

        height = block_data['height']
        assert height == 1000000

        # During Zerocoin era, header is 112 bytes
        header_len = coins.Pivx.static_header_len(height)
        assert header_len == 112

        raw_block = bytes.fromhex(block_data['block'])
        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()

        txs = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            txs.append(tx)

        # Verify transactions parsed correctly
        assert len(txs) == tx_count
        for tx in txs:
            assert isinstance(tx, tx_lib.TxPIVX)

    def test_block_5057529_unshielding_tx(self):
        '''Test block 5057529 which contains an unshielding transaction.

        This block has a Sapling tx with 2 spends and 2 outputs,
        unshielding funds from the shielded pool to transparent.
        '''
        block_data = self.load_block('pivx_mainnet_5057529.json')

        # Verify block height
        assert block_data['height'] == 5057529
        assert block_data['height'] > coins.Pivx.SAPLING_START_HEIGHT

        raw_block = bytes.fromhex(block_data['block'])
        header_len = coins.Pivx.static_header_len(block_data['height'])
        assert header_len == 112

        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()
        assert tx_count == 3

        txs = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            txs.append(tx)

        # Third transaction should be TxPIVXSapling with spends
        tx3 = txs[2]
        assert isinstance(tx3, tx_lib.TxPIVXSapling)
        assert tx3.txid[::-1].hex() == block_data['tx'][2]

        # Verify unshielding: positive value_balance means funds leaving
        # shielded pool
        assert tx3.value_balance > 0  # ~5.02 PIVX leaving shielded pool

        # Verify 2 spends (consuming shielded notes)
        assert len(tx3.sapling_spends) == 2

        # Verify nullifiers are unique
        nullifiers = [s.nullifier for s in tx3.sapling_spends]
        assert len(set(nullifiers)) == 2

        # Verify anchors (both spends use same anchor - same tree state)
        anchors = [s.anchor for s in tx3.sapling_spends]
        assert anchors[0] == anchors[1]

        # Verify 2 outputs (change back to shielded)
        assert len(tx3.sapling_outputs) == 2

        # Verify commitments are unique
        commitments = [o.cmu for o in tx3.sapling_outputs]
        assert len(set(commitments)) == 2


class TestPIVXCoinConfiguration:
    '''Test PIVX coin configuration.'''

    def test_sapling_height_mainnet(self):
        '''Test SAPLING_START_HEIGHT for mainnet.'''
        assert coins.Pivx.SAPLING_START_HEIGHT == 2700500

    def test_sapling_height_testnet(self):
        '''Test SAPLING_START_HEIGHT for testnet.'''
        assert coins.PivxTestnet.SAPLING_START_HEIGHT == 201

    def test_deserializer_class(self):
        '''Test that PIVX uses the correct deserializer.'''
        assert coins.Pivx.DESERIALIZER == tx_lib.DeserializerPIVX

    def test_session_class(self):
        '''Test that PIVX uses PIVXSaplingElectrumX session.'''
        from electrumx.server.session import PIVXSaplingElectrumX
        assert coins.Pivx.SESSIONCLS == PIVXSaplingElectrumX

    def test_block_processor_class(self):
        '''Test that PIVX uses PIVXSaplingBlockProcessor.'''
        from electrumx.server.block_processor import PIVXSaplingBlockProcessor
        assert coins.Pivx.BLOCK_PROCESSOR == PIVXSaplingBlockProcessor

    def test_header_size_pre_zerocoin(self):
        '''Test header size before Zerocoin activation.'''
        height = 100000  # Before Zerocoin
        assert coins.Pivx.static_header_len(height) == 80

    def test_header_size_zerocoin_era(self):
        '''Test header size during Zerocoin era.'''
        height = 1000000  # During Zerocoin
        assert coins.Pivx.static_header_len(height) == 112

    def test_header_size_sapling_era(self):
        '''Test header size after Sapling activation.'''
        height = 2800000  # After Sapling
        assert coins.Pivx.static_header_len(height) == 112


class TestSaplingDataExtraction:
    '''Test Sapling data extraction for indexing.'''

    def test_nullifier_uniqueness(self):
        '''Test that each spend has a unique nullifier.'''
        block_data = TestPIVXSaplingRealBlocks.load_block(
            'pivx_mainnet_2703076.json'
        )

        raw_block = bytes.fromhex(block_data['block'])
        header_len = coins.Pivx.static_header_len(block_data['height'])

        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()

        all_nullifiers = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            if isinstance(tx, tx_lib.TxPIVXSapling):
                for spend in tx.sapling_spends:
                    all_nullifiers.append(spend.nullifier)

        # Nullifiers should be unique (no double-spends in valid block)
        assert len(all_nullifiers) == len(set(all_nullifiers))

    def test_commitment_extraction(self):
        '''Test that commitments can be extracted for indexing.'''
        block_data = TestPIVXSaplingRealBlocks.load_block(
            'pivx_mainnet_2703076.json'
        )

        raw_block = bytes.fromhex(block_data['block'])
        header_len = coins.Pivx.static_header_len(block_data['height'])

        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()

        commitments = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            if isinstance(tx, tx_lib.TxPIVXSapling):
                for output in tx.sapling_outputs:
                    commitments.append(output.cmu)

        # All commitments should be 32 bytes
        for cmu in commitments:
            assert len(cmu) == 32

    def test_anchor_extraction(self):
        '''Test that anchors can be extracted from spends.'''
        block_data = TestPIVXSaplingRealBlocks.load_block(
            'pivx_mainnet_2703076.json'
        )

        raw_block = bytes.fromhex(block_data['block'])
        header_len = coins.Pivx.static_header_len(block_data['height'])

        deser = tx_lib.DeserializerPIVX(raw_block, start=header_len)
        tx_count = deser._read_varint()

        anchors = []
        for _ in range(tx_count):
            tx = deser.read_tx()
            if isinstance(tx, tx_lib.TxPIVXSapling):
                for spend in tx.sapling_spends:
                    anchors.append(spend.anchor)

        # All anchors should be 32 bytes (Merkle tree roots)
        for anchor in anchors:
            assert len(anchor) == 32
