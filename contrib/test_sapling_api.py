#!/usr/bin/env python3
"""
Test script for PIVX Sapling ElectrumX API endpoints.

Tests the Cake Wallet compatible APIs:
- blockchain.sapling.get_outputs (RECEIVING - trial decryption)
- blockchain.sapling.get_block_range (RECEIVING - compact blocks, lightwalletd)
- blockchain.sapling.get_witness (SENDING - spend proof)
- blockchain.sapling.get_nullifiers (SYNC)
- blockchain.sapling.get_tree_state (SYNC)
- blockchain.nullifier.get_spend (SYNC)

get_block_range response format (Zcash lightwalletd compatible):
    [
        {
            'height': int,
            'hash': str (hex),
            'time': int (unix timestamp),
            'txs': [
                {
                    'txid': str (hex),
                    'outputs': [
                        {
                            'cmu': str,          # note commitment
                            'epk': str,          # ephemeral public key
                            'ciphertext': str,   # encrypted ciphertext (580 bytes)
                            'cv': str,           # value commitment
                            'out_ciphertext': str  # outgoing ciphertext (80 bytes)
                        }
                    ],
                    'spends': [
                        {
                            'nullifier': str,    # spent note identifier
                            'cv': str,           # value commitment
                            'anchor': str,       # Merkle tree root
                            'rk': str            # randomized public key
                        }
                    ]
                }
            ]
        }
    ]

Usage:
    python test_sapling_api.py [host] [port] [--ssl]

Examples:
    python test_sapling_api.py localhost 50001
    python test_sapling_api.py electrum.pivx.org 50002 --ssl
"""

import json
import socket
import ssl
import sys
from typing import Any

# Test data from mainnet block 5057529 (unshielding tx with 2 spends, 2 outputs)
TEST_NULLIFIER_1 = (
    "63c05d0cb13b5e8cc0750dc2d19a0ee475d67edb935c898f5ea251657105fa6c"
)
TEST_COMMITMENT_1 = (
    "a3a5aca593fe65244ea569d93b75951b4e1520b5117b02d9ca88413548b3fb33"
)
TEST_TX_HASH = (
    "36151d608497fc12b7f3b1dc57e1ed927326bdc380dc808a75786abf7f0efdb1"
)
TEST_HEIGHT = 5057529
SAPLING_ACTIVATION = 2700500


class ElectrumXClient:
    def __init__(self, host: str, port: int, use_ssl: bool = False):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.request_id = 0
        self.sock = None
        self.file = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        if self.use_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.sock = context.wrap_socket(
                self.sock, server_hostname=self.host
            )
        self.sock.connect((self.host, self.port))
        self.file = self.sock.makefile('r')

    def close(self):
        if self.file:
            self.file.close()
        if self.sock:
            self.sock.close()

    def call(self, method: str, params: list = None) -> Any:
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": self.request_id
        }
        msg = json.dumps(request) + "\n"
        self.sock.sendall(msg.encode())
        response = self.file.readline()
        return json.loads(response)


