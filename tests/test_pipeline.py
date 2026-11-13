"""tools/update.py from end to end against a made-up node: the first run, a block the node has pruned,
a reorg, and the storage ceiling. These grade with the real engine, so they run only where it is
(PLUMB_CHECK_BIN), with reorg-watch for the pool names (REORG_WATCH_REAL)."""
import copy, gzip, importlib.util, json, os, sys, unittest

import support
import fakenode


def load_update():
    spec = importlib.util.spec_from_file_location("cesspool_update_tool", os.path.join(support.ROOT, "tools", "update.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(support.HAVE_CHECKER and support.HAVE_REORG_WATCH, "needs the engine (PLUMB_CHECK_BIN) and reorg-watch (REORG_WATCH_REAL)")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.low, self.high = support.block("blake2b-980000"), support.block("blake2b-981499")
        self.chain = fakenode.Chain({980000: self.low, 981499: self.high}, prune=981499, header_bytes=164, fork_hash=support.BLAKE2B_961640)
        self.node = fakenode.Node(self.chain, "a", "b")
        self.env = {"CESSPOOL_RPC_URL": self.node.url, "CESSPOOL_RPC_USER": "a", "CESSPOOL_RPC_PASS": "b", "PLUMB_CHECK_BIN": support.CHECKER,
                    "REORG_WATCH_REAL": os.environ["REORG_WATCH_REAL"]}
        if os.environ.get("PLUMB_FILTERS"):
            self.env["PLUMB_FILTERS"] = os.environ["PLUMB_FILTERS"]

    def tearDown(self):
        self.node.close()

    def run_update(self, ns, *args):
        update = load_update()
        argv = sys.argv
        sys.argv = ["update.py", "--out", ns.out, *args]
        try:
            update.main()
        finally:
            sys.argv = argv
        return update

    def records(self, ns):
        return sorted(int(n[:-5]) for n in os.listdir(os.path.join(ns.home, "blocks")))

    def test_first_run_then_nothing_to_do(self):
        with support.cesspool(self.env) as ns:
            self.run_update(ns)
            self.assertEqual(self.records(ns), [981499])
            cov = json.load(open(os.path.join(ns.home, "coverage.json")))
            self.assertEqual((cov["chain"], cov["start"], cov["top"], cov["pruned"], cov["pruneheight"], cov["blocked"], cov["filling"]),
                             ("blake2b", 981499, 981499, True, 981499, "pruned", False))
            status = json.load(open(os.path.join(ns.data, "status.json")))
            self.assertEqual((status["state"], status["blocks"]), ("grading", 1))
            self.assertIn("blocks 981499 to 981499", status["message"])
            # What was stored is the node's block, and the page is there.
            with gzip.open(os.path.join(ns.home, "raw", "981499.json.gz")) as f:
                self.assertEqual(json.load(f), self.high)
            self.assertTrue(os.path.exists(os.path.join(ns.out, "block", "981499", "index.html")))
            self.assertEqual(json.load(open(os.path.join(ns.out, "tip.json"))), {"h": 981499})
            asked = len(self.chain.calls)
            before = os.path.getmtime(os.path.join(ns.out, "index.html"))
            self.run_update(ns)                      # the same tip: no fetch, no build
            self.assertEqual(self.records(ns), [981499])
            self.assertNotIn("getblock", self.chain.calls[asked:])
            self.assertEqual(os.path.getmtime(os.path.join(ns.out, "index.html")), before)

    def test_an_archival_node_fills_history_from_the_node(self):
        self.chain.prune = 0
        self.chain.blocks[981498] = dict(copy.deepcopy(self.low), height=981498, hash="aa" * 32)   # a second block, just below the tip
        self.chain.by_hash["aa" * 32] = self.chain.blocks[981498]
        with support.cesspool(self.env) as ns:
            local = ns.module("local")
            local.RECENT = 1                          # the first run takes the tip alone
            self.run_update(ns)
            self.assertEqual(self.records(ns), [981499])
            self.assertTrue(local.pending())
            local.FILL_MOST = 1                       # one older block a run
            self.run_update(ns)
            self.assertEqual(self.records(ns), [981498, 981499])
            self.assertEqual(local.coverage()["source"], "node")

    def test_a_reorg_replaces_the_block(self):
        with support.cesspool(self.env) as ns:
            self.run_update(ns)
            first = json.load(open(os.path.join(ns.home, "blocks", "981499.json")))["hash"]
            other = dict(copy.deepcopy(self.high), hash="bb" * 32)
            other["tx"] = other["tx"][:5]             # the block that replaced it holds fewer transactions
            self.chain.by_hash["bb" * 32] = other
            self.chain.replaced[981499] = "bb" * 32
            self.run_update(ns)
            rec = json.load(open(os.path.join(ns.home, "blocks", "981499.json")))
            self.assertNotEqual(rec["hash"], first)
            self.assertEqual((rec["hash"], rec["ntx"]), ("bb" * 32, 5))
            gone = self.high["tx"][10]["txid"]
            with gzip.open(os.path.join(ns.out, "d", "i", gone[:3] + ".json.gz")) as f:
                self.assertNotIn(gone[3:16], json.load(f))   # its transactions left the index

    def test_the_ceiling_drops_the_oldest_block_and_the_pages_follow(self):
        self.chain.prune = 0
        with support.cesspool(self.env) as ns:
            local = ns.module("local")
            # Two blocks on file: the tip from the node, and an older one put there by hand.
            support.grade(ns, ["blake2b-980000"])
            local.RECENT = 1                          # after a long stop, the tip alone comes first
            self.run_update(ns)
            self.assertEqual(self.records(ns), [980000, 981499])
            self.assertTrue(os.path.exists(os.path.join(ns.out, "block", "980000", "index.html")))
            self.assertIn("since block 980000", open(os.path.join(ns.out, "about", "index.html")).read())
            sizes = local.footprint(ns.out)
            local.MAX_BYTES, local.KEEP_LEAST = sizes[981499] + 1000, 1   # room for the newer block only
            self.run_update(ns)
            self.assertEqual(self.records(ns), [981499])
            for gone in (os.path.join(ns.home, "raw", "980000.json.gz"), os.path.join(ns.home, "txd", "980000.json"),
                         os.path.join(ns.out, "block", "980000"), os.path.join(ns.out, "d", "b", "980000.json.gz"), os.path.join(ns.out, "api", "v1", "block", "980000.json")):
                self.assertFalse(os.path.exists(gone), gone)
            about = open(os.path.join(ns.out, "about", "index.html")).read()
            self.assertIn("since block 981499", about)
            self.assertIn("as many blocks as fit", about)
            self.assertEqual(json.load(open(os.path.join(ns.home, "coverage.json")))["blocked"], "budget")
            self.run_update(ns)                       # and it stays that way: the dropped block is not fetched back
            self.assertEqual(self.records(ns), [981499])

    def test_the_newest_blocks_are_never_dropped(self):
        self.chain.prune = 0
        with support.cesspool(self.env) as ns:
            local = ns.module("local")
            support.grade(ns, ["blake2b-980000"])
            local.RECENT = 1
            self.run_update(ns)
            local.MAX_BYTES = 1                       # far over, whatever is on file
            local.KEEP_LEAST = 2000                   # and both blocks are within this many of the top
            self.assertEqual(local.expire(ns.out, 981499), [])
            local.KEEP_LEAST = 100                    # now only the newer one is
            self.assertEqual(local.expire(ns.out, 981499), [980000])


if __name__ == "__main__":
    unittest.main()
