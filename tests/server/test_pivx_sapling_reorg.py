import asyncio
from collections import defaultdict
import json
import logging
from pathlib import Path
from unittest import mock

from aiorpcx import Request

from electrumx.lib.coins import Pivx, PivxTestnet
from electrumx.lib import tx as tx_lib
from electrumx.server.daemon import DaemonError
from electrumx.server.db import DB
from electrumx.server.session import (
    PIVXSaplingElectrumX,
    PIVX_SAPLING_MAX_BLOCK_RANGE,
    PIVX_SAPLING_RPC_CONTRACT,
)


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


def make_sapling_db():
    db = object.__new__(DB)
    db.utxo_db = FakeKV()
    db.logger = mock.Mock()
    db.coin = Pivx
    db.db_height = 0
    db.db_tx_count = 0
    db.db_tip = b'\0' * 32
    db.db_version = 8
    db.utxo_flush_count = 0
    db.wall_time = 0
    db.first_sync = False
    db.sapling_output_count = 0
    return db


def apply_deletes(db, keys):
    for key in keys:
        db.utxo_db.delete(key)


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


def test_sapling_reorg_removes_outputs_spends_anchors_roots_and_positions():
    db = make_sapling_db()
    kept_cm = b'c' * 32
    removed_cm = b'd' * 32
    kept_nf = b'n' * 32
    removed_nf = b'o' * 32
    kept_anchor = b'a' * 32
    removed_anchor = b'b' * 32

    db.flush_sapling_data(
        db.utxo_db,
        [(kept_nf, b'K' * 32, 149, 0),
         (removed_nf, b'R' * 32, 150, 1)],
        [(kept_cm, b'C' * 32, 0, 149),
         (removed_cm, b'D' * 32, 1, 150)],
        [(kept_anchor, 149), (removed_anchor, 150)],
    )
    kept_root = DB.sapling_root_from_commitments([kept_cm])
    removed_root = DB.sapling_root_from_commitments([kept_cm, removed_cm])

    deletes = []
    batch = mock.Mock()
    batch.delete.side_effect = deletes.append
    db.backup_sapling_data(
        batch, [removed_nf], [removed_cm], [removed_anchor],
        height_start=150)
    apply_deletes(db, deletes)

    assert db.get_nullifier_spend(kept_nf) == (b'K' * 32, 149, 0)
    assert db.get_nullifier_spend(removed_nf) is None
    assert db.get_commitment_info(kept_cm) == (b'C' * 32, 0, 149)
    assert db.get_commitment_info(removed_cm) is None
    assert db.get_sapling_output_by_position(0)[0] == kept_cm
    assert db.get_sapling_output_by_position(1) is None
    assert db.get_anchor_height(kept_anchor) == 149
    assert db.get_anchor_height(removed_anchor) is None
    assert db.get_sapling_root_info(kept_root) == (1, 149)
    assert db.get_sapling_root_info(removed_root) is None
    assert db.sapling_output_count == 1


def test_reorg_can_respend_nullifier_on_different_branch():
    db = make_sapling_db()
    nullifier = b'x' * 32

    db.flush_sapling_data(db.utxo_db, [(nullifier, b'o' * 32, 200, 0)],
                          [], [])
    assert db.get_nullifier_spend(nullifier) == (b'o' * 32, 200, 0)

    deletes = []
    batch = mock.Mock()
    batch.delete.side_effect = deletes.append
    db.backup_sapling_data(batch, [nullifier], [], [], height_start=200)
    apply_deletes(db, deletes)
    assert db.get_nullifier_spend(nullifier) is None

    db.flush_sapling_data(db.utxo_db, [(nullifier, b'p' * 32, 201, 1)],
                          [], [])
    assert db.get_nullifier_spend(nullifier) == (b'p' * 32, 201, 1)


def load_block_fixture(filename):
    path = Path(__file__).parents[1] / 'blocks' / filename
    data = json.loads(path.read_text())
    data['raw'] = bytes.fromhex(data['block'])
    return data


def parse_block_txs(block):
    deser = tx_lib.DeserializerPIVX(
        block['raw'], start=Pivx.static_header_len(block['height']))
    return [deser.read_tx() for _ in range(deser._read_varint())]


