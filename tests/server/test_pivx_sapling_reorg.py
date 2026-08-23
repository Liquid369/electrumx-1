'''Tests for the PIVX Sapling index: DB layer, block processor, and the
PIVXSaplingElectrumX session RPC surface.

Conventions under test:
- the index stores all 32-byte values in raw little-endian serialization
  order; the RPC boundary converts to/from PIVX Core display byte order
- commitment positions are explicit, assigned by the block processor in
  canonical order
- consensus anchors are finalsaplingroot values from block headers
  (bytes 80:112), recorded first-seen with the tree size at that height
- witness/anchor responses are validated against those consensus
  anchors and fail closed on mismatch
'''

import ast
import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from aiorpcx import ReplyAndDisconnect, Request, RPCError

from electrumx.lib.coins import Pivx, PivxTestnet
from electrumx.lib import tx as tx_lib
from electrumx.lib.hash import hash_to_hex_str, hex_str_to_hash
from electrumx.server.daemon import DaemonError
from electrumx.server.block_processor import (
    BlockProcessor, PIVXSaplingBlockProcessor)
from electrumx.server.db import DB, FlushData
from electrumx.server.session import (
    PIVXSaplingElectrumX,
    PIVX_SAPLING_MAX_BLOCK_RANGE,
    PIVX_SAPLING_RPC_CONTRACT,
    PIVX_SAPLING_WITNESS_HELPER_ENV,
)


def display(raw: bytes) -> str:
    '''PIVX Core display byte order (uint256 GetHex) of raw LE bytes.'''
    return raw[::-1].hex()


def asym32(*prefix) -> bytes:
    '''A 32-byte value that is not palindromic, so a missing or double
    byte-order reversal cannot go unnoticed.'''
    prefix = bytes(prefix)
    return (prefix + bytes(range(32)))[:32]


class FakeKV:

    def __init__(self):
        self.data = {}
        self.for_sync = False

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)

    def iterator(self, prefix=b'', reverse=False):
        items = [(key, value) for key, value in self.data.items()
                 if key.startswith(prefix)]
        return iter(sorted(items, reverse=reverse))

    def write_batch(self):
        # put/delete already match the batch interface
        return contextlib.nullcontext(self)


class BufferingFakeKV(FakeKV):
    '''FakeKV whose write_batch buffers writes and applies them only on
    context exit, modelling a real LevelDB batch.  commit_hook (if set)
    runs at commit time, just before the buffered ops apply — the
    widest point of the old publish-before-commit race window.'''

    commit_hook = None

    def write_batch(self):
        kv = self

        class Batch:
            def __init__(self):
                self.ops = []

            def put(self, key, value):
                self.ops.append(('put', key, value))

            def delete(self, key):
                self.ops.append(('delete', key))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    if kv.commit_hook is not None:
                        kv.commit_hook()
                    for op, key, *rest in self.ops:
                        if op == 'put':
                            kv.data[key] = rest[0]
                        else:
                            kv.data.pop(key, None)
                return False

        return Batch()


HEADER_SPACING = 1000


class FakeHeadersFile:
    '''headers_file lookalike keyed on the fake header_offset spacing.'''

    def __init__(self, headers_by_height):
        self.headers_by_height = headers_by_height

    def read(self, offset, size):
        return self.headers_by_height.get(offset // HEADER_SPACING, b'')[:size]


def make_sapling_db(blocks=()):
    '''A DB over a fake KV store.  ``blocks`` items need 'height' and
    'hash' (display hex); items with 'raw' also provide the header for
    get_sapling_root.  fs_block_hashes serves raw hash bytes whose
    hash_to_hex_str equals the fixture hash the fake daemon keys on.'''
    db = object.__new__(DB)
    db.utxo_db = FakeKV()
    db.logger = mock.Mock()
    db.coin = Pivx
    db.db_height = 0
    db.db_tx_count = 0
    db.db_tip = b'\0' * 32
    db.db_version = max(DB.DB_VERSIONS)
    db.utxo_flush_count = 0
    db.wall_time = 0
    db.first_sync = False
    db.sapling_output_count = 0
    db.sapling_active_heights_built = True

    hashes = {}
    headers = {}
    for block in blocks:
        hashes[block['height']] = hex_str_to_hash(block['hash'])
        if block.get('raw'):
            header_len = Pivx.static_header_len(block['height'])
            headers[block['height']] = block['raw'][:header_len]

    async def fs_block_hashes(height, count):
        return [hashes[h] for h in range(height, height + count)]

    db.fs_block_hashes = fs_block_hashes
    db.header_offset = lambda height: height * HEADER_SPACING
    db.headers_file = FakeHeadersFile(headers)
    if hashes:
        db.db_height = max(hashes)
    return db


def load_block_fixture(filename):
    path = Path(__file__).parents[1] / 'blocks' / filename
    data = json.loads(path.read_text())
    data['raw'] = bytes.fromhex(data['block'])
    return data


def parse_block_txs(block):
    deser = tx_lib.DeserializerPIVX(
        block['raw'], start=Pivx.static_header_len(block['height']))
    return [deser.read_tx() for _ in range(deser._read_varint())]


def index_block_sapling(db, block, position_start=0):
    '''Index a fixture block the way the block processor would: explicit
    canonical positions and the header finalsaplingroot as the anchor.'''
    nullifiers = []
    commitments = []
    position = position_start
    for tx in parse_block_txs(block):
        if isinstance(tx, tx_lib.TxPIVXSapling):
            for spend_index, spend in enumerate(tx.sapling_spends):
                nullifiers.append((spend.nullifier, tx.txid,
                                   block['height'], spend_index))
            for output_index, output in enumerate(tx.sapling_outputs):
                commitments.append((output.cmu, tx.txid, output_index,
                                    block['height'], position))
                position += 1
    anchors = []
    if Pivx.static_header_len(block['height']) >= 112:
        anchors.append((block['raw'][80:112], block['height'], position))
    db.flush_sapling_data(db.utxo_db, nullifiers, commitments, anchors)
    db.sapling_output_count = position


class FixtureDaemon:

    def __init__(self, blocks):
        self.blocks_by_hash = {block['hash']: block for block in blocks}

    async def block_hex_hashes(self, *args):
        raise AssertionError(
            'the Sapling RPC surface must take block hashes from the '
            'indexed chain, never the daemon')

    async def raw_blocks(self, block_hashes):
        return [self.blocks_by_hash[block_hash]['raw']
                for block_hash in block_hashes]

    async def getnetworkinfo(self):
        return {
            'version': 5060100,
            'subversion': '/PIVX Core:5.6.1/',
        }


class LaggingDaemon(FixtureDaemon):

    def __init__(self, blocks, cached_height):
        super().__init__(blocks)
        self._cached_height = cached_height

    def cached_height(self):
        return self._cached_height


def make_session(db, daemon):
    session = object.__new__(PIVXSaplingElectrumX)
    session.coin = Pivx
    session.db = db
    session.session_mgr = mock.Mock()
    session.session_mgr.daemon = daemon
    session.session_mgr._method_counts = defaultdict(int)
    session.session_mgr._sapling_commitments_cache_key = None
    session.session_mgr._sapling_commitments_cache = None
    session.session_mgr._sapling_current_anchor_cache_key = None
    session.session_mgr._sapling_current_anchor_cache = None
    session.logger = logging.getLogger('test-pivx-sapling')
    session.bump_cost = lambda _cost: None
    session.sv_seen = False
    session.sv_negotiated = asyncio.Event()

    async def daemon_request(method, *args):
        return await getattr(daemon, method)(*args)

    session.daemon_request = daemon_request
    return session


def install_fake_helper(session, response=None,
                        helper_path='/fake/pivx_sapling_witness'):
    '''Replace the witness helper subprocess with a canned response.
    Returns the list of payloads the helper was called with.'''
    calls = []

    async def fake_helper(payload):
        calls.append(payload)
        return response

    session._sapling_call_witness_helper = fake_helper
    session._sapling_witness_helper_path = lambda: Path(helper_path)
    return calls


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_sapling_tx(tag, num_spends=0, num_outputs=0):
    spends = [
        tx_lib.SaplingSpend(
            cv=bytes(32), anchor=bytes(32),
            nullifier=asym32(0xF0, tag, n),
            rk=bytes(32), zkproof=bytes(192), spend_auth_sig=bytes(64))
        for n in range(num_spends)
    ]
    outputs = [
        tx_lib.SaplingOutput(
            cv=bytes(32), cmu=asym32(0xC0, tag, n),
            ephemeral_key=bytes(32), enc_ciphertext=bytes(580),
            out_ciphertext=bytes(80), zkproof=bytes(192))
        for n in range(num_outputs)
    ]
    return tx_lib.TxPIVXSapling(
        version=3, txtype=0, inputs=[], outputs=[], locktime=0,
        txid=bytes([tag]) * 32, wtxid=bytes([tag]) * 32,
        value_balance=0, sapling_spends=spends, sapling_outputs=outputs,
        binding_sig=bytes(64),
    )


def make_processor(db=None, height=0, coin=Pivx):
    processor = object.__new__(PIVXSaplingBlockProcessor)
    processor.coin = coin
    processor.db = db
    processor.height = height
    processor.sapling_nullifiers = []
    processor.sapling_commitments = []
    processor.sapling_anchors = []
    processor.sapling_undo_nullifiers = []
    processor.sapling_undo_commitments = []
    processor._sapling_backup_pending = False
    processor.sapling_output_count = None
    processor._last_sapling_root = None
    return processor


# ---------------------------------------------------------------------------
# Rollback policy
# ---------------------------------------------------------------------------

def test_pivx_sapling_rollback_policy_and_activation_heights():
    assert Pivx.REORG_LIMIT >= 100
    assert Pivx.SAPLING_START_HEIGHT == 2700500
    assert PivxTestnet.SAPLING_START_HEIGHT == 201


def test_client_rescan_start_covers_full_rollback_window_from_activation():
    def rescan_start(last_scanned_height):
        return max(
            Pivx.SAPLING_START_HEIGHT,
            last_scanned_height - Pivx.REORG_LIMIT + 1,
        )

    assert rescan_start(Pivx.SAPLING_START_HEIGHT) == Pivx.SAPLING_START_HEIGHT
    assert rescan_start(Pivx.SAPLING_START_HEIGHT + 99) == Pivx.SAPLING_START_HEIGHT
    assert rescan_start(Pivx.SAPLING_START_HEIGHT + 100) == (
        Pivx.SAPLING_START_HEIGHT + 1
    )
    assert (Pivx.SAPLING_START_HEIGHT + 100
            - rescan_start(Pivx.SAPLING_START_HEIGHT + 100) + 1) == Pivx.REORG_LIMIT


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_sapling_reorg_removes_outputs_spends_positions_and_anchors():
    db = make_sapling_db()
    kept_cm = asym32(0x01)
    removed_cm = asym32(0x02)
    kept_nf = asym32(0x03)
    removed_nf = asym32(0x04)
    kept_anchor = asym32(0x05)
    removed_anchor = asym32(0x06)

    db.flush_sapling_data(
        db.utxo_db,
        [(kept_nf, b'K' * 32, 149, 0),
         (removed_nf, b'R' * 32, 150, 1)],
        [(kept_cm, b'C' * 32, 0, 149, 0),
         (removed_cm, b'D' * 32, 1, 150, 1)],
        [(kept_anchor, 149, 1), (removed_anchor, 150, 2)],
    )

    db.backup_sapling_data(
        db.utxo_db, [removed_nf], [removed_cm], height_start=150)

    assert db.get_nullifier_spend(kept_nf) == (b'K' * 32, 149, 0)
    assert db.get_nullifier_spend(removed_nf) is None
    assert db.get_commitment_info(kept_cm) == (b'C' * 32, 0, 149)
    assert db.get_commitment_info(removed_cm) is None
    assert db.get_sapling_output_by_position(0)[0] == kept_cm
    assert db.get_sapling_output_by_position(1) is None
    assert db.get_anchor_height(kept_anchor) == 149
    assert db.get_anchor_height(removed_anchor) is None
    assert db.get_sapling_anchor_info(kept_anchor) == (149, 1)
    assert db.get_sapling_anchor_info(removed_anchor) is None


def test_reorg_can_respend_nullifier_on_different_branch():
    db = make_sapling_db()
    nullifier = asym32(0x11)

    db.flush_sapling_data(db.utxo_db, [(nullifier, b'o' * 32, 200, 0)],
                          [], [])
    assert db.get_nullifier_spend(nullifier) == (b'o' * 32, 200, 0)

    db.backup_sapling_data(db.utxo_db, [nullifier], [], height_start=200)
    assert db.get_nullifier_spend(nullifier) is None

    db.flush_sapling_data(db.utxo_db, [(nullifier, b'p' * 32, 201, 1)],
                          [], [])
    assert db.get_nullifier_spend(nullifier) == (b'p' * 32, 201, 1)


def test_anchor_first_seen_is_not_overwritten_by_later_reappearance():
    db = make_sapling_db()
    root = asym32(0x21)

    db.flush_sapling_data(db.utxo_db, [], [], [(root, 100, 5)])
    # The same consensus root re-appears later (e.g. blocks without
    # Sapling activity after a restart): put-if-absent keeps the first
    # sighting.
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 200, 9)])

    assert db.get_sapling_anchor_info(root) == (100, 5)


