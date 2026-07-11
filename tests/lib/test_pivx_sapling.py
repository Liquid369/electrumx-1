# Copyright (c) 2024, the ElectrumX authors
#
# All rights reserved.
#
# The MIT License (MIT)

'''Unit tests for PIVX Sapling deserializer.'''

import struct

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
        assert deser.cursor == len(raw)


def create_sapling_tx_hex(
    num_spends=0,
    num_outputs=0,
    value_balance=0,
    tx_type=0,
    sap_data_present=True,
    extra_payload=None,
):
    '''Create a synthetic v3 PIVX transaction.

    Layout after nLockTime:
      Optional<SaplingTxData>: 1 presence byte, then when present
        valueBalance (int64) + vShieldedSpend + vShieldedOutput +
        bindingSig(64) only if spends+outputs non-empty.
      For tx_type != 0, Optional<vector<u8>> extraPayload: 1 presence
        byte + compactsize + data.
    '''
    # header uint32: low 16 bits version, high 16 bits type
    parts = [struct.pack('<HH', 3, tx_type).hex()]
    parts.append("00")  # no transparent inputs
    parts.append("00")  # no transparent outputs
    parts.append("00000000")  # locktime

    if sap_data_present:
        parts.append("01")  # SaplingTxData present
        parts.append(struct.pack('<q', value_balance).hex())
        assert num_spends < 253 and num_outputs < 253
        parts.append(format(num_spends, '02x'))
        parts.append("00" * 384 * num_spends)
        parts.append(format(num_outputs, '02x'))
        parts.append("00" * 948 * num_outputs)
        if num_spends or num_outputs:
            parts.append("00" * 64)  # bindingSig
    else:
        parts.append("00")  # SaplingTxData absent

    if tx_type:
        if extra_payload is None:
            parts.append("00")  # extraPayload absent
        else:
            assert len(extra_payload) < 253
            parts.append("01")
            parts.append(format(len(extra_payload), '02x'))
            parts.append(extra_payload.hex())

    return ''.join(parts)