def index_block_sapling(db, block):
    nullifiers = []
    commitments = []
    anchors = []
    seen_anchors = set()
    for tx in parse_block_txs(block):
        if isinstance(tx, tx_lib.TxPIVXSapling):
            for spend_index, spend in enumerate(tx.sapling_spends):
                nullifiers.append((spend.nullifier, tx.txid,
                                   block['height'], spend_index))
                if spend.anchor not in seen_anchors:
                    anchors.append((spend.anchor, block['height']))
                    seen_anchors.add(spend.anchor)
            for output_index, output in enumerate(tx.sapling_outputs):
                commitments.append((output.cmu, tx.txid, output_index,
                                    block['height']))
    db.flush_sapling_data(db.utxo_db, nullifiers, commitments, anchors)


class FixtureDaemon:

    def __init__(self, blocks):
        self.blocks_by_height = {block['height']: block for block in blocks}
        self.blocks_by_hash = {block['hash']: block for block in blocks}

    async def block_hex_hashes(self, start_height, count):
        return [
            self.blocks_by_height[height]['hash']
            for height in range(start_height, start_height + count)
        ]

    async def raw_blocks(self, block_hashes):
        return [self.blocks_by_hash[block_hash]['raw']
                for block_hash in block_hashes]

    async def getnetworkinfo(self):
        return {
            'version': 5060100,
            'subversion': '/PIVX Core:5.6.1/',
        }


def make_session(db, daemon):
    session = object.__new__(PIVXSaplingElectrumX)
    session.coin = Pivx
    session.db = db
    session.session_mgr = mock.Mock()
    session.session_mgr.daemon = daemon
    session.session_mgr._method_counts = defaultdict(int)
    session.logger = logging.getLogger('test-pivx-sapling')
    session.bump_cost = lambda _cost: None

    async def daemon_request(method, *args):
        return await getattr(daemon, method)(*args)

    session.daemon_request = daemon_request
    return session


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_client_can_rescan_full_pivx_rollback_boundary_with_hashes():
    block = load_block_fixture('pivx_mainnet_2703076.json')
    db = make_sapling_db()
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


def test_sapling_capabilities_document_cake_wallet_v1_contract():
    session = make_session(make_sapling_db(), FixtureDaemon([]))

    capabilities = run(session.sapling_capabilities())

    assert capabilities['success'] is True
    assert capabilities['contract'] == PIVX_SAPLING_RPC_CONTRACT
    assert capabilities['version'] == 1
    assert capabilities['server_version']
    assert capabilities['pivx_core_version'] == 'PIVX Core:5.6.1'
    assert capabilities['network'] == 'mainnet'
    assert capabilities['sapling_activation_height'] == 2700500
    assert capabilities['max_block_range'] == PIVX_SAPLING_MAX_BLOCK_RANGE
    assert capabilities['range_response'] == 'envelope'
    assert capabilities['release_contract_ready'] is True
    assert capabilities['features'] == {
        'global_output_positions': True,
        'block_hashes': True,
        'structured_errors': True,
    }
    assert capabilities['range_response_format'][
        'global_output_positions'] is True
    assert capabilities['range_response_format']['block_hashes'] is True
    assert 'unsupported_method' in capabilities['range_error_types']
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


def test_sapling_capabilities_do_not_advertise_v1_if_not_release_ready():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.SAPLING_METHODS = ['blockchain.sapling.get_block_range']

    capabilities = run(session.sapling_capabilities())

    assert capabilities['success'] is False
    assert capabilities['contract'] is None
    assert capabilities['release_contract_ready'] is False


def test_sapling_capabilities_request_handler_is_awaitable():
    session = make_session(make_sapling_db(), FixtureDaemon([]))
    session.request_handlers = {
        'blockchain.sapling.capabilities': session.sapling_capabilities,
    }

    response = run(session.handle_request(
        Request('blockchain.sapling.capabilities', [])))

    assert response['success'] is True
    assert response['contract'] == PIVX_SAPLING_RPC_CONTRACT


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


def test_get_block_range_success_empty_scanned_range_is_complete():
    block = load_block_fixture('pivx_mainnet_10000.json')
    db = make_sapling_db()
    session = make_session(db, FixtureDaemon([block]))

    response = run(session.sapling_get_block_range(
        block['height'], block['height']))

    assert response['success'] is True
    assert response['complete'] is True
    assert response['empty'] is True
    assert response['height_count'] == 1
    assert response['block_count'] == 0
    assert response['sapling_tx_count'] == 0
    assert response['block_hashes'] == [
        {'height': block['height'], 'block_hash': block['hash']}
    ]
    assert response['blocks'] == []
    assert response['error'] is None


class FailingDaemon:

    async def block_hex_hashes(self, start_height, count):
        raise DaemonError('daemon unavailable')