def test_anchor_survives_reorg_of_later_height_where_it_reappeared():
    db = make_sapling_db()
    root = asym32(0x22)

    db.flush_sapling_data(db.utxo_db, [], [], [(root, 100, 5)])
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 200, 9)])

    # Reorg reverting heights >= 150: the root was first seen at 100,
    # so it is still an anchor of the surviving branch.
    db.backup_sapling_data(db.utxo_db, [], [], height_start=150)

    assert db.get_sapling_anchor_info(root) == (100, 5)


def test_anchors_first_seen_at_reverted_heights_are_purged():
    db = make_sapling_db()
    old_root = asym32(0x23)
    new_root = asym32(0x24)

    db.flush_sapling_data(db.utxo_db, [], [],
                          [(old_root, 149, 3), (new_root, 160, 7)])
    db.backup_sapling_data(db.utxo_db, [], [], height_start=150)

    assert db.get_sapling_anchor_info(old_root) == (149, 3)
    assert db.get_sapling_anchor_info(new_root) is None


def test_backup_without_height_start_keeps_anchors():
    db = make_sapling_db()
    root = asym32(0x25)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 300, 2)])

    db.backup_sapling_data(db.utxo_db, [], [], height_start=None)

    assert db.get_sapling_anchor_info(root) == (300, 2)


def test_explicit_positions_round_trip_through_both_indexes():
    db = make_sapling_db()
    first = asym32(0x31)
    second = asym32(0x32)

    # Positions are assigned by the block processor; empty flushes in
    # between must not disturb them.
    db.flush_sapling_data(db.utxo_db, [], [(first, b'a' * 32, 0, 200, 0)], [])
    db.flush_sapling_data(db.utxo_db, [], [], [])
    db.flush_sapling_data(db.utxo_db, [], [(second, b'b' * 32, 0, 202, 1)], [])

    assert db.get_commitment_position_info(first) == (b'a' * 32, 0, 200, 0)
    assert db.get_commitment_position_info(second) == (b'b' * 32, 0, 202, 1)
    assert db.get_commitment_position(first) == 0
    assert db.get_commitment_position(second) == 1
    assert db.get_sapling_output_by_position(0) == (first, b'a' * 32, 0, 200, 0)
    assert db.get_sapling_output_by_position(1) == (second, b'b' * 32, 0, 202, 1)


def test_sapling_tree_size_at_binary_search():
    db = make_sapling_db()
    heights = [100, 100, 101, 103, 103, 107]
    db.flush_sapling_data(
        db.utxo_db,
        [],
        [(asym32(0x40, n), bytes([0x50 + n]) * 32, 0, height, n)
         for n, height in enumerate(heights)],
        [],
    )
    db.sapling_output_count = len(heights)

    assert db.sapling_tree_size_at(99) == 0
    assert db.sapling_tree_size_at(100) == 2
    assert db.sapling_tree_size_at(101) == 3
    assert db.sapling_tree_size_at(102) == 3
    assert db.sapling_tree_size_at(103) == 5
    assert db.sapling_tree_size_at(106) == 5
    assert db.sapling_tree_size_at(107) == 6
    assert db.sapling_tree_size_at(10**9) == 6


def test_sapling_tree_size_at_missing_position_raises():
    db = make_sapling_db()
    db.sapling_output_count = 2  # but no b'P' entries exist
    with pytest.raises(DB.DBError, match='missing Sapling output position'):
        db.sapling_tree_size_at(100)


def test_get_sapling_root_reads_header_bytes_80_112():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])

    root = db.get_sapling_root(block['height'])
    assert root == block['raw'][80:112]
    # Above the DB tip: not served
    assert db.get_sapling_root(block['height'] + 1) is None
    # Below activation: headers carry no Sapling root
    assert db.get_sapling_root(Pivx.SAPLING_START_HEIGHT - 1) is None


def test_get_sapling_root_requires_expanded_header():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    short = {'height': block['height'], 'hash': block['hash'],
             'raw': block['raw'][:80]}
    db = make_sapling_db([short])
    db.headers_file = FakeHeadersFile({block['height']: block['raw'][:80]})

    assert db.get_sapling_root(block['height']) is None


def test_legacy_synthetic_tree_apis_are_removed():
    for name in (
            'sapling_root_from_commitments',
            'get_sapling_root_info',
            'get_sapling_witness',
            'sapling_witness_path',
            'get_sapling_tree_state',
            'iter_sapling_nullifiers_by_height',
            'iter_sapling_commitments_by_height',
            'count_sapling_nullifiers',
            'count_sapling_commitments',
    ):
        assert not hasattr(DB, name)


def test_sapling_positions_remain_stable_across_restart():
    db = make_sapling_db()
    commitments = [asym32(0x60, n) for n in range(3)]
    db.db_height = 101
    db.flush_sapling_data(
        db.utxo_db,
        [],
        [(commitments[0], b'a' * 32, 0, 100, 0),
         (commitments[1], b'b' * 32, 1, 100, 1),
         (commitments[2], b'c' * 32, 0, 101, 2)],
        [],
    )
    db.sapling_output_count = 3
    db.write_utxo_state(db.utxo_db)

    restarted = make_sapling_db()
    restarted.utxo_db = db.utxo_db
    restarted.read_utxo_state()

    assert restarted.sapling_output_count == 3
    assert [restarted.get_commitment_position_info(c)[3]
            for c in commitments] == [0, 1, 2]


def _utxo_state(**overrides):
    state = {
        'genesis': Pivx.GENESIS_HASH,
        'height': Pivx.SAPLING_START_HEIGHT + 10,
        'tx_count': 1,
        'tip': b'\1' * 32,
        'utxo_flush_count': 1,
        'wall_time': 0,
        'first_sync': False,
        'db_version': max(DB.DB_VERSIONS),
        'sapling_output_count': 5,
    }
    state.update(overrides)
    return state


def test_read_utxo_state_forces_resync_without_sapling_index_version():
    db = make_sapling_db()
    db.utxo_db.put(b'state', repr(_utxo_state()).encode())

    with pytest.raises(DB.DBError, match='resync'):
        db.read_utxo_state()


def test_read_utxo_state_forces_resync_on_wrong_sapling_index_version():
    db = make_sapling_db()
    state = _utxo_state(
        sapling_index_version=DB.SAPLING_INDEX_VERSION + 1)
    db.utxo_db.put(b'state', repr(state).encode())

    with pytest.raises(DB.DBError, match='resync'):
        db.read_utxo_state()


def test_read_utxo_state_pre_activation_needs_no_sapling_index_version():
    db = make_sapling_db()
    state = _utxo_state(height=Pivx.SAPLING_START_HEIGHT - 1)
    db.utxo_db.put(b'state', repr(state).encode())

    db.read_utxo_state()

    assert db.db_height == Pivx.SAPLING_START_HEIGHT - 1
    assert db.sapling_output_count == 5


def test_write_utxo_state_stamps_sapling_index_version():
    assert DB.SAPLING_INDEX_VERSION == 1
    db = make_sapling_db()
    db.db_height = Pivx.SAPLING_START_HEIGHT + 10
    db.sapling_output_count = 42
    db.write_utxo_state(db.utxo_db)

    restarted = make_sapling_db()
    restarted.utxo_db = db.utxo_db
    restarted.read_utxo_state()

    assert restarted.db_height == Pivx.SAPLING_START_HEIGHT + 10
    assert restarted.sapling_output_count == 42


def test_flush_utxo_db_persists_sapling_data_and_output_count():
    db = make_sapling_db()
    db.history = mock.Mock(flush_count=7)
    nf = asym32(0x71)
    cm = asym32(0x72)
    root = asym32(0x73)
    flush_data = FlushData(
        5, 9, [], [], [], {}, [], b't' * 32,
        sapling_nullifiers=[(nf, b'h' * 32, 5, 0)],
        sapling_commitments=[(cm, b'h' * 32, 0, 5, 0)],
        sapling_anchors=[(root, 5, 1)],
        sapling_output_count=1,
    )

    db.flush_utxo_db(db.utxo_db, flush_data)

    # Heights are staged, not published, until the batch commits
    assert db.db_height == 0
    assert db.sapling_output_count == 0
    db.publish_flushed_state()

    # Persisted atomically with the UTXO flush
    assert db.sapling_output_count == 1
    assert db.db_height == 5
    assert db.get_nullifier_spend(nf) == (b'h' * 32, 5, 0)
    assert db.get_commitment_position_info(cm) == (b'h' * 32, 0, 5, 0)
    assert db.get_sapling_anchor_info(root) == (5, 1)
    # The live lists are cleared only once actually flushed
    assert flush_data.sapling_nullifiers == []
    assert flush_data.sapling_commitments == []
    assert flush_data.sapling_anchors == []


