"""Rebuilding a pruned block from its bytes, the node's decoding and an Electrum server: the result
has to be the block `getblock <hash> 3` would have given, for everything cesspool reads."""
import os, unittest

import support
import fakenode


def checker_lines(b):
    return [f'{t["txid"]} {t["hex"]} ' + ",".join(f'{v["prevout"]["scriptPubKey"]["hex"]}:{round(v["prevout"]["value"] * 1e8)}' for v in t["vin"])
            for t in b["tx"][1:]]


# What cesspool reads from a stored block (process.py, classify.py, txdetail.py, miner.py, address.py).
BLOCK_KEYS = ("height", "hash", "time", "weight", "size", "previousblockhash")
TX_KEYS = ("txid", "hex", "size", "vsize", "weight", "locktime")


class Servers:
    def __init__(self, name, header_bytes, **electrum):
        self.block = support.block(name)
        h = self.block["height"]
        self.chain = fakenode.Chain({h: self.block}, prune=h + 1, header_bytes=header_bytes)
        self.node = fakenode.Node(self.chain, "a", "b")
        self.electrum = fakenode.Electrum(self.chain, **electrum)
        self.env = {"CESSPOOL_RPC_URL": self.node.url, "CESSPOOL_RPC_USER": "a", "CESSPOOL_RPC_PASS": "b",
                    "CESSPOOL_ELECTRUM_HOST": "127.0.0.1", "CESSPOOL_ELECTRUM_PORT": str(self.electrum.port)}

    def close(self):
        self.node.close()
        self.electrum.close()