def test_get_block_range_daemon_failure_is_not_complete():
    db = make_sapling_db()
    session = make_session(db, FailingDaemon())

    response = run(session.sapling_get_block_range(10, 12))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['height_count'] == 3
    assert response['block_hashes'] == []
    assert response['blocks'] == []
    assert response['error']['type'] == 'daemon_error'


def test_get_block_range_invalid_range_is_structured():
    db = make_sapling_db()
    session = make_session(db, FixtureDaemon([]))

    response = run(session.sapling_get_block_range(20, 19))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['height_count'] == 0
    assert response['error']['type'] == 'invalid_range'


class PartialHashDaemon:

    async def block_hex_hashes(self, start_height, count):
        return ['11' * 32]


def test_get_block_range_partial_hash_response_is_not_complete():
    db = make_sapling_db()
    session = make_session(db, PartialHashDaemon())

    response = run(session.sapling_get_block_range(10, 11))

    assert response['success'] is False
    assert response['complete'] is False
    assert response['empty'] is False
    assert response['height_count'] == 2
    assert response['block_hashes'] == [
        {'height': 10, 'block_hash': '11' * 32}
    ]
    assert response['blocks'] == []
    assert response['error']['type'] == 'missing_block_hash'
    assert response['error']['expected_count'] == 2
    assert response['error']['actual_count'] == 1


def test_get_block_range_index_incomplete_is_not_complete():
    block = load_block_fixture('pivx_mainnet_5057529.json')
    db = make_sapling_db()
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


def test_empty_blocks_do_not_consume_sapling_positions():
    db = make_sapling_db()
    first = b'f' * 32
    second = b'g' * 32

    db.flush_sapling_data(db.utxo_db, [], [(first, b'a' * 32, 0, 200)], [])
    db.flush_sapling_data(db.utxo_db, [], [], [])
    db.flush_sapling_data(db.utxo_db, [], [(second, b'b' * 32, 0, 202)], [])

    assert db.get_commitment_position_info(first)[3] == 0
    assert db.get_commitment_position_info(second)[3] == 1
    assert db.sapling_output_count == 2


def test_sapling_positions_remain_stable_across_restart():
    db = make_sapling_db()
    commitments = [bytes([n]) * 32 for n in range(3)]
    db.db_height = 101
    db.flush_sapling_data(
        db.utxo_db,
        [],
        [(commitments[0], b'a' * 32, 0, 100),
         (commitments[1], b'b' * 32, 1, 100),
         (commitments[2], b'c' * 32, 0, 101)],
        [],
    )
    db.write_utxo_state(db.utxo_db)

    restarted = make_sapling_db()
    restarted.utxo_db = db.utxo_db
    restarted.read_utxo_state()

    assert restarted.sapling_output_count == 3
    assert [restarted.get_commitment_position_info(c)[3]
            for c in commitments] == [0, 1, 2]


def test_get_block_range_returns_canonical_output_order_with_positions():
    block = load_block_fixture('pivx_mainnet_5057529.json')
    db = make_sapling_db()
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
                    output.cmu.hex(),
                    tx_lib.hash_to_hex_str(tx.txid)
                    if hasattr(tx_lib, 'hash_to_hex_str')
                    else tx.txid[::-1].hex(),
                ))
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


def verify_indexed_witness(commitment_hex, position, path, root_hex):
    node = DB.sapling_leaf_hash(bytes.fromhex(commitment_hex))
    index = position
    for item in path:
        sibling = bytes.fromhex(item['hash'])
        if item['position'] == 'left':
            node = DB.sapling_parent_hash(sibling, node)
        else:
            node = DB.sapling_parent_hash(node, sibling)
        index >>= 1
    assert index == 0
    return node.hex() == root_hex


def test_sapling_witness_path_verifies_against_requested_anchor():
    db = make_sapling_db()
    db.db_height = 400
    commitments = [bytes([n]) * 32 for n in range(1, 5)]
    db.flush_sapling_data(
        db.utxo_db,
        [],
        [(commitment, bytes([40 + n]) * 32, 0, 400)
         for n, commitment in enumerate(commitments)],
        [],
    )
    root = DB.sapling_root_from_commitments(commitments)
    session = make_session(db, FixtureDaemon([]))

    witness = run(session.sapling_get_witness(2, root.hex()))

    assert witness['anchor'] == root.hex()
    assert witness['root'] == root.hex()
    assert witness['anchor_height'] == 400
    assert witness['position'] == 2
    assert witness['note_position'] == 2
    assert witness['commitment'] == commitments[2].hex()
    assert verify_indexed_witness(
        witness['commitment'], witness['position'], witness['path'],
        witness['root'])