def test_flush_utxo_db_applies_pending_backup_purge():
    db = make_sapling_db()
    db.history = mock.Mock(flush_count=7)
    nf = asym32(0x74)
    cm = asym32(0x75)
    kept_root = asym32(0x76)
    reverted_root = asym32(0x77)
    db.flush_sapling_data(
        db.utxo_db,
        [(nf, b'h' * 32, 150, 0)],
        [(cm, b'h' * 32, 0, 150, 0)],
        [(kept_root, 100, 0), (reverted_root, 150, 1)],
    )

    flush_data = FlushData(
        149, 9, [], [], [], {}, [], b't' * 32,
        sapling_delete_nullifiers=[nf],
        sapling_delete_commitments=[cm],
        sapling_backup_height_start=150,
        sapling_output_count=0,
    )
    db.flush_utxo_db(db.utxo_db, flush_data)
    db.publish_flushed_state()

    assert db.sapling_output_count == 0
    assert db.get_nullifier_spend(nf) is None
    assert db.get_commitment_position_info(cm) is None
    assert db.get_sapling_output_by_position(0) is None
    assert db.get_sapling_anchor_info(kept_root) == (100, 0)
    assert db.get_sapling_anchor_info(reverted_root) is None
    assert flush_data.sapling_delete_nullifiers == []
    assert flush_data.sapling_delete_commitments == []


# ---------------------------------------------------------------------------
# Block processor
# ---------------------------------------------------------------------------

def test_advance_txs_assigns_positions_in_canonical_order(monkeypatch):
    monkeypatch.setattr(
        BlockProcessor, 'advance_txs',
        lambda _self, _txs, _is_unspendable: [])
    db = make_sapling_db()
    db.sapling_output_count = 7  # lazily seeds the in-memory counter
    processor = make_processor(db=db, height=299)
    processor._advance_block_height = 300

    tx_a = make_sapling_tx(1, num_spends=1, num_outputs=2)
    plain = tx_lib.TxPIVX(version=1, txtype=0, inputs=[], outputs=[],
                          locktime=0, txid=b'p' * 32, wtxid=b'p' * 32)
    tx_b = make_sapling_tx(2, num_outputs=1)

    processor.advance_txs([tx_a, plain, tx_b], lambda _script: False)

    assert processor.sapling_commitments == [
        (tx_a.sapling_outputs[0].cmu, tx_a.txid, 0, 300, 7),
        (tx_a.sapling_outputs[1].cmu, tx_a.txid, 1, 300, 8),
        (tx_b.sapling_outputs[0].cmu, tx_b.txid, 0, 300, 9),
    ]
    assert processor.sapling_nullifiers == [
        (tx_a.sapling_spends[0].nullifier, tx_a.txid, 300, 0),
    ]
    assert processor.sapling_output_count == 10


def test_advance_txs_indexes_current_block_height_from_fixture(monkeypatch):
    block = load_block_fixture('pivx_mainnet_5057529.json')
    txs = parse_block_txs(block)
    monkeypatch.setattr(
        BlockProcessor, 'advance_txs',
        lambda _self, _txs, _is_unspendable: [])
    processor = make_processor(height=block['height'] - 1)
    processor.sapling_output_count = 0
    processor._advance_block_height = block['height']

    processor.advance_txs(txs, lambda _script: False)

    assert processor.sapling_commitments
    assert processor.sapling_nullifiers
    indexed_heights = (
        [item[2] for item in processor.sapling_nullifiers]
        + [item[3] for item in processor.sapling_commitments]
    )
    assert set(indexed_heights) == {block['height']}
    assert [item[4] for item in processor.sapling_commitments] == [0, 1]


def test_advance_blocks_records_first_seen_header_anchors(monkeypatch):
    # PivxTestnet activation is 201, keeping the fixture small
    def fake_advance(self, blocks):
        self.height += len(blocks)
        self.sapling_output_count += 2  # pretend each block adds outputs

    monkeypatch.setattr(BlockProcessor, 'advance_blocks', fake_advance)
    processor = make_processor(height=199, coin=PivxTestnet)
    processor.sapling_output_count = 3

    root_a = asym32(0xA1)
    root_b = asym32(0xB1)

    def block(root, header_len=112):
        header = (bytes(80) + root)[:header_len]
        return SimpleNamespace(header=header)

    processor.advance_blocks([
        block(root_a),            # height 200: below activation, ignored
        block(root_a),            # height 201: first appearance, recorded
        block(root_a),            # height 202: unchanged root, skipped
        block(root_b),            # height 203: new root, recorded
        block(root_b, header_len=80),  # height 204: short header, ignored
    ])

    assert processor.height == 204
    # tree size recorded is the count after each recording block
    assert processor.sapling_anchors == [
        (root_a, 201, 7),
        (root_b, 203, 11),
    ]


def test_backup_txs_rewinds_count_and_marks_backup_pending(monkeypatch):
    monkeypatch.setattr(
        BlockProcessor, 'backup_txs',
        lambda _self, _txs, _is_unspendable: None)
    processor = make_processor(height=250)
    processor.sapling_output_count = 10
    processor._last_sapling_root = asym32(0xA2)

    tx_a = make_sapling_tx(3, num_spends=1, num_outputs=2)
    tx_b = make_sapling_tx(4, num_outputs=1)

    processor.backup_txs([tx_a, tx_b], lambda _script: False)

    assert processor.sapling_output_count == 7
    assert processor.sapling_undo_nullifiers == [
        tx_a.sapling_spends[0].nullifier]
    assert set(processor.sapling_undo_commitments) == {
        tx_a.sapling_outputs[0].cmu, tx_a.sapling_outputs[1].cmu,
        tx_b.sapling_outputs[0].cmu}
    assert processor._sapling_backup_pending is True
    assert processor._last_sapling_root is None


def test_flush_data_passes_live_lists_count_and_backup_height(monkeypatch):
    monkeypatch.setattr(
        BlockProcessor, 'flush_data',
        lambda _self: FlushData(123, 10, [], [], [], {}, [], b'h' * 32))
    processor = make_processor(height=123)
    processor.sapling_output_count = 6
    processor.sapling_nullifiers = [(asym32(0xA3), b't' * 32, 123, 0)]
    processor.sapling_commitments = [(asym32(0xA4), b't' * 32, 0, 123, 5)]
    processor.sapling_anchors = [(asym32(0xA5), 123, 6)]
    processor.sapling_undo_nullifiers = [asym32(0xA6)]
    processor.sapling_undo_commitments = [asym32(0xA7)]

    flush_data = processor.flush_data()

    # Live lists, not copies: history-only flushes must not lose data
    assert flush_data.sapling_nullifiers is processor.sapling_nullifiers
    assert flush_data.sapling_commitments is processor.sapling_commitments
    assert flush_data.sapling_anchors is processor.sapling_anchors
    assert flush_data.sapling_delete_nullifiers is (
        processor.sapling_undo_nullifiers)
    assert flush_data.sapling_delete_commitments is (
        processor.sapling_undo_commitments)
    assert flush_data.sapling_output_count == 6
    # No backup pending: no purge height
    assert flush_data.sapling_backup_height_start is None
    flush_data.sapling_commitments.clear()
    assert processor.sapling_commitments == []

    # With a pending backup the first reverted height is height+1.
    # The flag survives flush_data so a failed backup flush can be
    # retried; it clears when the next advance begins.
    processor._sapling_backup_pending = True
    flush_data = processor.flush_data()
    assert flush_data.sapling_backup_height_start == 124
    assert processor._sapling_backup_pending is True
    flush_data = processor.flush_data()
    assert flush_data.sapling_backup_height_start == 124
    processor.advance_blocks([])
    assert processor._sapling_backup_pending is False


def test_positions_rewound_after_reorg_via_processor_count(monkeypatch):
    monkeypatch.setattr(
        BlockProcessor, 'advance_txs',
        lambda _self, _txs, _is_unspendable: [])
    monkeypatch.setattr(
        BlockProcessor, 'backup_txs',
        lambda _self, _txs, _is_unspendable: None)
    db = make_sapling_db()
    processor = make_processor(db=db, height=299)

    # Advance a block with two outputs at positions 0 and 1
    old_branch_tx = make_sapling_tx(5, num_outputs=2)
    processor._advance_block_height = 300
    processor.advance_txs([old_branch_tx], lambda _script: False)
    db.flush_sapling_data(db.utxo_db, processor.sapling_nullifiers,
                          processor.sapling_commitments,
                          processor.sapling_anchors)
    db.sapling_output_count = processor.sapling_output_count
    processor.sapling_commitments.clear()
    assert db.sapling_output_count == 2

    # Reorg: revert the block, then flush the deletions
    processor.backup_txs([old_branch_tx], lambda _script: False)
    processor.height = 299  # fork point after backup
    db.backup_sapling_data(db.utxo_db, processor.sapling_undo_nullifiers,
                           processor.sapling_undo_commitments,
                           height_start=processor.height + 1)
    db.sapling_output_count = processor.sapling_output_count
    assert processor.sapling_output_count == 0
    assert db.get_sapling_output_by_position(0) is None
    assert db.get_sapling_output_by_position(1) is None

    # The replacement branch reuses the rewound positions
    new_branch_tx = make_sapling_tx(6, num_outputs=1)
    processor._advance_block_height = 300
    processor.advance_txs([new_branch_tx], lambda _script: False)
    assert processor.sapling_commitments == [
        (new_branch_tx.sapling_outputs[0].cmu, new_branch_tx.txid, 0, 300, 0),
    ]


# ---------------------------------------------------------------------------
# Session: capabilities, aliases, envelopes
# ---------------------------------------------------------------------------

