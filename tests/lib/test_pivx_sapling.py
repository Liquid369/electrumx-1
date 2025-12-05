# Copyright (c) 2024, the ElectrumX authors
#
# All rights reserved.
#
# The MIT License (MIT)

'''Unit tests for PIVX Sapling deserializer.'''

from electrumx.lib import tx as tx_lib


class TestSaplingSpend:
    '''Tests for SaplingSpend dataclass.'''

    def test_creation(self):
        spend = tx_lib.SaplingSpend(
            cv=bytes(32),
            anchor=bytes(32),
            nullifier=bytes(32),
            rk=bytes(32),
            zkproof=bytes(192),
            spend_auth_sig=bytes(64),
        )
        assert len(spend.cv) == 32
        assert len(spend.anchor) == 32
        assert len(spend.nullifier) == 32
        assert len(spend.rk) == 32
        assert len(spend.zkproof) == 192
        assert len(spend.spend_auth_sig) == 64


class TestSaplingOutput:
    '''Tests for SaplingOutput dataclass.'''

    def test_creation(self):
        output = tx_lib.SaplingOutput(
            cv=bytes(32),
            cmu=bytes(32),
            ephemeral_key=bytes(32),
            enc_ciphertext=bytes(580),
            out_ciphertext=bytes(80),
            zkproof=bytes(192),
        )
        assert len(output.cv) == 32
        assert len(output.cmu) == 32
        assert len(output.ephemeral_key) == 32
        assert len(output.enc_ciphertext) == 580
        assert len(output.out_ciphertext) == 80
        assert len(output.zkproof) == 192


class TestTxPIVXSapling:
    '''Tests for TxPIVXSapling dataclass.'''

    def test_creation(self):
        tx = tx_lib.TxPIVXSapling(
            version=3,
            txtype=0,
            inputs=[],
            outputs=[],
            locktime=0,
            txid=bytes(32),
            wtxid=bytes(32),
            value_balance=0,
            sapling_spends=[],
            sapling_outputs=[],
            binding_sig=bytes(64),
        )
        assert tx.version == 3
        assert tx.value_balance == 0
        assert len(tx.sapling_spends) == 0
        assert len(tx.sapling_outputs) == 0

    def test_with_shielded_data(self):
        spend = tx_lib.SaplingSpend(
            cv=bytes(32),
            anchor=bytes(32),
            nullifier=b'\x01' * 32,
            rk=bytes(32),
            zkproof=bytes(192),
            spend_auth_sig=bytes(64),
        )
        output = tx_lib.SaplingOutput(
            cv=bytes(32),
            cmu=b'\x02' * 32,
            ephemeral_key=bytes(32),
            enc_ciphertext=bytes(580),
            out_ciphertext=bytes(80),
            zkproof=bytes(192),
        )
        tx = tx_lib.TxPIVXSapling(
            version=3,
            txtype=0,
            inputs=[],
            outputs=[],
            locktime=0,
            txid=bytes(32),
            wtxid=bytes(32),
            value_balance=1000000,
            sapling_spends=[spend],
            sapling_outputs=[output],
            binding_sig=bytes(64),
        )
        assert len(tx.sapling_spends) == 1
        assert len(tx.sapling_outputs) == 1
        assert tx.sapling_spends[0].nullifier == b'\x01' * 32
        assert tx.sapling_outputs[0].cmu == b'\x02' * 32


class TestDeserializerPIVXSizes:
    '''Tests for PIVX Sapling deserializer constants.'''

    def test_spend_size(self):
        # 32 + 32 + 32 + 32 + 192 + 64 = 384
        assert tx_lib.DeserializerPIVX.SAPLING_SPEND_SIZE == 384

    def test_output_size(self):
        # 32 + 32 + 32 + 580 + 80 + 192 = 948
        assert tx_lib.DeserializerPIVX.SAPLING_OUTPUT_SIZE == 948


class TestDeserializerPIVXPreSapling:
    '''Tests for pre-Sapling PIVX transaction parsing.'''

    # A simple pre-Sapling PIVX transaction (version 1)
    # Format: version(4) | nin(varint) | inputs | nout(varint) | outputs | locktime(4)
    PRE_SAPLING_TX = (
        "01000000"  # version = 1 (4 bytes little-endian)
        "01"  # input count (varint)
        "0000000000000000000000000000000000000000000000000000000000000000"  # prev_hash
        "ffffffff"  # prev_idx (coinbase = 0xffffffff)
        "05"  # script length
        "0102030405"  # script
        "ffffffff"  # sequence
        "01"  # output count (varint)
        "0100000000000000"  # value = 1 satoshi (8 bytes)
        "01"  # script length
        "00"  # script (OP_FALSE)
        "00000000"  # locktime (4 bytes)
    )

    def test_pre_sapling_tx(self):
        raw = bytes.fromhex(self.PRE_SAPLING_TX)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        # Should return TxPIVX not TxPIVXSapling
        assert isinstance(tx, tx_lib.TxPIVX)
        assert not isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 1
        assert tx.txtype == 0
        assert len(tx.inputs) == 1
        assert len(tx.outputs) == 1