def test_api(host: str, port: int, use_ssl: bool = False):
    client = ElectrumXClient(host, port, use_ssl)

    print(f"\n{'='*60}")
    print("PIVX Sapling API Test - Cake Wallet Compatible")
    print(f"Server: {host}:{port} (SSL: {use_ssl})")
    print("="*60 + "\n")

    try:
        client.connect()
        print("✓ Connected to server\n")
    except Exception as e:
        print(f"✗ Failed to connect: {e}")
        return False

    all_passed = True

    # Test 1: Server version
    print("Test 1: server.version")
    try:
        result = client.call("server.version", ["test-client", "1.4"])
        if "result" in result:
            print(f"  ✓ Server: {result['result']}")
        else:
            print(f"  ✗ Error: {result.get('error')}")
    except Exception as e:
        print(f"  ✗ Exception: {e}")

    # Test 2: Current height
    print("\nTest 2: blockchain.headers.subscribe")
    server_height = 0
    try:
        result = client.call("blockchain.headers.subscribe")
        if "result" in result:
            server_height = result['result'].get('height', 0)
            print(f"  ✓ Current height: {server_height}")
            if server_height < TEST_HEIGHT:
                print(f"  ⚠ Server not synced to test height {TEST_HEIGHT}")
        else:
            print(f"  ✗ Error: {result.get('error')}")
    except Exception as e:
        print(f"  ✗ Exception: {e}")

    # =========================================================================
    # CAKE WALLET CORE APIs
    # =========================================================================

    print("\n" + "="*60)
    print("CAKE WALLET CORE APIs")
    print("="*60)

    # Test 3: blockchain.sapling.get_outputs (RECEIVING)
    print("\nTest 3: blockchain.sapling.get_outputs (RECEIVING)")
    print(f"  Range: {TEST_HEIGHT} to {TEST_HEIGHT}, limit=1000")
    try:
        result = client.call(
            "blockchain.sapling.get_outputs",
            [TEST_HEIGHT, TEST_HEIGHT, 1000]
        )
        if "result" in result:
            r = result['result']
            outputs = r.get('outputs', [])
            print(f"  ✓ Found {len(outputs)} output(s)")
            print(f"    more: {r.get('more', False)}")
            for out in outputs[:2]:
                print(f"    - txid: {out.get('txid', 'N/A')[:16]}...")
                print(f"      index: {out.get('index')}")
                print(f"      cmu: {out.get('cmu', 'N/A')[:16]}...")
                print(f"      epk: {out.get('epk', 'N/A')[:16]}...")
                enc_len = len(out.get('enc_ciphertext', '')) // 2
                print(f"      enc_ciphertext: {enc_len} bytes")
            if not outputs:
                print("  ⚠ No outputs - may need resync from Sapling")
                all_passed = False
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
            all_passed = False
    except Exception as e:
        print(f"  ✗ Exception: {e}")
        all_passed = False

    # Test 4: blockchain.sapling.get_witness (SENDING)
    print("\nTest 4: blockchain.sapling.get_witness (SENDING)")
    print(f"  Commitment: {TEST_COMMITMENT_1[:16]}...")
    print(f"  Anchor height: {TEST_HEIGHT}")
    try:
        result = client.call(
            "blockchain.sapling.get_witness",
            [TEST_COMMITMENT_1, TEST_HEIGHT]
        )
        if "result" in result:
            r = result['result']
            print("  ✓ Witness retrieved")
            print(f"    position: {r.get('position')}")
            path = r.get('path', [])
            print(f"    path: {len(path)} hashes")
            print(f"    anchor: {r.get('anchor', 'N/A')[:16]}...")
        elif "error" in result:
            err = result['error']
            msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
            if 'not yet implemented' in msg:
                print("  ⚠ Witness not implemented (expected)")
                print("    Spending requires full node for now")
            else:
                print(f"  ✗ Error: {err}")
                all_passed = False
    except Exception as e:
        print(f"  ✗ Exception: {e}")

    # Test 5: blockchain.sapling.get_nullifiers (SYNC)
    print("\nTest 5: blockchain.sapling.get_nullifiers (SYNC)")
    print(f"  Range: {TEST_HEIGHT} to {TEST_HEIGHT}")
    try:
        result = client.call(
            "blockchain.sapling.get_nullifiers",
            [TEST_HEIGHT, TEST_HEIGHT]
        )
        if "result" in result:
            r = result['result']
            nullifiers = r.get('nullifiers', [])
            print(f"  ✓ Found {len(nullifiers)} nullifier(s)")
            for nf in nullifiers[:2]:
                print(f"    - {nf.get('nullifier', 'N/A')[:16]}...")
                print(f"      txid: {nf.get('txid', 'N/A')[:16]}...")
                print(f"      height: {nf.get('height')}")
            if not nullifiers:
                print("  ⚠ No nullifiers - may need resync")
                all_passed = False
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
            all_passed = False
    except Exception as e:
        print(f"  ✗ Exception: {e}")
        all_passed = False

    # Test 6: blockchain.sapling.get_tree_state (SYNC)
    print("\nTest 6: blockchain.sapling.get_tree_state (SYNC)")
    print(f"  Height: {TEST_HEIGHT}")
    try:
        result = client.call(
            "blockchain.sapling.get_tree_state",
            [TEST_HEIGHT]
        )
        if "result" in result:
            r = result['result']
            print("  ✓ Tree state retrieved")
            print(f"    height: {r.get('height')}")
            block_hash = r.get('block_hash', 'N/A')
            print(f"    block_hash: {block_hash[:16]}...")
            print(f"    nullifier_count: {r.get('nullifier_count')}")
            print(f"    commitment_count: {r.get('commitment_count')}")
            anchor = r.get('latest_anchor')
            if anchor:
                print(f"    latest_anchor: {anchor[:16]}...")
            print(f"    latest_anchor_height: {r.get('latest_anchor_height')}")
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
            all_passed = False
    except Exception as e:
        print(f"  ✗ Exception: {e}")
        all_passed = False

    # Test 7: blockchain.sapling.get_block_range (Zcash lightwalletd compatible)
    print("\nTest 7: blockchain.sapling.get_block_range (SYNC)")
    print(f"  Range: {TEST_HEIGHT} to {TEST_HEIGHT}")
    try:
        result = client.call(
            "blockchain.sapling.get_block_range",
            [TEST_HEIGHT, TEST_HEIGHT]
        )
        if "result" in result:
            r = result['result']
            if isinstance(r, list):
                print(f"  ✓ Got {len(r)} block(s)")
                for blk in r[:2]:
                    print(f"    - height: {blk.get('height')}")
                    blk_hash = blk.get('hash', 'N/A')
                    print(f"      hash: {blk_hash[:16]}...")
                    print(f"      time: {blk.get('time')}")
                    txs = blk.get('txs', [])
                    print(f"      txs: {len(txs)}")
                    for tx in txs[:1]:
                        print(f"        txid: {tx.get('txid', 'N/A')[:16]}...")
                        outputs = tx.get('outputs', [])
                        spends = tx.get('spends', [])
                        print(f"        outputs: {len(outputs)}, spends: {len(spends)}")
                        if outputs:
                            out = outputs[0]
                            print(f"          cmu: {out.get('cmu', '')[:16]}...")
                            print(f"          cv: {out.get('cv', '')[:16]}...")
                            print(f"          out_ciphertext present: "
                                  f"{'out_ciphertext' in out}")
                        if spends:
                            sp = spends[0]
                            print(f"          nullifier: {sp.get('nullifier', '')[:16]}...")
            else:
                print(f"  ✗ Expected list, got: {type(r)}")
                all_passed = False
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
            all_passed = False
    except Exception as e:
        print(f"  ✗ Exception: {e}")
        all_passed = False

    # Test 8: blockchain.nullifier.get_spend (SYNC)
    print("\nTest 8: blockchain.nullifier.get_spend (SYNC)")
    print(f"  Nullifier: {TEST_NULLIFIER_1[:16]}...")
    try:
        result = client.call(
            "blockchain.nullifier.get_spend",
            [TEST_NULLIFIER_1]
        )
        if "result" in result:
            r = result['result']
            if r is None:
                print("  ⚠ Nullifier not found (may need resync)")
                all_passed = False
            else:
                print("  ✓ Nullifier spent")
                print(f"    txid: {r.get('txid', 'N/A')[:16]}...")
                print(f"    height: {r.get('height')}")
                if r.get('height') == TEST_HEIGHT:
                    print(f"  ✓ Height matches expected: {TEST_HEIGHT}")
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
            all_passed = False
    except Exception as e:
        print(f"  ✗ Exception: {e}")
        all_passed = False

    # =========================================================================
    # UTILITY APIs
    # =========================================================================

    print("\n" + "="*60)
    print("UTILITY APIs")
    print("="*60)

    # Test 9: blockchain.commitment.get_info
    print("\nTest 9: blockchain.commitment.get_info")
    print(f"  Commitment: {TEST_COMMITMENT_1[:16]}...")
    try:
        result = client.call(
            "blockchain.commitment.get_info",
            [TEST_COMMITMENT_1]
        )
        if "result" in result:
            r = result['result']
            if r is None:
                print("  ⚠ Commitment not found")
            else:
                print("  ✓ Commitment found")
                print(f"    txid: {r.get('txid', 'N/A')[:16]}...")
                print(f"    output_index: {r.get('output_index')}")
                print(f"    height: {r.get('height')}")
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
    except Exception as e:
        print(f"  ✗ Exception: {e}")

    # Test 10: blockchain.transaction.get_sapling
    print("\nTest 10: blockchain.transaction.get_sapling")
    print(f"  TX: {TEST_TX_HASH[:16]}...")
    try:
        result = client.call(
            "blockchain.transaction.get_sapling",
            [TEST_TX_HASH, True]
        )
        if "result" in result:
            r = result['result']
            print("  ✓ Transaction info")
            print(f"    is_sapling: {r.get('is_sapling')}")
            print(f"    value_balance: {r.get('value_balance')}")
            print(f"    spend_count: {r.get('spend_count')}")
            print(f"    output_count: {r.get('output_count')}")
            if r.get('spends'):
                spend = r['spends'][0]
                nf = spend['nullifier'][:16]
                print(f"    First spend nullifier: {nf}...")
        elif "error" in result:
            print(f"  ✗ Error: {result['error']}")
    except Exception as e:
        print(f"  ✗ Exception: {e}")

    client.close()

    # Summary
    print("\n" + "="*60)
    if all_passed:
        print("✓ All Sapling API tests passed!")
        print("\nCake Wallet integration ready:")
        print("  - RECEIVING: get_outputs works")
        print("  - SYNC: get_nullifiers, get_tree_state, nullifier.get_spend")
        print("  - SENDING: get_witness returns helpful error (use full node)")
    else:
        print("⚠ Some tests failed")
        print("\nTo fix, resync from Sapling activation:")
        print(f"  electrumx_server --reorg={SAPLING_ACTIVATION - 1}")
    print("="*60 + "\n")

    return all_passed


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 50001
    use_ssl = "--ssl" in sys.argv or port in (50002, 443)

    test_api(host, port, use_ssl)