def test_sapling_capabilities_do_not_advertise_release_ready_without_witness_backend(
        monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    session = make_session(make_sapling_db(), FixtureDaemon([]))

    capabilities = run(session.sapling_capabilities())

    assert capabilities['success'] is False
    assert capabilities['contract'] is None
    assert capabilities['version'] == 1
    assert capabilities['server_version']
    assert capabilities['pivx_core_version'] == 'PIVX Core:5.6.1'
    assert capabilities['network'] == 'mainnet'
    assert capabilities['sapling_activation_height'] == 2700500
    assert capabilities['max_block_range'] == PIVX_SAPLING_MAX_BLOCK_RANGE
    assert capabilities['range_response'] == 'envelope'
    assert capabilities['release_contract_ready'] is False
    assert capabilities['features'] == {
        'global_output_positions': True,
        'block_hashes': True,
        'structured_errors': True,
        'canonical_witnesses': False,
        'consistent_db_height': True,
    }
    assert capabilities['hex_byte_order'] == 'display'
    assert capabilities['consensus_anchors'] is True
    assert capabilities['range_response_format'][
        'global_output_positions'] is True
    assert capabilities['range_response_format']['block_hashes'] is True
    for error_type in ('invalid_range', 'index_incomplete', 'index_error',
                       'missing_block', 'unsupported_method'):
        assert error_type in capabilities['range_error_types']
    # Removed by the indexed-chain redesign: hashes come from the index,
    # which is complete by construction
    for legacy in ('missing_block_hash', 'partial_index', 'pruned_range'):
        assert legacy not in capabilities['range_error_types']
    assert capabilities['witness_response'] == 'unavailable'
    assert 'witness_backend_unavailable' in capabilities[
        'witness_error_types']
    for method in (
            'blockchain.sapling.get_block_range',
            'blockchain.sapling.get_best_anchor',
            'blockchain.sapling.get_witness',
            'blockchain.sapling.get_nullifier_status',
            'blockchain.sapling.get_commitment_info'):
        assert method in capabilities['required_methods']
        assert method in capabilities['methods']
    assert 'get_block_range' in capabilities['aliases'][
        'blockchain.sapling.get_block_range']


def test_sapling_capabilities_advertise_canonical_witness_backend(
        monkeypatch, tmp_path):
    helper = tmp_path / 'pivx_sapling_witness'
    helper.write_text('#!/bin/sh\nexit 1\n')
    helper.chmod(0o755)
    monkeypatch.setenv(PIVX_SAPLING_WITNESS_HELPER_ENV, str(helper))
    session = make_session(make_sapling_db(), FixtureDaemon([]))

    capabilities = run(session.sapling_capabilities())

    assert capabilities['success'] is True
    assert capabilities['contract'] == PIVX_SAPLING_RPC_CONTRACT
    assert capabilities['release_contract_ready'] is True
    assert capabilities['features']['canonical_witnesses'] is True
    assert capabilities['witness_response'] == 'canonical_path'
    assert capabilities['witness_backend'] == str(helper)
    assert capabilities['witness_path_length'] == 32
    assert capabilities['witness_path_order'] == 'leaf_to_root'


def test_sapling_cake_wallet_aliases_are_advertised_and_registered():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.set_request_handlers((1, 4))

    expected_aliases = {
        'blockchain.sapling.capabilities': [
            'blockchain.sapling.get_capabilities',
            'server.sapling.capabilities',
            'sapling.capabilities',
            'get_capabilities',
        ],
        'blockchain.sapling.get_block_range': [
            'blockchain.sapling.get_blocks',
            'get_block_range',
            'sapling.get_block_range',
        ],
        'blockchain.sapling.get_nullifier_status': [
            'blockchain.sapling.check_nullifier',
            'sapling.get_nullifier_status',
        ],
        'blockchain.sapling.get_commitment_info': [
            'blockchain.sapling.get_commitment',
            'blockchain.commitment.get_info',
            'sapling.get_commitment_info',
        ],
        'blockchain.sapling.get_best_anchor': [
            'blockchain.sapling.best_anchor',
            'sapling.get_best_anchor',
        ],
        'blockchain.sapling.get_anchor_height': [
            'blockchain.anchor.get_height',
            'sapling.get_anchor_height',
        ],
        'blockchain.sapling.get_tree_state': [
            'blockchain.sapling.get_treestate',
            'sapling.get_tree_state',
        ],
        'blockchain.sapling.get_witness': [
            'sapling.get_witness',
        ],
    }

    aliases = run(session.sapling_capabilities())['aliases']
    for canonical, method_aliases in expected_aliases.items():
        assert aliases[canonical] == method_aliases
        canonical_handler = session.request_handlers[canonical]
        for alias in method_aliases:
            assert session.request_handlers[alias] == canonical_handler
    assert session.request_handlers['blockchain.nullifier.get_spend'] == (
        session.nullifier_get_spend
    )


def test_sapling_capabilities_do_not_advertise_v1_if_not_release_ready():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.SAPLING_METHODS = ['blockchain.sapling.get_block_range']

    capabilities = run(session.sapling_capabilities())

    assert capabilities['success'] is False
    assert capabilities['contract'] is None
    assert capabilities['release_contract_ready'] is False


def test_sapling_capabilities_request_handler_is_awaitable(monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.request_handlers = {
        'blockchain.sapling.capabilities': session.sapling_capabilities,
    }

    response = run(session.handle_request(
        Request('blockchain.sapling.capabilities', [])))

    assert response['success'] is False
    assert response['contract'] is None
    assert response['features']['canonical_witnesses'] is False


def test_sapling_unknown_contract_method_returns_structured_error():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.request_handlers = {}

    response = run(session.handle_request(
        Request('blockchain.sapling.future_method', [])))

    assert response['success'] is False
    assert response['contract'] == PIVX_SAPLING_RPC_CONTRACT
    assert response['method'] == 'blockchain.sapling.future_method'
    assert response['error']['type'] == 'unsupported_method'
    assert 'blockchain.sapling.get_block_range' in response['supported_methods']


def test_sapling_get_nullifiers_method_is_removed():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.set_request_handlers((1, 4))

    assert not hasattr(PIVXSaplingElectrumX, 'sapling_get_nullifiers')
    assert 'blockchain.sapling.get_nullifiers' not in session.request_handlers

    response = run(session.handle_request(
        Request('blockchain.sapling.get_nullifiers', [100, 200])))

    assert response['success'] is False
    assert response['error']['type'] == 'unsupported_method'
    assert response['method'] == 'blockchain.sapling.get_nullifiers'


# ---------------------------------------------------------------------------
# Session: version negotiation
# ---------------------------------------------------------------------------

def test_sapling_methods_are_allowed_before_server_version():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.set_request_handlers((1, 4))
    assert session.sv_seen is False

    response = run(session.handle_request(
        Request('blockchain.sapling.capabilities', [])))

    assert response['version'] == 1
    # Harmless server info probes are whitelisted too
    for method in ('server.features', 'server.ping', 'server.banner',
                   'get_capabilities', 'get_block_range'):
        assert session.pre_version_method_allowed(method) is True
    assert session.pre_version_method_allowed(
        'blockchain.headers.subscribe') is False


def test_non_sapling_method_before_server_version_disconnects():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.set_request_handlers((1, 4))
    crash_attempts = []

    async def fake_crash():
        crash_attempts.append(True)

    session._do_crash_old_electrum_client = fake_crash

    with pytest.raises(ReplyAndDisconnect):
        run(session.handle_request(
            Request('blockchain.headers.subscribe', [])))
    assert crash_attempts == [True]


# ---------------------------------------------------------------------------
# Session: index readiness
# ---------------------------------------------------------------------------

def test_sapling_index_status_tolerates_daemon_lag_up_to_two_blocks():
    db = make_sapling_db()
    db.db_height = Pivx.SAPLING_START_HEIGHT + 100

    tolerant = make_session(
        db, LaggingDaemon([], cached_height=db.db_height + 2))
    status = tolerant._sapling_index_status()
    assert status['ready'] is True
    assert status['state'] == 'ready'
    assert status['lag'] == 2

    behind = make_session(
        db, LaggingDaemon([], cached_height=db.db_height + 3))
    status = behind._sapling_index_status()
    assert status['ready'] is False
    assert status['state'] == 'index_not_ready'
    assert status['lag'] == 3


def test_sapling_capabilities_downgrade_when_index_is_behind_tip(
        monkeypatch, tmp_path):
    helper = tmp_path / 'pivx_sapling_witness'
    helper.write_text('#!/bin/sh\nexit 1\n')
    helper.chmod(0o755)
    monkeypatch.setenv(PIVX_SAPLING_WITNESS_HELPER_ENV, str(helper))
    db = make_sapling_db()
    db.db_height = 100
    session = make_session(db, LaggingDaemon([], cached_height=105))

    response = run(session.sapling_capabilities())

    assert response['success'] is False
    assert response['contract'] is None
    assert response['release_contract_ready'] is False
    assert response['features']['canonical_witnesses'] is True
    assert response['index_status'] == {
        'ready': False,
        'state': 'index_not_ready',
        'db_height': 100,
        'daemon_height': 105,
        'lag': 5,
        'sapling_output_count': 0,
        'retryable': True,
    }


def test_live_helper_methods_fail_fast_when_index_is_behind_tip():
    db = make_sapling_db()
    db.db_height = 10
    session = make_session(db, LaggingDaemon([], cached_height=13))

    commitment_info = run(session.commitment_get_info(display(asym32(1))))
    nullifier_status = run(
        session.sapling_get_nullifier_status(display(asym32(2))))
    best_anchor = run(session.sapling_get_best_anchor())

    assert commitment_info['success'] is False
    assert commitment_info['error']['type'] == 'index_not_ready'
    assert nullifier_status['success'] is False
    assert nullifier_status['error']['type'] == 'index_not_ready'
    assert best_anchor['available'] is False
    assert best_anchor['error']['type'] == 'index_not_ready'


# ---------------------------------------------------------------------------
# Session: get_block_range
# ---------------------------------------------------------------------------

def test_client_can_rescan_full_pivx_rollback_boundary_with_hashes():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    index_block_sapling(db, block)
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is True
    assert response['complete'] is True
    assert response['empty'] is False
    assert response['height_count'] == 1
    assert response['block_hashes'] == [
        {'height': block['height'], 'block_hash': block['hash']}
    ]
    stale_local_hashes = {block['height']: 'ff' * 32}
    mismatches = [
        item['height']
        for item in response['block_hashes']
        if stale_local_hashes[item['height']] != item['block_hash']
    ]
    assert mismatches == [block['height']]


def test_get_block_range_success_empty_scanned_range_is_complete():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is True
    assert response['complete'] is True
    assert response['empty'] is True
    assert response['height_count'] == 1
    assert response['block_count'] == 1
    assert response['sapling_tx_count'] == 0
    assert response['block_hashes'] == [
        {'height': block['height'], 'block_hash': block['hash']}
    ]
    assert response['blocks'] == [{
        'height': block['height'],
        'hash': block['hash'],
        'block_hash': block['hash'],
        'time': int.from_bytes(block['raw'][68:72], 'little'),
        'outputs': [],
        'txs': [],
    }]
    assert response['error'] is None


def test_get_block_range_returns_canonical_output_order_with_positions():
    block = load_block_fixture('pivx_mainnet_5057529.json')
    db = make_sapling_db([block])
    index_block_sapling(db, block)
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is True
    outputs = response['blocks'][0]['outputs']
    expected_outputs = []
    for tx_index, tx in enumerate(parse_block_txs(block)):
        if isinstance(tx, tx_lib.TxPIVXSapling):
            for output_index, output in enumerate(tx.sapling_outputs):
                expected_outputs.append((
                    len(expected_outputs),
                    tx_index,
                    output_index,
                    display(output.cmu),
                    hash_to_hex_str(tx.txid),
                ))
    assert expected_outputs
    assert [(output['position'], output['global_position'],
             output['output_index'], output['cmu'])
            for output in outputs] == [
                (position, position, output_index, cmu)
                for position, _tx_index, output_index, cmu, _txid
                in expected_outputs
            ]
    assert [(output['tx_index'], output['txid']) for output in outputs] == [
        (tx_index, txid)
        for _position, tx_index, _output_index, _cmu, txid in expected_outputs
    ]


def test_get_block_range_serves_display_byte_order():
    block = load_block_fixture('pivx_mainnet_5057529.json')
    db = make_sapling_db([block])
    index_block_sapling(db, block)
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is True
    sapling_tx = next(tx for tx in parse_block_txs(block)
                      if isinstance(tx, tx_lib.TxPIVXSapling))
    compact_tx = response['blocks'][0]['txs'][0]

    output = sapling_tx.sapling_outputs[0]
    output_data = compact_tx['outputs'][0]
    assert output_data['cmu'] == display(output.cmu)
    assert output_data['cmu'] != output.cmu.hex()  # reversal must happen
    assert output_data['epk'] == display(output.ephemeral_key)
    assert output_data['ephemeral_key'] == output_data['epk']
    assert output_data['cv'] == display(output.cv)
    # Ciphertexts are natural-order byte vectors, not uint256 values
    assert output_data['enc_ciphertext'] == output.enc_ciphertext.hex()
    assert output_data['out_ciphertext'] == output.out_ciphertext.hex()

    spend = sapling_tx.sapling_spends[0]
    spend_data = compact_tx['spends'][0]
    assert spend_data['nullifier'] == display(spend.nullifier)
    assert spend_data['nullifier'] != spend.nullifier.hex()
    assert spend_data['anchor'] == display(spend.anchor)
    assert spend_data['cv'] == display(spend.cv)
    assert spend_data['rk'] == display(spend.rk)


def test_get_block_range_invalid_range_is_structured():
    db = make_sapling_db()
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_block_range(20, 19))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['height_count'] == 0
    assert response['error']['type'] == 'invalid_range'


def test_get_block_range_too_large_is_structured():
    db = make_sapling_db()
    db.db_height = 10_000
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_block_range(
        0, PIVX_SAPLING_MAX_BLOCK_RANGE))

    assert response['success'] is False
    assert response['error']['type'] == 'invalid_range'
    assert response['error']['max_block_range'] == (
        PIVX_SAPLING_MAX_BLOCK_RANGE)