class TestDeserializerPIVXSapling:
    '''Tests for Sapling PIVX transaction parsing with synthetic data.'''

    @staticmethod
    def create_sapling_tx_hex(
        num_spends=0,
        num_outputs=0,
        value_balance=0,
    ):
        '''Create a synthetic Sapling transaction for testing.'''
        # Version 3 (Sapling)
        header = "03000000"  # version 3, type 0

        # Empty inputs
        inputs = "00"

        # Empty transparent outputs
        outputs = "00"

        # Locktime
        locktime = "00000000"

        # Expiry height (varint)
        expiry_height = "00"

        # Value balance (8 bytes, little endian)
        import struct
        val_bal = struct.pack('<q', value_balance).hex()

        # Sapling spends count
        spend_count = format(num_spends, '02x') if num_spends < 253 else None
        if spend_count is None:
            raise ValueError("Too many spends for simple test")

        # Sapling spend data (384 bytes each)
        spend_data = "00" * 384 * num_spends

        # Sapling outputs count
        output_count = format(num_outputs, '02x') if num_outputs < 253 else None
        if output_count is None:
            raise ValueError("Too many outputs for simple test")

        # Sapling output data (948 bytes each)
        output_data = "00" * 948 * num_outputs

        # Binding signature (64 bytes)
        binding_sig = "00" * 64

        return (header + inputs + outputs + locktime + expiry_height +
                val_bal + spend_count + spend_data + output_count +
                output_data + binding_sig)

    def test_empty_sapling_tx(self):
        '''Test Sapling transaction with no shielded data.'''
        tx_hex = self.create_sapling_tx_hex()
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        # No shielded data, so should be TxPIVX not TxPIVXSapling
        assert isinstance(tx, tx_lib.TxPIVX)
        assert not isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3

    def test_sapling_tx_with_spends(self):
        '''Test Sapling transaction with spends.'''
        tx_hex = self.create_sapling_tx_hex(num_spends=2)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert len(tx.sapling_spends) == 2
        assert len(tx.sapling_outputs) == 0

        # Verify spend structure
        for spend in tx.sapling_spends:
            assert len(spend.cv) == 32
            assert len(spend.anchor) == 32
            assert len(spend.nullifier) == 32
            assert len(spend.rk) == 32
            assert len(spend.zkproof) == 192
            assert len(spend.spend_auth_sig) == 64

    def test_sapling_tx_with_outputs(self):
        '''Test Sapling transaction with outputs.'''
        tx_hex = self.create_sapling_tx_hex(num_outputs=3)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert len(tx.sapling_spends) == 0
        assert len(tx.sapling_outputs) == 3

        # Verify output structure
        for output in tx.sapling_outputs:
            assert len(output.cv) == 32
            assert len(output.cmu) == 32
            assert len(output.ephemeral_key) == 32
            assert len(output.enc_ciphertext) == 580
            assert len(output.out_ciphertext) == 80
            assert len(output.zkproof) == 192

    def test_sapling_tx_with_both(self):
        '''Test Sapling transaction with both spends and outputs.'''
        tx_hex = self.create_sapling_tx_hex(
            num_spends=1,
            num_outputs=2,
            value_balance=500000,
        )
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert tx.value_balance == 500000
        assert len(tx.sapling_spends) == 1
        assert len(tx.sapling_outputs) == 2

    def test_sapling_nullifier_extraction(self):
        '''Test that nullifiers are correctly extracted.'''
        # Create tx with specific nullifier pattern
        tx_hex = self.create_sapling_tx_hex(num_spends=1)
        raw = bytearray.fromhex(tx_hex)

        # Position of nullifier in spend (after cv:32, anchor:32 = 64 bytes
        # from start of spend data)
        # Spend data starts after: header:4 + inputs:1 + outputs:1 +
        # locktime:4 + expiry:1 + valueBalance:8 + spendCount:1 = 20 bytes
        # Plus cv:32 + anchor:32 = 84 bytes from start
        nullifier_start = 20 + 32 + 32
        test_nullifier = bytes(range(32))
        raw[nullifier_start:nullifier_start+32] = test_nullifier

        deser = tx_lib.DeserializerPIVX(bytes(raw))
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.sapling_spends[0].nullifier == test_nullifier

    def test_sapling_commitment_extraction(self):
        '''Test that commitments (cmu) are correctly extracted.'''
        tx_hex = self.create_sapling_tx_hex(num_outputs=1)
        raw = bytearray.fromhex(tx_hex)

        # Position of cmu in output (after cv:32 = 32 bytes from start of
        # output data)
        # Output data starts after: header:4 + inputs:1 + outputs:1 +
        # locktime:4 + expiry:1 + valueBalance:8 + spendCount:1 +
        # outputCount:1 = 21 bytes
        # Plus cv:32 = 53 bytes from start
        cmu_start = 21 + 32
        test_cmu = bytes(range(32))
        raw[cmu_start:cmu_start+32] = test_cmu

        deser = tx_lib.DeserializerPIVX(bytes(raw))
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.sapling_outputs[0].cmu == test_cmu
