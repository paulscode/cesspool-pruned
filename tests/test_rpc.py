"""The JSON-RPC client and what it learns from the node: credentials, errors, and which chain."""
import os, tempfile, unittest

import support
import fakenode
from cesspool import rpc


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.chain = fakenode.Chain({980000: support.block("blake2b-980000")})
        self.nodes = []

    def tearDown(self):
        for n in self.nodes:
            n.close()

    def node(self, **kw):
        n = fakenode.Node(self.chain, **kw)
        self.nodes.append(n)
        return n

    def test_user_and_password(self):
        n = self.node(user="alice", password="secret")
        self.assertEqual(rpc.Client(n.url, "alice", "secret").getblockchaininfo()["blocks"], 980000)
        with self.assertRaises(rpc.Unreachable):
            rpc.Client(n.url, "alice", "wrong").getblockchaininfo()

    def test_cookie_is_read_again_after_the_node_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            n = self.node(cookie=os.path.join(d, ".cookie"))
            c = rpc.Client(n.url, cookie=d)  # the data directory: the cookie is found under it
            self.assertEqual(c.getbestblockhash(), self.chain.hash_at(980000))
            n.rotate_cookie()
            self.assertEqual(c.getbestblockhash(), self.chain.hash_at(980000))

    def test_no_credentials_at_all_says_so(self):
        n = self.node(user="a", password="b")
        with self.assertRaises(rpc.Unreachable) as e:
            rpc.Client(n.url, cookie="/nonexistent/.cookie").getbestblockhash()
        self.assertIn("cookie", str(e.exception))

    def test_nothing_listening(self):
        with self.assertRaises(rpc.Unreachable):
            rpc.Client("http://127.0.0.1:9", "a", "b", timeout=3).getbestblockhash()

    def test_pruned_block_is_told_apart_from_other_errors(self):
        self.chain.prune = 980001
        c = rpc.Client(self.node(user="a", password="b").url, "a", "b")
        with self.assertRaises(rpc.RpcError) as e:
            c.getblock(self.chain.hash_at(980000), 3)
        self.assertTrue(e.exception.pruned)
        self.assertEqual(c.getblock(self.chain.hash_at(980000), 1)["height"], 980000)  # the proxy still serves these
        with self.assertRaises(rpc.RpcError) as e:
            c.getblockhash(990000)
        self.assertFalse(e.exception.pruned)

    def test_block_with_prevouts_is_the_stored_block(self):
        c = rpc.Client(self.node(user="a", password="b").url, "a", "b")
        self.assertEqual(c.getblock(self.chain.hash_at(980000), 3), support.block("blake2b-980000"))

    def test_batch_keeps_order_and_carries_errors(self):
        c = rpc.Client(self.node(user="a", password="b").url, "a", "b")
        got = c.batch([("getblockhash", [980000]), ("getblockhash", [990000]), ("getbestblockhash", [])])
        self.assertEqual(got[0], self.chain.hash_at(980000))
        self.assertIsInstance(got[1], rpc.RpcError)
        self.assertEqual(got[2], self.chain.hash_at(980000))

    def test_batch_falls_back_to_single_calls(self):
        c = rpc.Client(self.node(user="a", password="b", batches=False).url, "a", "b")
        got = c.batch([("getblockhash", [980000]), ("getblockhash", [990000])])
        self.assertEqual(got[0], self.chain.hash_at(980000))
        self.assertIsInstance(got[1], rpc.RpcError)


class ChainTests(unittest.TestCase):
    def detect(self, **kw):
        chain = fakenode.Chain({980000: support.block("blake2b-980000")}, **kw)
        n = fakenode.Node(chain, "a", "b")
        try:
            with support.cesspool() as ns:
                # The client from the same import of the package as local: its errors are the ones local catches.
                return ns.module("local").detect_chain(ns.module("rpc").Client(n.url, "a", "b"))
        finally:
            n.close()

    def test_blake2b_by_the_block_at_the_fork_height(self):
        self.assertEqual(self.detect(fork_hash=support.BLAKE2B_961640), "blake2b")

    def test_any_other_block_there_is_sha256(self):
        self.assertEqual(self.detect(fork_hash="00000000000000000001d82da6ecccf08e07afa383f9212b0e1b95cc72430c00"), "sha256")

    def test_undecided_below_the_fork_height(self):
        self.assertIsNone(self.detect(tip=900000))


if __name__ == "__main__":
    unittest.main()