def test_get_block_range_above_indexed_tip_is_index_incomplete():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    db.db_height = block['height'] - 1
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['blocks'] == []
    assert response['error']['type'] == 'index_incomplete'
    assert response['error']['indexed_height'] == block['height'] - 1


def test_get_block_range_fails_fast_when_index_is_behind_tip():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    db.db_height = block['height'] - 3
    session = make_session(
        db,
        LaggingDaemon([block], cached_height=block['height']),
    )

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['block_count'] == 0
    assert response['error']['type'] == 'index_not_ready'
    assert response['error']['retryable'] is True
    assert response['error']['db_height'] == block['height'] - 3
    assert response['error']['daemon_height'] == block['height']


class FailingRawBlocksDaemon:

    async def raw_blocks(self, block_hashes):
        raise DaemonError('daemon unavailable')


def test_get_block_range_daemon_failure_is_not_complete():
    blocks = [{'height': height, 'hash': format(height, '064x')}
              for height in (10, 11, 12)]
    db = make_sapling_db(blocks)
    session = make_session(db, FailingRawBlocksDaemon())

    response = run(session.sapling_get_block_range(10, 12))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['height_count'] == 3
    # Hashes come from our own index and are reported even on failure
    assert [item['height'] for item in response['block_hashes']] == [10, 11, 12]
    assert response['blocks'] == []
    assert response['error']['type'] == 'daemon_error'


class SlowRawBlocksDaemon:

    def cached_height(self):
        return 20

    async def raw_blocks(self, block_hashes):
        await asyncio.sleep(0.2)
        return []


def test_get_block_range_core_rpc_timeout_is_structured(monkeypatch):
    monkeypatch.setenv('PIVX_SAPLING_RPC_TIMEOUT', '0.05')
    db = make_sapling_db([{'height': 20, 'hash': '11' * 32}])
    session = make_session(db, SlowRawBlocksDaemon())

    response = run(session.sapling_get_block_range(20, 20))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['error']['type'] == 'backend_timeout'
    assert response['error']['retryable'] is True


class ShortRawBlocksDaemon:

    def __init__(self):
        self.raw_calls = 0

    async def raw_blocks(self, block_hashes):
        self.raw_calls += 1
        return []


def test_get_block_range_persistent_short_raw_blocks_is_missing_block():
    blocks = [{'height': height, 'hash': format(height, '064x')}
              for height in (10, 11)]
    db = make_sapling_db(blocks)
    daemon = ShortRawBlocksDaemon()
    session = make_session(db, daemon)

    response = run(session.sapling_get_block_range(10, 11))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['error']['type'] == 'missing_block'
    assert response['error']['expected_count'] == 2
    assert response['error']['actual_count'] == 0
    assert daemon.raw_calls == 2  # one retry, then fail closed


class TransientShortRawBlocksDaemon(FixtureDaemon):

    def __init__(self, blocks):
        super().__init__(blocks)
        self.raw_calls = 0

    async def raw_blocks(self, block_hashes):
        self.raw_calls += 1
        if self.raw_calls == 1:
            return []
        return await super().raw_blocks(block_hashes)


def test_get_block_range_recovers_from_transient_short_raw_blocks():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    daemon = TransientShortRawBlocksDaemon([block])
    session = make_session(db, daemon)

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is True
    assert response['complete'] is True
    assert response['empty'] is True
    assert response['block_count'] == 1
    assert response['blocks'][0]['height'] == block['height']
    assert response['blocks'][0]['txs'] == []
    assert daemon.raw_calls == 2


def test_get_block_range_index_incomplete_is_not_complete():
    block = load_block_fixture('pivx_mainnet_5057529.json')
    db = make_sapling_db([block])  # commitments deliberately not indexed
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['height_count'] == 1
    assert response['block_hashes'] == [
        {'height': block['height'], 'block_hash': block['hash']}
    ]
    assert response['blocks'] == []
    assert response['error']['type'] == 'index_incomplete'
    assert response['error']['height'] == block['height']
    assert 'commitment' in response['error']
    # The offending commitment is reported in display byte order
    sapling_tx = next(tx for tx in parse_block_txs(block)
                      if isinstance(tx, tx_lib.TxPIVXSapling))
    assert response['error']['commitment'] == display(
        sapling_tx.sapling_outputs[0].cmu)


# ---------------------------------------------------------------------------
# Session: get_outputs
# ---------------------------------------------------------------------------

def test_get_outputs_serves_display_order_from_indexed_hashes():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_outputs(
        block['height'], block['height']))

    sapling_tx = next(tx for tx in parse_block_txs(block)
                      if isinstance(tx, tx_lib.TxPIVXSapling))
    output = sapling_tx.sapling_outputs[0]
    assert response['count'] == 1
    assert response['more'] is False
    assert response['outputs'] == [{
        'txid': hash_to_hex_str(sapling_tx.txid),
        'index': 0,
        'height': block['height'],
        'cmu': display(output.cmu),
        'epk': display(output.ephemeral_key),
        'enc_ciphertext': output.enc_ciphertext.hex(),
    }]


def test_get_outputs_fails_on_short_daemon_block_response():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])

    class ShortDaemon(FixtureDaemon):
        # Daemon returns fewer blocks than requested
        async def raw_blocks(self, block_hashes):
            return []

    session = make_session(db, ShortDaemon([block]))

    with pytest.raises(RPCError, match='of 1 requested blocks'):
        run(session.sapling_get_outputs(block['height'], block['height']))


def test_get_outputs_rejects_range_above_indexed_tip():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    with pytest.raises(RPCError, match='above indexed tip'):
        run(session.sapling_get_outputs(
            block['height'], block['height'] + 1))


def test_get_outputs_caps_range_at_max_block_range():
    db = make_sapling_db()
    db.db_height = 10_000
    session = make_session(db, FixtureDaemon([]))

    with pytest.raises(RPCError, match='range too large'):
        run(session.sapling_get_outputs(0, PIVX_SAPLING_MAX_BLOCK_RANGE))


# ---------------------------------------------------------------------------
# Session: get_tree_state
# ---------------------------------------------------------------------------

def test_get_tree_state_serves_consensus_header_root():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    index_block_sapling(db, block)
    session = make_session(db, FixtureDaemon([block]))
    root = block['raw'][80:112]

    response = run(session.sapling_get_tree_state(block['height']))

    assert response == {
        'success': True,
        'contract': PIVX_SAPLING_RPC_CONTRACT,
        'height': block['height'],
        'block_hash': block['hash'],
        'anchor': display(root),
        'root': display(root),
        'latest_anchor': display(root),
        'anchor_first_height': block['height'],
        'tree_size': 1,
        'commitment_count': 1,
        'indexed_height': block['height'],
        'sapling_activation_height': Pivx.SAPLING_START_HEIGHT,
    }
    # Defaults to the indexed tip
    assert run(session.sapling_get_tree_state()) == response


def test_get_tree_state_above_tip_is_index_incomplete():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_tree_state(block['height'] + 1))

    assert response['success'] is False
    assert response['error']['type'] == 'index_incomplete'
    assert response['error']['indexed_height'] == block['height']


def test_get_tree_state_below_activation_is_invalid_range():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_tree_state(
        Pivx.SAPLING_START_HEIGHT - 1))

    assert response['success'] is False
    assert response['error']['type'] == 'invalid_range'
    assert response['error']['sapling_activation_height'] == (
        Pivx.SAPLING_START_HEIGHT)


def test_get_tree_state_missing_header_root_is_index_error():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    db.db_height = block['height'] + 1  # no header stored for this height
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_tree_state(block['height'] + 1))

    assert response['success'] is False
    assert response['error']['type'] == 'index_error'