class TestDeserializerPIVXSapling:
    '''Tests for Sapling PIVX transaction parsing with synthetic data.'''

    create_sapling_tx_hex = staticmethod(create_sapling_tx_hex)

    def test_empty_sapling_tx(self):
        '''Present SaplingTxData with empty vectors: no bindingSig.'''
        tx_hex = create_sapling_tx_hex()
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        # No shielded data, so should be TxPIVX not TxPIVXSapling
        assert isinstance(tx, tx_lib.TxPIVX)
        assert not isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        # Empty shielded vectors mean no binding signature is serialized
        assert deser.cursor == len(raw)

    def test_absent_sapling_data(self):
        '''Presence byte 0: no SaplingTxData payload follows.'''
        tx_hex = create_sapling_tx_hex(sap_data_present=False)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVX)
        assert not isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert deser.cursor == len(raw)

    def test_sapling_tx_with_spends(self):
        '''Test Sapling transaction with spends.'''
        tx_hex = create_sapling_tx_hex(num_spends=2)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert len(tx.sapling_spends) == 2
        assert len(tx.sapling_outputs) == 0
        assert len(tx.binding_sig) == 64
        assert deser.cursor == len(raw)

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
        tx_hex = create_sapling_tx_hex(num_outputs=3)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert len(tx.sapling_spends) == 0
        assert len(tx.sapling_outputs) == 3
        assert deser.cursor == len(raw)

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
        tx_hex = create_sapling_tx_hex(
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
        assert deser.cursor == len(raw)

    def test_sapling_nullifier_extraction(self):
        '''Test that nullifiers are correctly extracted.'''
        # Create tx with specific nullifier pattern
        tx_hex = create_sapling_tx_hex(num_spends=1)
        raw = bytearray.fromhex(tx_hex)

        # Position of nullifier in spend (after cv:32, anchor:32 = 64 bytes
        # from start of spend data)
        # Spend data starts after: header:4 + inputs:1 + outputs:1 +
        # locktime:4 + sapDataPresence:1 + valueBalance:8 + spendCount:1
        # = 20 bytes
        # Plus cv:32 + anchor:32 = 84 bytes from start
        nullifier_start = 20 + 32 + 32
        test_nullifier = bytes(range(32))
        raw[nullifier_start:nullifier_start + 32] = test_nullifier

        deser = tx_lib.DeserializerPIVX(bytes(raw))
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.sapling_spends[0].nullifier == test_nullifier

    def test_sapling_commitment_extraction(self):
        '''Test that commitments (cmu) are correctly extracted.'''
        tx_hex = create_sapling_tx_hex(num_outputs=1)
        raw = bytearray.fromhex(tx_hex)

        # Position of cmu in output (after cv:32 = 32 bytes from start of
        # output data)
        # Output data starts after: header:4 + inputs:1 + outputs:1 +
        # locktime:4 + sapDataPresence:1 + valueBalance:8 + spendCount:1 +
        # outputCount:1 = 21 bytes
        # Plus cv:32 = 53 bytes from start
        cmu_start = 21 + 32
        test_cmu = bytes(range(32))
        raw[cmu_start:cmu_start + 32] = test_cmu

        deser = tx_lib.DeserializerPIVX(bytes(raw))
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.sapling_outputs[0].cmu == test_cmu


class TestDeserializerPIVXSpecialTx:
    '''Tests for v3 special (DIP2-style) txs with optional trailing data.'''

    def test_special_tx_with_empty_sapling_and_extra_payload(self):
        '''nVersion=3, nType=6, sapData present with empty vectors (so no
        bindingSig), followed by an extraPayload blob.'''
        payload = bytes(range(80))
        tx_hex = create_sapling_tx_hex(tx_type=6, extra_payload=payload)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVX)
        assert not isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.version == 3
        assert tx.txtype == 6
        # The full buffer, including extraPayload, must be consumed
        assert deser.cursor == len(raw)

    def test_special_tx_absent_optionals(self):
        '''Both optionals absent: presence bytes are 0.'''
        tx_hex = create_sapling_tx_hex(tx_type=6, sap_data_present=False)
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVX)
        assert tx.version == 3
        assert tx.txtype == 6
        assert deser.cursor == len(raw)

    def test_two_consecutive_special_txs_stay_aligned(self):
        '''A misread optional would desynchronise the cursor and corrupt
        the second tx; both must parse and consume the exact buffer.'''
        first_hex = create_sapling_tx_hex(
            tx_type=6, extra_payload=bytes(range(80)))
        second_hex = create_sapling_tx_hex(
            tx_type=6, extra_payload=bytes(range(80, 160)))
        raw = bytes.fromhex(first_hex + second_hex)

        deser = tx_lib.DeserializerPIVX(raw)
        first = deser.read_tx()
        boundary = deser.cursor
        second = deser.read_tx()

        assert boundary == len(bytes.fromhex(first_hex))
        assert deser.cursor == len(raw)
        assert first.txtype == second.txtype == 6
        assert first.txid != second.txid
        # txids cover each tx's exact byte range
        assert first.txid == tx_lib.double_sha256(raw[:boundary])
        assert second.txid == tx_lib.double_sha256(raw[boundary:])

    def test_special_tx_with_shielded_data_and_extra_payload(self):
        '''Shielded vectors and extraPayload can coexist on a special tx.'''
        tx_hex = create_sapling_tx_hex(
            num_spends=1, num_outputs=1, tx_type=6,
            extra_payload=bytes(range(40)))
        raw = bytes.fromhex(tx_hex)
        deser = tx_lib.DeserializerPIVX(raw)
        tx = deser.read_tx()

        assert isinstance(tx, tx_lib.TxPIVXSapling)
        assert tx.txtype == 6
        assert len(tx.sapling_spends) == 1
        assert len(tx.sapling_outputs) == 1
        assert len(tx.binding_sig) == 64
        assert deser.cursor == len(raw)