class BytesTests(unittest.TestCase):
    def test_transaction_ids_are_recomputed_from_the_bytes(self):
        with support.cesspool() as ns:
            backfill = ns.module("backfill")
            for name in ("blake2b-981499", "sha256-975450-cut"):
                for t in support.block(name)["tx"]:
                    end, txid, ins, outs = backfill.parse_tx(bytes.fromhex(t["hex"]))
                    self.assertEqual(txid, t["txid"])
                    self.assertEqual(end, len(t["hex"]) // 2)
                    self.assertEqual(len(outs), len(t["vout"]))
                    self.assertEqual([s for s, _ in outs], [round(o["value"] * 1e8) for o in t["vout"]])
                    self.assertEqual([spk for _, spk in outs], [o["scriptPubKey"]["hex"] for o in t["vout"]])

    def test_a_block_splits_under_either_header_length(self):
        with support.cesspool() as ns:
            backfill = ns.module("backfill")
            b = support.block("blake2b-981499")
            ids = [t["txid"] for t in b["tx"]]
            for header in (80, 164):
                raw = bytes.fromhex(fakenode.Chain({b["height"]: b}, header_bytes=header).raw_block(b))
                self.assertEqual([t[0] for t in backfill.split_block(raw, ids)], [t["hex"] for t in b["tx"]])
            with self.assertRaises(backfill.Unusable):
                backfill.split_block(raw, ids[:-1] + ["00" * 32])  # a transaction the node does not list
            with self.assertRaises(backfill.Unusable):
                backfill.split_block(raw[:-10], ids)


class RebuildTests(unittest.TestCase):
    def rebuilt(self, name, header_bytes, verify=False, **electrum):
        s = Servers(name, header_bytes, **electrum)
        try:
            with support.cesspool(s.env, chain=name.split("-")[0]) as ns:
                backfill = ns.module("backfill")
                backfill.VERIFY, backfill.PAUSE = verify, 0
                got = backfill.rebuild(s.block["height"], s.block["hash"])
                return s.block, got, backfill.report(), s.electrum.requests
        finally:
            s.close()

    def same(self, orig, got):
        self.assertIsNotNone(got)
        self.assertEqual(checker_lines(got), checker_lines(orig))
        for k in BLOCK_KEYS:
            self.assertEqual(got.get(k), orig.get(k), k)
        for a, b in zip(orig["tx"], got["tx"]):
            for k in TX_KEYS:
                self.assertEqual(a[k], b[k], k)
            self.assertEqual(round(a.get("fee", 0) * 1e8), round(b.get("fee", 0) * 1e8))
            self.assertEqual(a["vout"], b["vout"])
            for x, y in zip(a["vin"], b["vin"]):
                for k in ("txid", "vout", "coinbase", "scriptSig", "txinwitness", "sequence"):
                    self.assertEqual(x.get(k), y.get(k), k)
                if "prevout" in x:
                    self.assertEqual(round(x["prevout"]["value"] * 1e8), round(y["prevout"]["value"] * 1e8))
                    for k in ("hex", "type", "address"):
                        self.assertEqual(x["prevout"]["scriptPubKey"].get(k), y["prevout"]["scriptPubKey"].get(k), k)

    def test_blake2b_block(self):
        orig, got, report, _ = self.rebuilt("blake2b-981499", 164)
        self.same(orig, got)
        self.assertEqual(report["state"], "ready")

    def test_sha256_block(self):
        orig, got, _, _ = self.rebuilt("sha256-975450-cut", 80)
        self.same(orig, got)

    def test_the_records_are_identical(self):
        if not support.HAVE_REORG_WATCH:
            self.skipTest("needs reorg-watch (REORG_WATCH_REAL)")
        orig, got, _, _ = self.rebuilt("blake2b-981499", 164)
        with support.cesspool() as ns:
            process = ns.module("process")
            rows = support.rows("blake2b-981499")
            self.assertEqual(process.process_block(orig, rows), process.process_block(got, rows))

    def test_a_transaction_that_does_not_hash_to_its_id_is_refused(self):
        # The fake Electrum server makes up the transactions these blocks spend from, so their bytes
        # cannot hash to the ids asked for: with the check on, nothing is rebuilt from them.
        _, got, report, _ = self.rebuilt("blake2b-981499", 164, verify=True)
        self.assertIsNone(got)
        self.assertEqual(report["state"], "failed")
        self.assertIn("different transaction", report["reason"])

    def test_a_server_that_lacks_a_transaction_stops_the_backfill(self):
        b = support.block("blake2b-981499")
        lost = b["tx"][1]["vin"][0]["txid"]
        _, got, report, _ = self.rebuilt("blake2b-981499", 164, missing=[lost])
        self.assertIsNone(got)
        self.assertEqual(report["state"], "failed")
        self.assertIn(lost[:16], report["reason"])

    def test_a_server_on_the_wrong_protocol_version_is_refused(self):
        _, got, report, _ = self.rebuilt("blake2b-981499", 164, protocol="1.2")
        self.assertIsNone(got)
        self.assertEqual(report["state"], "failed")

    def test_a_blake2b_server_at_1_8_is_accepted(self):
        orig, got, _, _ = self.rebuilt("blake2b-981499", 164, protocol="1.8")
        self.same(orig, got)

    def test_a_new_block_interrupts_and_the_work_is_kept(self):
        s = Servers("sha256-975450-cut", 80)
        try:
            with support.cesspool(s.env, chain="sha256") as ns:
                backfill = ns.module("backfill")
                backfill.VERIFY, backfill.PAUSE = False, 0
                backfill.ASK = 5
                calls = []
                got = backfill.rebuild(s.block["height"], s.block["hash"], interrupted=lambda: len(calls) >= 2 or calls.append(1))
                self.assertIsNone(got)
                self.assertTrue(os.path.exists(backfill._work_path(s.block["height"])))
                self.assertEqual(backfill.report()["state"], "ready")  # an interruption is not a failure
                before = s.electrum.requests
                got = backfill.rebuild(s.block["height"], s.block["hash"])
                self.assertIsNotNone(got)
                self.assertFalse(os.path.exists(backfill._work_path(s.block["height"])))
                first = before - 1  # one request was the version handshake
                self.assertLess(s.electrum.requests - before, first + (len(s.chain.spent) // 5))
        finally:
            s.close()

    def test_off_and_unconfigured_are_reported(self):
        with support.cesspool({"CESSPOOL_BACKFILL": "0", "CESSPOOL_ELECTRUM_HOST": "127.0.0.1"}) as ns:
            self.assertEqual(ns.module("backfill").report(), {"state": "off"})
            self.assertFalse(ns.module("backfill").usable())
        with support.cesspool() as ns:
            self.assertEqual(ns.module("backfill").report(), {"state": "none"})
            self.assertFalse(ns.module("backfill").usable())


if __name__ == "__main__":
    unittest.main()