def test_get_tree_state_unindexed_root_is_index_incomplete():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    # Commitments indexed but the header root was never anchored
    for tx in parse_block_txs(block):
        if isinstance(tx, tx_lib.TxPIVXSapling):
            db.flush_sapling_data(
                db.utxo_db, [],
                [(output.cmu, tx.txid, n, block['height'], n)
                 for n, output in enumerate(tx.sapling_outputs)],
                [])
            db.sapling_output_count = len(tx.sapling_outputs)
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_tree_state(block['height']))

    assert response['success'] is False
    assert response['error']['type'] == 'index_incomplete'


# ---------------------------------------------------------------------------
# Session: nullifier / commitment / anchor lookups (display byte order)
# ---------------------------------------------------------------------------

def test_nullifier_status_uses_display_byte_order():
    db = make_sapling_db()
    nf_raw = asym32(0x81)
    tx_hash = asym32(0x82)
    db.flush_sapling_data(db.utxo_db, [(nf_raw, tx_hash, 250, 1)], [], [])
    session = make_session(db, FixtureDaemon([]))

    spent = run(session.sapling_get_nullifier_status(display(nf_raw)))
    assert spent == {
        'spent': True,
        'tx_hash': hash_to_hex_str(tx_hash),
        'txid': hash_to_hex_str(tx_hash),
        'height': 250,
        'spend_index': 1,
    }

    # Raw-order input must not match: the boundary reverses exactly once
    raw_order = run(session.sapling_get_nullifier_status(nf_raw.hex()))
    assert raw_order['spent'] is False


def test_unknown_nullifier_status_returns_structured_unspent_response():
    db = make_sapling_db()
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_nullifier_status(display(asym32(9))))

    assert response == {
        'spent': False,
        'tx_hash': None,
        'txid': None,
        'height': None,
        'spend_index': None,
    }


def test_check_nullifiers_wraps_statuses_in_envelope():
    db = make_sapling_db()
    nf_raw = asym32(0x83)
    db.flush_sapling_data(db.utxo_db, [(nf_raw, asym32(0x84), 251, 0)],
                          [], [])
    session = make_session(db, FixtureDaemon([]))
    spent_hex = display(nf_raw)
    unspent_hex = display(asym32(0x85))

    response = run(session.sapling_check_nullifiers(
        [spent_hex, unspent_hex]))

    assert response['success'] is True
    assert response['contract'] == PIVX_SAPLING_RPC_CONTRACT
    assert response['results'][spent_hex]['spent'] is True
    assert response['results'][unspent_hex]['spent'] is False


def test_commitment_info_uses_display_byte_order():
    db = make_sapling_db()
    cm_raw = asym32(0x86)
    tx_hash = asym32(0x87)
    db.flush_sapling_data(db.utxo_db, [],
                          [(cm_raw, tx_hash, 3, 260, 12)], [])
    session = make_session(db, FixtureDaemon([]))

    response = run(session.commitment_get_info(display(cm_raw)))
    assert response == {
        'exists': True,
        'txid': hash_to_hex_str(tx_hash),
        'output_index': 3,
        'height': 260,
        'position': 12,
    }

    raw_order = run(session.commitment_get_info(cm_raw.hex()))
    assert raw_order['exists'] is False


def test_unknown_commitment_info_returns_structured_absent_response():
    db = make_sapling_db()
    session = make_session(db, FixtureDaemon([]))

    response = run(session.commitment_get_info(display(asym32(8))))

    assert response == {
        'exists': False,
        'txid': None,
        'output_index': None,
        'height': None,
        'position': None,
    }


def test_anchor_get_height_uses_display_byte_order():
    db = make_sapling_db()
    root = asym32(0x88)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 123, 9)])
    session = make_session(db, FixtureDaemon([]))

    assert run(session.anchor_get_height(display(root))) == 123
    assert run(session.anchor_get_height(root.hex())) is None


# ---------------------------------------------------------------------------
# Session: witnesses and best anchor (canonical helper + consensus anchors)
# ---------------------------------------------------------------------------

def seed_witness_db(db, count=4, height_base=401):
    '''Index ``count`` commitments at positions 0..count-1.'''
    cmus = [asym32(0x10, n) for n in range(count)]
    tx_hashes = [asym32(0x90, n) for n in range(count)]
    db.flush_sapling_data(
        db.utxo_db,
        [],
        [(cmu, tx_hashes[n], n, height_base + n, n)
         for n, cmu in enumerate(cmus)],
        [],
    )
    db.sapling_output_count = count
    return cmus, tx_hashes


WITNESS_PATH = [(bytes([i]) * 32).hex() for i in range(32)]


def test_sapling_commitments_for_witness_is_async_raw_order_and_cached():
    db = make_sapling_db()
    cmus, _tx_hashes = seed_witness_db(db)
    session = make_session(db, FixtureDaemon([]))

    commitments = run(session._sapling_commitments_for_witness())

    assert commitments == [
        {'cmu': cmu.hex(), 'height': 401 + n}  # raw order for the helper
        for n, cmu in enumerate(cmus)
    ]
    assert run(session._sapling_commitments_for_witness()) is commitments


def test_sapling_witness_fails_closed_without_canonical_backend(monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    root = asym32(0xA8)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([]))

    with pytest.raises(RPCError, match='witness_backend_unavailable'):
        run(session.sapling_get_witness(display(cmus[2]), display(root)))


def test_sapling_commitment_only_witness_fails_closed_without_backend(
        monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db, count=1)
    session = make_session(db, FixtureDaemon([]))

    with pytest.raises(RPCError, match='witness_backend_unavailable'):
        run(session.sapling_get_witness(display(cmus[0])))


def test_sapling_witness_round_trips_display_order_and_validates_anchor():
    db = make_sapling_db()
    db.db_height = 500
    cmus, tx_hashes = seed_witness_db(db)
    root = asym32(0xA9)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([]))
    costs = []
    session.bump_cost = costs.append
    calls = install_fake_helper(session, {
        'success': True,
        'root': root.hex(),  # helper speaks raw order
        'anchor_height': 404,
        'tree_size': 4,
        'path': WITNESS_PATH,
    })

    witness = run(session.sapling_get_witness(
        display(cmus[2]), display(root)))

    # The helper receives raw-order hex for the anchor and commitments
    assert len(calls) == 1
    payload = calls[0]
    assert payload['mode'] == 'witness'
    assert payload['position'] == 2
    assert payload['anchor'] == root.hex()
    assert payload['commitments'] == [
        {'cmu': cmu.hex(), 'height': 401 + n}
        for n, cmu in enumerate(cmus)
    ]

    # The response converts back to display order exactly once
    assert witness['anchor'] == display(root)
    assert witness['root'] == display(root)
    assert witness['cmu'] == display(cmus[2])
    assert witness['commitment'] == display(cmus[2])
    assert witness['anchor'] != root.hex()
    assert witness['position'] == 2
    assert witness['global_position'] == 2
    assert witness['height'] == 403
    assert witness['txid'] == hash_to_hex_str(tx_hashes[2])
    assert witness['output_index'] == 2
    assert witness['anchor_height'] == 404
    assert witness['tree_size'] == 4
    # Witness path elements remain raw Sapling node encodings
    assert witness['path'] == WITNESS_PATH
    assert witness['witness'] == WITNESS_PATH
    assert witness['path_length'] == 32
    assert witness['path_order'] == 'leaf_to_root'
    # Witness cost scales with the indexed output count
    assert costs[0] == 5.0 + db.sapling_output_count / 500


def test_sapling_witness_by_position_without_anchor():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    root = asym32(0xAA)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([]))
    calls = install_fake_helper(session, {
        'success': True,
        'root': root.hex(),
        'anchor_height': 404,
        'tree_size': 4,
        'path': WITNESS_PATH,
    })

    witness = run(session.sapling_get_witness(1))

    assert calls[0]['position'] == 1
    assert calls[0]['anchor'] is None
    assert witness['cmu'] == display(cmus[1])
    assert witness['anchor'] == display(root)


def test_sapling_witness_rejects_non_consensus_requested_anchor():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    session = make_session(db, FixtureDaemon([]))
    calls = install_fake_helper(session, {'success': True})

    with pytest.raises(RPCError, match='index_incomplete'):
        run(session.sapling_get_witness(
            display(cmus[0]), display(asym32(0xAB))))

    # Validated against b'A' before paying for the helper call
    assert calls == []


def test_sapling_witness_fails_closed_when_helper_root_not_consensus():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    session = make_session(db, FixtureDaemon([]))
    rogue_root = asym32(0xAC)  # never anchored in b'A'
    calls = install_fake_helper(session, {
        'success': True,
        'root': rogue_root.hex(),
        'anchor_height': 404,
        'tree_size': 4,
        'path': WITNESS_PATH,
    })

    with pytest.raises(RPCError, match='index_incomplete'):
        run(session.sapling_get_witness(display(cmus[0])))
    assert len(calls) == 1


def test_sapling_witness_fails_closed_on_tree_size_mismatch():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    root = asym32(0xAD)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([]))
    install_fake_helper(session, {
        'success': True,
        'root': root.hex(),
        'anchor_height': 404,
        'tree_size': 3,  # diverges from the consensus anchor's size
        'path': WITNESS_PATH,
    })

    with pytest.raises(RPCError, match='tree size'):
        run(session.sapling_get_witness(display(cmus[0])))


def test_sapling_witness_fails_closed_on_missing_tree_size():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    root = asym32(0xAD)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([]))
    # Helper omits tree_size entirely: must not bypass the size check
    install_fake_helper(session, {
        'success': True,
        'root': root.hex(),
        'anchor_height': 404,
        'path': WITNESS_PATH,
    })

    with pytest.raises(RPCError, match='missing its tree size'):
        run(session.sapling_get_witness(display(cmus[0])))


def test_sapling_witness_detects_anchor_mismatch_from_helper():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    requested = asym32(0xAE)
    db.flush_sapling_data(db.utxo_db, [], [], [(requested, 404, 4)])
    session = make_session(db, FixtureDaemon([]))
    install_fake_helper(session, {
        'success': True,
        'root': asym32(0xAF).hex(),  # not the requested anchor
        'anchor_height': 404,
        'tree_size': 4,
        'path': WITNESS_PATH,
    })

    with pytest.raises(RPCError, match='anchor_mismatch'):
        run(session.sapling_get_witness(
            display(cmus[0]), display(requested)))


def test_sapling_witness_rejects_invalid_helper_path_shape():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    session = make_session(db, FixtureDaemon([]))
    install_fake_helper(session, {
        'success': True,
        'root': asym32(0xB0).hex(),
        'tree_size': 4,
        'path': WITNESS_PATH[:31],  # must be exactly 32 nodes
    })

    with pytest.raises(RPCError, match='invalid path shape'):
        run(session.sapling_get_witness(display(cmus[0])))


def test_sapling_get_witnesses_batches_and_caps():
    db = make_sapling_db()
    db.db_height = 500
    cmus, _tx_hashes = seed_witness_db(db)
    root = asym32(0xB1)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([]))
    install_fake_helper(session, {
        'success': True,
        'root': root.hex(),
        'anchor_height': 404,
        'tree_size': 4,
        'path': WITNESS_PATH,
    })

    witnesses = run(session.sapling_get_witnesses([0, 1]))
    assert [w['position'] for w in witnesses] == [0, 1]
    assert [w['cmu'] for w in witnesses] == [
        display(cmus[0]), display(cmus[1])]

    with pytest.raises(RPCError, match='more than 100'):
        run(session.sapling_get_witnesses(list(range(101))))


def test_sapling_best_anchor_serves_validated_display_anchor():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    cmus, _tx_hashes = seed_witness_db(db)
    root = asym32(0xB2)
    db.flush_sapling_data(db.utxo_db, [], [], [(root, 404, 4)])
    session = make_session(db, FixtureDaemon([block]))
    calls = install_fake_helper(session, {
        'success': True,
        'anchor': root.hex(),  # raw order from the helper
        'anchor_height': 404,
        'tree_size': 4,
    })

    response = run(session.sapling_get_best_anchor())

    assert response['available'] is True
    assert response['anchor'] == display(root)
    assert response['root'] == display(root)
    assert response['anchor'] != root.hex()
    assert response['anchor_height'] == 404
    assert response['tree_size'] == 4
    assert response['height'] == block['height']
    assert response['block_hash'] == block['hash']
    assert calls[0]['mode'] == 'root'

    # Cached while the tree is unchanged: no second helper call
    again = run(session.sapling_get_best_anchor())
    assert again['anchor'] == response['anchor']
    assert len(calls) == 1


def test_sapling_best_anchor_fails_closed_when_root_not_consensus():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    seed_witness_db(db)
    session = make_session(db, FixtureDaemon([block]))
    install_fake_helper(session, {
        'success': True,
        'anchor': asym32(0xB3).hex(),  # not in b'A'
        'anchor_height': 404,
        'tree_size': 4,
    })

    response = run(session.sapling_get_best_anchor())

    assert response['available'] is False
    assert response['anchor'] is None
    assert response['root'] is None
    assert response['error']['type'] == 'canonical_anchor_unavailable'
    assert 'index_incomplete' in response['error']['message']


def test_sapling_best_anchor_fails_closed_without_canonical_backend(
        monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db([block])
    index_block_sapling(db, block)
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_best_anchor())

    assert response['available'] is False
    assert response['anchor'] is None
    assert response['root'] is None
    assert response['height'] == block['height']
    assert response['anchor_height'] is None
    assert response['block_hash'] == block['hash']
    assert response['error']['type'] == 'canonical_anchor_unavailable'


def test_sapling_best_anchor_returns_structured_response_without_anchor(
        monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_best_anchor())

    assert response['available'] is False
    assert response['anchor'] is None
    assert response['anchor_height'] is None
    assert response['height'] == block['height']
    assert response['block_hash'] == block['hash']
    assert response['error']['type'] == 'canonical_anchor_unavailable'


def test_live_helper_methods_do_not_leak_internal_errors(monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))
    session.set_request_handlers((1, 4))

    best_anchor = run(session.handle_request(
        Request('blockchain.sapling.get_best_anchor', [])))
    nullifier_status = run(session.handle_request(Request(
        'blockchain.sapling.get_nullifier_status', [display(asym32(7))])))
    commitment_info = run(session.handle_request(Request(
        'blockchain.sapling.get_commitment_info', [display(asym32(6))])))

    assert best_anchor['available'] is False
    assert best_anchor['anchor'] is None
    assert best_anchor['block_hash'] == block['hash']
    assert nullifier_status['spent'] is False
    assert commitment_info['exists'] is False


# ---------------------------------------------------------------------------
# Active-height index (blockchain.sapling.get_active_heights)
# ---------------------------------------------------------------------------

def _flush_activity(db, spends=(), outputs=()):
    '''Flush spend/output activity at the given heights.'''
    nullifiers = [(asym32(0xA0, h & 0xFF), b'\2' * 32, h, 0) for h in spends]
    commitments = [(asym32(0xB0, h & 0xFF), b'\3' * 32, 0, h, pos)
                   for pos, h in enumerate(outputs)]
    db.flush_sapling_data(db.utxo_db, nullifiers, commitments, [])


def test_flush_sapling_data_records_active_heights():
    db = make_sapling_db()
    _flush_activity(db, spends=[7], outputs=[5, 7])

    heights, complete = db.get_sapling_active_heights(0, 100, 10)

    assert heights == [5, 7]
    assert complete is True


def test_active_heights_query_clamps_to_requested_range():
    db = make_sapling_db()
    _flush_activity(db, outputs=[5, 10, 20, 30])

    heights, complete = db.get_sapling_active_heights(10, 20, 10)

    assert heights == [10, 20]
    assert complete is True


def test_active_heights_pages_deterministically():
    db = make_sapling_db()
    _flush_activity(db, outputs=[10, 20, 30, 40, 50])

    first = db.get_sapling_active_heights(10, 50, 2)
    assert first == ([10, 20], False)
    second = db.get_sapling_active_heights(21, 50, 2)
    assert second == ([30, 40], False)
    third = db.get_sapling_active_heights(41, 50, 2)
    assert third == ([50], True)


def test_active_heights_exact_limit_is_complete():
    db = make_sapling_db()
    _flush_activity(db, outputs=[10, 20])

    assert db.get_sapling_active_heights(0, 100, 2) == ([10, 20], True)


def test_backup_sapling_data_purges_active_heights():
    db = make_sapling_db()
    _flush_activity(db, spends=[5, 6], outputs=[7])

    db.backup_sapling_data(db.utxo_db, [], [], height_start=6)

    assert db.get_sapling_active_heights(0, 100, 10) == ([5], True)


def test_backfill_builds_active_heights_from_existing_indexes():
    db = make_sapling_db()
    _flush_activity(db, spends=[6], outputs=[5, 8])
    # Simulate a DB synced before the b'S' index existed
    for key in [k for k in db.utxo_db.data if k.startswith(b'S')]:
        db.utxo_db.delete(key)
    db.sapling_active_heights_built = False
    assert db.get_sapling_active_heights(0, 100, 10) == ([], True)

    db._backfill_sapling_active_heights()

    assert db.get_sapling_active_heights(0, 100, 10) == ([5, 6, 8], True)
    assert db.sapling_active_heights_built is True
    state = ast.literal_eval(db.utxo_db.get(b'state').decode())
    assert state['sapling_active_heights_built'] is True


def test_read_utxo_state_backfills_active_heights_once():
    db = make_sapling_db()
    _flush_activity(db, spends=[6], outputs=[5])
    for key in [k for k in db.utxo_db.data if k.startswith(b'S')]:
        db.utxo_db.delete(key)
    state = _utxo_state(sapling_index_version=DB.SAPLING_INDEX_VERSION)
    assert 'sapling_active_heights_built' not in state
    db.utxo_db.put(b'state', repr(state).encode())

    db.read_utxo_state()

    assert db.get_sapling_active_heights(0, 100, 10) == ([5, 6], True)
    # A later restart must not rebuild: remove one key and re-read
    db.utxo_db.delete(DB.sapling_active_height_key(5))
    db.read_utxo_state()
    assert db.get_sapling_active_heights(0, 100, 10) == ([6], True)


def test_write_utxo_state_stamps_active_heights_marker():
    db = make_sapling_db()
    db.write_utxo_state(db.utxo_db)

    state = ast.literal_eval(db.utxo_db.get(b'state').decode())
    assert state['sapling_active_heights_built'] is True


def test_get_active_heights_rpc_envelope_and_clamp():
    db = make_sapling_db()
    db.db_height = 100
    _flush_activity(db, spends=[40], outputs=[20, 90])
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_active_heights(10, 500, 10))

    assert response == {
        'heights': [20, 40, 90],
        'start': 10,
        'end': 100,  # clamped to db_height, not an error
        'complete': True,
        'db_height': 100,
    }


def test_get_active_heights_rpc_empty_range_is_complete():
    db = make_sapling_db()
    db.db_height = 100
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_active_heights(10, 50))

    assert response['heights'] == []
    assert response['complete'] is True
    assert response['end'] == 50


def test_get_active_heights_rpc_above_tip_is_empty_and_complete():
    db = make_sapling_db()
    db.db_height = 100
    _flush_activity(db, outputs=[90])
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_active_heights(101, 200, 10))

    assert response['heights'] == []
    assert response['complete'] is True
    assert response['end'] == 100
    assert response['db_height'] == 100


def test_get_active_heights_rpc_truncation_sets_resume_end():
    db = make_sapling_db()
    db.db_height = 100
    _flush_activity(db, outputs=[10, 20, 30, 40, 50])
    session = make_session(db, FixtureDaemon([]))

    first = run(session.sapling_get_active_heights(0, 100, 2))
    assert first['heights'] == [10, 20]
    assert first['complete'] is False
    assert first['end'] == 20

    resumed = run(session.sapling_get_active_heights(
        first['end'] + 1, 100, 2))
    assert resumed['heights'] == [30, 40]
    assert resumed['complete'] is False
    assert resumed['end'] == 40

    final = run(session.sapling_get_active_heights(
        resumed['end'] + 1, 100, 2))
    assert final['heights'] == [50]
    assert final['complete'] is True
    assert final['end'] == 100


def test_get_active_heights_rpc_fails_fast_when_index_is_behind_tip():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    db.db_height = block['height'] - 3
    session = make_session(
        db,
        LaggingDaemon([block], cached_height=block['height']),
    )

    response = run(session.sapling_get_active_heights(
        block['height'], block['height']))

    assert response['heights'] == []
    assert response['complete'] is False
    assert response['error']['type'] == 'index_not_ready'
    assert response['error']['retryable'] is True


def test_get_active_heights_rpc_rejects_inverted_range():
    db = make_sapling_db()
    db.db_height = 100
    session = make_session(db, FixtureDaemon([]))

    with pytest.raises(RPCError, match='end_height'):
        run(session.sapling_get_active_heights(50, 10))


def test_get_active_heights_matches_get_block_range_emptiness():
    '''Acceptance: active heights == the heights get_block_range reports
    non-empty, verified over real mainnet fixture blocks.'''
    for filename in ('pivx_mainnet_5057529.json', 'pivx_mainnet_10000.json'):
        block = load_block_fixture(filename)
        db = make_sapling_db([block])
        index_block_sapling(db, block)
        session = make_session(db, FixtureDaemon([block]))

        range_response = run(session.sapling_get_block_range(
            block['height'], block['height']))
        assert range_response['success'] is True
        active = run(session.sapling_get_active_heights(
            block['height'], block['height']))
        assert active['complete'] is True

        expected = [] if range_response['empty'] else [block['height']]
        assert active['heights'] == expected


def test_capabilities_advertise_active_height_index(monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    capabilities = run(session.sapling_capabilities())

    assert capabilities['supports_active_height_index'] is True
    assert capabilities['active_heights_max_limit'] == 50000
    assert ('blockchain.sapling.get_active_heights'
            in capabilities['methods'])
    assert capabilities['aliases'][
        'blockchain.sapling.get_active_heights'] == [
            'sapling.get_active_heights']


# ---------------------------------------------------------------------------
# db_height as a committed watermark (commit-then-publish)
# ---------------------------------------------------------------------------

def _flushable_db():
    '''A fixture DB wired so full flush_dbs/flush_backup runs, with a
    batch-faithful KV: writes apply only at batch commit.'''
    db = make_sapling_db()
    db.utxo_db = BufferingFakeKV()
    db.history = mock.Mock(flush_count=1)
    db.history.assert_flushed = lambda: None

    def fake_flush_fs(fd):
        # Real flush_fs writes files then updates the fs pointers
        db.fs_height = fd.height
        db.fs_tx_count = fd.tx_count

    db.flush_fs = fake_flush_fs
    db.flush_history = lambda: None
    db.backup_fs = lambda _height, _tx_count: None
    db.last_flush = 0.0
    db.last_flush_tx_count = 0
    db.fs_tx_count = 0
    return db


def _advance_flush_data(cm, nf):
    return FlushData(
        5, 9, [], [], [], {}, [], b't' * 32,
        sapling_nullifiers=[(nf, b'h' * 32, 5, 0)],
        sapling_commitments=[(cm, b'h' * 32, 0, 5, 0)],
        sapling_anchors=[(asym32(0x83), 5, 1)],
        sapling_output_count=1,
    )


def test_db_height_publishes_only_after_batch_commit():
    '''A reader at the widest point of the old race window (heights
    assigned, batch not yet committed) must see the OLD consistent
    view: old db_height AND no new rows.  Regression for the live bug
    where get_active_heights omitted a just-flushed block while
    db_height already covered it (block 5553285).'''
    db = _flushable_db()
    cm, nf = asym32(0x81), asym32(0x82)
    observed = {}

    def commit_hook():
        observed['db_height'] = db.db_height
        observed['output_count'] = db.sapling_output_count
        observed['active'] = db.get_sapling_active_heights(0, 10 ** 9, 10)
        observed['commitment'] = db.get_commitment_position_info(cm)

    db.utxo_db.commit_hook = commit_hook

    db.flush_dbs(_advance_flush_data(cm, nf), True,
                 estimate_txs_remaining=lambda: 0)

    # At commit time nothing was published: watermark still old,
    # matching the still-invisible rows
    assert observed == {
        'db_height': 0,
        'output_count': 0,
        'active': ([], True),
        'commitment': None,
    }
    # After the flush returns, everything is visible together
    assert db.db_height == 5
    assert db.sapling_output_count == 1
    assert db.get_sapling_active_heights(0, 100, 10) == ([5], True)
    assert db.get_commitment_position_info(cm) == (b'h' * 32, 0, 5, 0)
    # The persisted state carries the NEW heights (written inside the
    # same batch as the rows)
    state = ast.literal_eval(db.utxo_db.get(b'state').decode())
    assert state['height'] == 5
    assert state['sapling_output_count'] == 1


def test_backup_publishes_lowered_heights_only_after_purge_commits():
    db = _flushable_db()
    cm, nf = asym32(0x84), asym32(0x85)
    db.flush_dbs(_advance_flush_data(cm, nf), True,
                 estimate_txs_remaining=lambda: 0)
    assert db.db_height == 5

    observed = {}

    def commit_hook():
        observed['db_height'] = db.db_height
        observed['active'] = db.get_sapling_active_heights(0, 100, 10)
        observed['commitment'] = db.get_commitment_position_info(cm)

    db.utxo_db.commit_hook = commit_hook
    backup_data = FlushData(
        4, 8, [], [], [], {}, [], b'u' * 32,
        sapling_delete_nullifiers=[nf],
        sapling_delete_commitments=[cm],
        sapling_backup_height_start=5,
        sapling_output_count=0,
    )

    db.flush_backup(backup_data, touched=set())

    # At commit time the watermark is ALREADY lowered while the purged
    # rows are still readable — the safe direction: no reader can hold
    # db_height 5 after its rows vanish.  Surviving rows above the
    # watermark are harmless (range reads clamp to db_height).
    assert observed == {
        'db_height': 4,
        'active': ([5], True),
        'commitment': (b'h' * 32, 0, 5, 0),
    }
    # After: watermark lowered and rows purged, atomically
    assert db.db_height == 4
    assert db.sapling_output_count == 0
    assert db.get_sapling_active_heights(0, 100, 10) == ([], True)
    assert db.get_commitment_position_info(cm) is None
    state = ast.literal_eval(db.utxo_db.get(b'state').decode())
    assert state['height'] == 4
    assert state['sapling_output_count'] == 0


def test_capabilities_advertise_consistent_db_height(monkeypatch):
    monkeypatch.delenv(PIVX_SAPLING_WITNESS_HELPER_ENV, raising=False)
    monkeypatch.setattr('electrumx.server.session.shutil.which',
                        lambda _name: None)
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db([block])
    session = make_session(db, FixtureDaemon([block]))

    capabilities = run(session.sapling_capabilities())

    assert capabilities['features']['consistent_db_height'] is True


def test_failed_batch_publishes_for_upstream_failure_semantics():
    '''If the batch fails to commit, the staged heights are still
    published (memory ahead of disk, matching upstream's pre-fix
    failure behavior) and cleared, so the shutdown flush early-exits
    via assert_flushed instead of persisting state for rows the dead
    batch lost.'''
    db = _flushable_db()
    cm, nf = asym32(0x86), asym32(0x87)

    def boom():
        raise RuntimeError('commit failed')

    db.utxo_db.commit_hook = boom
    flush_data = _advance_flush_data(cm, nf)

    with pytest.raises(RuntimeError, match='commit failed'):
        db.flush_dbs(flush_data, True, estimate_txs_remaining=lambda: 0)

    assert db.db_height == 5
    assert db._staged_publish is None
    assert db.utxo_db.get(b'state') is None  # nothing committed

    # The shutdown flush retry must write nothing (assert_flushed path)
    db.utxo_db.commit_hook = None
    db.flush_dbs(flush_data, True, estimate_txs_remaining=lambda: 0)
    assert db.utxo_db.get(b'state') is None


def test_get_active_heights_detects_mid_request_reorg():
    '''A request that snapshots db_height, then has a reorg purge land
    before its scan, must fail retryable (index_incomplete) instead of
    returning complete:true without the purged height — the response
    carries no block_hashes, so a silent partial answer would let a
    wallet skip a replacement shielded block forever.'''
    db = make_sapling_db()
    db.db_height = 5
    _flush_activity(db, outputs=[5])
    session = make_session(db, FixtureDaemon([]))

    real_scan = db.get_sapling_active_heights

    def scan_after_reorg(start, end, limit):
        # The reorg lands between the handler's snapshot and the scan:
        # watermark lowered, height-5 rows purged
        db.backup_sapling_data(db.utxo_db, [], [], height_start=5)
        db.db_height = 4
        return real_scan(start, end, limit)

    db.get_sapling_active_heights = scan_after_reorg

    response = run(session.sapling_get_active_heights(0, 5, 10))

    assert response['heights'] == []
    assert response['complete'] is False
    assert response['error']['type'] == 'index_incomplete'
    assert response['error']['retryable'] is True
    assert response['error']['indexed_height'] == 4
    assert response['db_height'] == 4
    assert response['end'] == 4

    # The retry against the new watermark succeeds consistently
    db.get_sapling_active_heights = real_scan
    retry = run(session.sapling_get_active_heights(0, 4, 10))
    assert retry['complete'] is True
    assert retry['heights'] == []


def test_backup_flush_increments_reorg_count_advance_does_not():
    db = _flushable_db()
    cm, nf = asym32(0x88), asym32(0x89)
    assert db.sapling_reorg_count == 0

    db.flush_dbs(_advance_flush_data(cm, nf), True,
                 estimate_txs_remaining=lambda: 0)
    assert db.sapling_reorg_count == 0

    backup_data = FlushData(
        4, 8, [], [], [], {}, [], b'u' * 32,
        sapling_delete_nullifiers=[nf],
        sapling_delete_commitments=[cm],
        sapling_backup_height_start=5,
        sapling_output_count=0,
    )
    db.flush_backup(backup_data, touched=set())
    assert db.sapling_reorg_count == 1


def test_get_active_heights_detects_same_height_replacement_reorg():
    '''Down-and-back-up: the reorg purges height 5 and the replacement
    branch regrows to 5 before the handler re-reads db_height.  The
    height check alone cannot see it; the reorg counter must.'''
    db = make_sapling_db()
    db.db_height = 5
    _flush_activity(db, outputs=[5])
    session = make_session(db, FixtureDaemon([]))

    real_scan = db.get_sapling_active_heights

    def scan_during_replacement(start, end, limit):
        result = real_scan(start, end, limit)
        # After the scan: backup publishes (counter moves), replacement
        # branch reflushes different activity at the same height
        db.backup_sapling_data(db.utxo_db, [], [], height_start=5)
        db.sapling_reorg_count += 1
        db.flush_sapling_data(
            db.utxo_db, [], [(asym32(0x8A), b'r' * 32, 0, 5, 0)], [])
        return result

    db.get_sapling_active_heights = scan_during_replacement

    response = run(session.sapling_get_active_heights(0, 5, 10))

    assert response['complete'] is False
    assert response['error']['type'] == 'index_incomplete'
    assert response['error']['retryable'] is True
    assert response['heights'] == []


def test_get_outputs_detects_mid_request_reorg():
    block = load_block_fixture('pivx_mainnet_5057529.json')
    db = make_sapling_db([block])
    index_block_sapling(db, block)

    class ReorgingDaemon(FixtureDaemon):
        async def raw_blocks(self, block_hashes):
            db.sapling_reorg_count += 1
            return await super().raw_blocks(block_hashes)

    session = make_session(db, ReorgingDaemon([block]))

    with pytest.raises(RPCError, match='index_incomplete'):
        run(session.sapling_get_outputs(block['height'], block['height']))
