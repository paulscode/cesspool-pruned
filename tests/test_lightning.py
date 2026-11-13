"""The Lightning graph: reading it from LND and Core Lightning into one shape, and finding how a
channel closed from the blocks graded here."""
import json, os, shutil, tempfile, unittest

import support
import fakenode

A, B, C, D = ("02" + c * 64 for c in "abcd")
POLICY = {"time_lock_delta": 80, "min_htlc": "1000", "fee_base_msat": "1000", "fee_rate_milli_msat": "250", "disabled": False, "max_htlc_msat": "990000000"}


def lnd_id(scid):
    h, t, o = (int(x) for x in scid.split("x"))
    return str((h << 40) | (t << 16) | o)


class ShapeTests(unittest.TestCase):
    def test_short_ids_both_ways(self):
        with support.cesspool() as ns:
            ln = ns.module("lightning")
            self.assertEqual(ln.scid_from_lnd("1066526278943375361"), "970000x10x1")
            self.assertEqual(ln.scid_to_lnd("970000x10x1"), 1066526278943375361)
            self.assertEqual(ln.scid_parse("0970000x010x1"), "970000x10x1")
            self.assertEqual(ln.scid_parse("1066526278943375361"), "970000x10x1")
            self.assertEqual(ln.scid_parse(1066526278943375361), "970000x10x1")
            for bad in ("", "970000x10", "x", "970000x99999999x1", "abc", None, "12"):
                self.assertIsNone(ln.scid_parse(bad), bad)
            self.assertEqual(ln.scid_height("970000x10x1"), 970000)

    def test_an_alias_is_only_ever_text(self):
        with support.cesspool() as ns:
            ln = ns.module("lightning")
            self.assertEqual(ln.clean_alias("tor‮only\x07node\n"), "toronlynode")
            self.assertEqual(ln.clean_alias("<img src=x onerror=alert(1)>"), "<img src=x onerror=alert(1)>")  # kept as text; the pages escape it
            self.assertEqual(len(ln.clean_alias("x" * 500)), 40)
            self.assertEqual(ln.clean_alias(None), "")
            n = ln.normal_node(A.upper(), "ok", "javascript:alert(1)", ["1.2.3.4:9735", "bad\x00addr"], "7")
            self.assertEqual((n["pub"], n["color"], n["addrs"], n["upd"]), (A, "", ["1.2.3.4:9735"], 7))
            self.assertIsNone(ln.normal_node("not a key", "x", "#ffffff", [], 0))
            self.assertIsNone(ln.normal_channel("1x1x1", A, "nope", 1, None, None, None, 0))
            self.assertIsNone(ln.normal_channel("1x1x1", A, B, 1, "<script>:0", None, None, 0)["point"])

    def test_a_reply_is_read_in_pieces(self):
        with support.cesspool() as ns:
            ln = ns.module("lightning")
            doc = json.dumps({"nodes": [{"n": i, "s": "]}," * 3} for i in range(50)], "other": [1, 2], "edges": [{"e": i} for i in range(30)], "tail": 1}).encode()
            for size in (1, 7, 64, 100000):
                pos = [0]

                def read(n, size=size):
                    chunk = doc[pos[0]:pos[0] + size]
                    pos[0] += size
                    return chunk

                got = list(ln.stream_arrays(read, ("nodes", "edges")))
                self.assertEqual([x["n"] for k, x in got if k == "nodes"], list(range(50)), size)
                self.assertEqual([x["e"] for k, x in got if k == "edges"], list(range(30)), size)

    def test_a_reply_cut_short_is_an_error_not_a_small_graph(self):
        with support.cesspool() as ns:
            ln = ns.module("lightning")
            doc = b'{"nodes":[{"a":1},{"a":2},{"a":'
            pos = [0]

            def read(n):
                chunk = doc[pos[0]:pos[0] + 8]
                pos[0] += 8
                return chunk

            with self.assertRaises(ln.Unavailable):
                list(ln.stream_arrays(read, ("nodes",)))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="ln-test-")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl command for LND's certificate")
    def test_lnd_over_tls_with_its_own_certificate(self):
        edges = [{"channel_id": lnd_id("970000x10x1"), "chan_point": "ab" * 32 + ":1", "last_update": 5, "node1_pub": A, "node2_pub": B,
                  "capacity": "5000000", "node1_policy": POLICY, "node2_policy": None}]
        nodes = [{"pub_key": A, "alias": "alice", "color": "#ff9900", "addresses": [{"network": "tcp", "addr": "203.0.113.5:9735"}], "last_update": 5}]
        info = {"identity_pubkey": A, "alias": "alice", "color": "#ff9900", "version": "0.21", "block_height": 100, "block_hash": "00", "synced_to_chain": True, "synced_to_graph": False}
        lnd = fakenode.Lnd(self.work, info, nodes, edges)
        try:
            with support.cesspool({"CESSPOOL_LN_BACKEND": "lnd", "CESSPOOL_LND_REST_URL": lnd.url, "CESSPOOL_LND_TLS_CERT": lnd.cert, "CESSPOOL_LND_MACAROON": lnd.macaroon}) as ns:
                ln = ns.module("lightning")
                c = ln.client(ln.backend())
                me = c.info()
                self.assertEqual((me["pub"], me["chain_synced"], me["graph_synced"]), (A, True, False))
                got = list(c.graph())
                self.assertEqual([k for k, _ in got], ["node", "channel"])
                ch = got[1][1]
                self.assertEqual((ch["id"], ch["cap"], ch["point"], ch["pa"], ch["pb"]), ("970000x10x1", 5000000, "ab" * 32 + ":1", [1000, 250, 80, 1000, 990000000, 0], None))
            # Another certificate, as an impostor at the same address would present: refused.
            other = tempfile.mkdtemp(prefix="ln-test-", dir=self.work)
            fakenode.Lnd(other, info, nodes, edges).close()
            with support.cesspool({"CESSPOOL_LN_BACKEND": "lnd", "CESSPOOL_LND_REST_URL": lnd.url, "CESSPOOL_LND_TLS_CERT": os.path.join(other, "tls.cert"),
                                   "CESSPOOL_LND_MACAROON": lnd.macaroon}) as ns:
                ln = ns.module("lightning")
                with self.assertRaises(ln.Unavailable):
                    ln.client(ln.backend()).info()
            # A wallet that does not exist yet has no macaroon.
            with support.cesspool({"CESSPOOL_LN_BACKEND": "lnd", "CESSPOOL_LND_REST_URL": lnd.url, "CESSPOOL_LND_TLS_CERT": lnd.cert,
                                   "CESSPOOL_LND_MACAROON": os.path.join(self.work, "none.macaroon")}) as ns:
                ln = ns.module("lightning")
                with self.assertRaises(ln.Unavailable) as e:
                    ln.client(ln.backend()).info()
                self.assertIn("macaroon", str(e.exception))
        finally:
            lnd.close()

    def test_core_lightning_merges_the_two_directions(self):
        half = lambda src, dst, scid, active=True, base=1000: {
            "source": src, "destination": dst, "short_channel_id": scid, "amount_msat": 5000000000, "active": active, "last_update": 9,
            "base_fee_millisatoshi": base, "fee_per_millionth": 10, "delay": 34, "htlc_minimum_msat": 0, "htlc_maximum_msat": "4950000000msat"}
        chans = [half(A, B, "800000x1x0"), half(B, A, "800000x1x0", active=False, base=5), half(C, D, "800001x2x1")]
        nodes = [{"nodeid": A, "alias": "alice", "color": "ff9900", "last_timestamp": 3, "addresses": [{"type": "ipv6", "address": "2001:db8::1", "port": 9735}]},
                 {"nodeid": B}]
        cln = fakenode.Cln(os.path.join(self.work, "lightning-rpc"), {"id": A, "alias": "alice", "color": "ff9900", "version": "v26.06", "blockheight": 800010}, nodes, chans)
        try:
            with support.cesspool({"CESSPOOL_LN_BACKEND": "cln", "CESSPOOL_CLN_SOCKET": cln.path}) as ns:
                ln = ns.module("lightning")
                c = ln.client(ln.backend())
                me = c.info()
                self.assertEqual((me["pub"], me["color"], me["height"]), (A, "#ff9900", 800010))
                got = list(c.graph())
                ns_ = {x["pub"]: x for k, x in got if k == "node"}
                self.assertEqual(ns_[A]["addrs"], ["[2001:db8::1]:9735"])
                self.assertEqual(ns_[B]["alias"], "")
                cs = {x["id"]: x for k, x in got if k == "channel"}
                both = cs["800000x1x0"]
                self.assertEqual((both["a"], both["b"], both["cap"]), (A, B, 5000000))  # millisatoshi to satoshi
                self.assertEqual(both["pa"], [1000, 10, 34, 0, 4950000000, 0])
                self.assertEqual(both["pb"], [5, 10, 34, 0, 4950000000, 1])
                one = cs["800001x2x1"]
                self.assertEqual((one["a"], one["b"], one["pb"]), (C, D, None))
        finally:
            cln.close()

    def test_auto_follows_the_chain(self):
        fork, lnd = os.path.join(self.work, "fork"), os.path.join(self.work, "lnd")
        for d in (fork, lnd):
            os.makedirs(os.path.join(d, "data/chain/bitcoin/mainnet"))
            open(os.path.join(d, "data/chain/bitcoin/mainnet/readonly.macaroon"), "w").close()
        env = {"CESSPOOL_LN_BACKEND": "auto", "CESSPOOL_LN_FORK_DIR": fork, "CESSPOOL_LN_FORK_REST_URL": "https://10.21.21.66:8180",
               "CESSPOOL_LN_LND_DIR": lnd, "CESSPOOL_LN_LND_REST_URL": "https://10.21.21.9:8080", "CESSPOOL_LN_CLN_DIR": os.path.join(self.work, "nocln")}
        with support.cesspool(env, chain="blake2b") as ns:
            b = ns.module("lightning").backend()
            self.assertEqual((b["name"], b["url"]), ("Lightning Fork", "https://10.21.21.66:8180"))
        with support.cesspool(env, chain="sha256") as ns:
            b = ns.module("lightning").backend()
            self.assertEqual((b["name"], b["url"]), ("LND", "https://10.21.21.9:8080"))
        os.remove(os.path.join(lnd, "data/chain/bitcoin/mainnet/readonly.macaroon"))  # no wallet yet
        with support.cesspool(env, chain="sha256") as ns:
            self.assertIsNone(ns.module("lightning").backend())
        with support.cesspool({}, chain="sha256") as ns:
            self.assertIsNone(ns.module("lightning").backend())
            self.assertFalse(ns.module("lightning").shown())


def tx(txid, spends, kp=None, tier="clean", x=None):
    t = {"id": txid, "i": [{"p": p} for p in spends], "o": [], "k": tier}
    if kp:
        t["kp"] = kp
    if x:
        t["x"] = x
    return t


class CloseTests(unittest.TestCase):
    """scan() over transaction files written by hand: each kind of close, and a reorg taking one back."""

    def setup(self, ns, blocks):
        ln, local = ns.module("lightning"), ns.module("local")
        os.makedirs(local.TXD, exist_ok=True)
        index = {}
        for h, txs in blocks.items():
            with open(f"{local.TXD}/{h}.json", "w") as f:
                json.dump({"h": h, "hash": f"hash{h}", "t": 1000 + h, "cb": {"id": "cb%d" % h}, "tx": txs}, f)
            index[h] = {"_m": 1.0}
        return ln, index

    def channels(self, ln, chans):
        ln._write(ln.CHANNELS, {scid: {"a": A, "b": B, "c": 1000, "p": point, "s": "open", "f": 1, "l": 9e9} for scid, point in chans.items()})

    def test_each_kind_of_close(self):
        with support.cesspool() as ns:
            ln, index = self.setup(ns, {
                100: [tx("t-mutual", ["fund1:0"], "two-of-two"), tx("t-force", ["fund2:1"], "ln-commitment"), tx("t-revoked", ["fund3:0"], "ln-commitment")],
                101: [tx("t-sweep", ["t-force:0"], "ln-to-local"), tx("t-penalty", ["t-revoked:0", "other:5"], "ln-penalty"),
                      tx("t-refused", ["x:0"], "ln-anchor-sweep", "gray", {"core": [], "knots": ["bare-datacarrier"], "plumb": ["bare-datacarrier"]})],
            })
            self.channels(ln, {"1x1x0": "fund1:0", "2x1x1": "fund2:1", "3x1x0": "fund3:0", "4x1x0": "fund4:0"})
            self.assertTrue(ln.scan(index))
            ch = json.load(open(ln.CHANNELS))
            self.assertEqual((ch["1x1x0"]["s"], ch["1x1x0"]["x"]["k"], ch["1x1x0"]["x"]["t"], ch["1x1x0"]["x"]["h"]), ("closed", "mutual", "t-mutual", 100))
            self.assertEqual(ch["2x1x1"]["x"]["k"], "force")
            # The penalty is in block 101, which is read before the close it punishes in block 100.
            self.assertEqual((ch["3x1x0"]["x"]["k"], ch["3x1x0"]["x"]["pen"], ch["3x1x0"]["x"]["penh"]), ("penalty", "t-penalty", 101))
            self.assertEqual(ch["4x1x0"]["s"], "open")
            total, flagged, read = ln.filters(index)
            self.assertEqual(read, 2)
            self.assertEqual(total["ln-commitment"][0], 2)
            self.assertEqual(total["ln-anchor-sweep"], [1, 0, 1, 1, 1, 0])
            self.assertEqual([f[1] for f in flagged], ["t-refused"])
            self.assertFalse(ln.scan(index))  # nothing new: nothing read again

    def test_a_reorg_takes_a_close_back(self):
        with support.cesspool() as ns:
            ln, index = self.setup(ns, {100: [tx("t-force", ["fund2:1"], "ln-commitment")], 101: [tx("t-penalty", ["t-force:0"], "ln-penalty")]})
            self.channels(ln, {"2x1x1": "fund2:1"})
            ln.scan(index)
            self.assertEqual(json.load(open(ln.CHANNELS))["2x1x1"]["x"]["k"], "penalty")
            # Block 101 is replaced by one without the penalty: the close stands, as a forced close.
            local = ns.module("local")
            with open(f"{local.TXD}/101.json", "w") as f:
                json.dump({"h": 101, "hash": "other", "t": 1, "cb": {"id": "cb"}, "tx": []}, f)
            index[101] = {"_m": 2.0}
            ln.scan(index)
            x = json.load(open(ln.CHANNELS))["2x1x1"]["x"]
            self.assertEqual((x["k"], x.get("pen")), ("force", None))
            # Block 100 goes too: the channel is open again.
            del index[100]
            ln.scan(index)
            self.assertEqual(json.load(open(ln.CHANNELS))["2x1x1"]["s"], "open")

    def test_a_close_with_no_script_to_go_by_is_read_from_the_transaction(self):
        with support.cesspool() as ns:
            ln = ns.module("lightning")
            commitment = {"locktime": 0x20abcdef, "vin": [{"sequence": 0x80123456}]}
            cooperative = {"locktime": 981000, "vin": [{"sequence": 0xfffffffd}]}
            self.assertEqual(ln.close_kind({"kp": "ln-commitment"}, None), "force")
            self.assertEqual(ln.close_kind({"kp": "two-of-two"}, None), "mutual")
            self.assertEqual(ln.close_kind({}, commitment), "force")       # a taproot channel's commitment
            self.assertEqual(ln.close_kind({}, cooperative), "mutual")
            self.assertEqual(ln.close_kind({}, None), "unknown")           # the stored block is gone: never guessed

    def test_a_channel_is_checked_against_the_block_its_id_names(self):
        with support.cesspool() as ns:
            ln, index = self.setup(ns, {100: [tx("funding-tx", ["a:0"]), tx("t2", ["b:0"])]})
            local = ns.module("local")
            d = json.load(open(f"{local.TXD}/100.json"))
            d["tx"][0]["o"] = [{"a": 7}, {"a": 1000}]
            json.dump(d, open(f"{local.TXD}/100.json", "w"))
            self.channels(ln, {"100x1x1": "funding-tx:1", "100x1x0": "funding-tx:0", "100x2x0": "funding-tx:0", "50x1x0": "zz:0"})
            ln.scan(index)
            ch = json.load(open(ln.CHANNELS))
            self.assertEqual(ch["100x1x1"]["v"], 1)      # the right transaction, holding the capacity (1000)
            self.assertEqual(ch["100x1x0"]["v"], 0)      # the right transaction, another amount
            self.assertEqual(ch["100x2x0"]["v"], 0)      # the id names another transaction
            self.assertNotIn("v", ch["50x1x0"])          # block 50 is not graded here: nothing to check against

    def test_core_lightning_channels_get_their_funding_from_graded_blocks(self):
        with support.cesspool() as ns:
            ln, index = self.setup(ns, {100: [tx("funding-tx", ["a:0"]), tx("t2", ["b:0"])], 101: [tx("closer", ["funding-tx:1"], "two-of-two")]})
            self.channels(ln, {"100x1x1": None, "100x9x0": None, "50x1x0": None})
            ln.scan(index)
            ch = json.load(open(ln.CHANNELS))
            self.assertEqual(ch["100x1x1"]["p"], "funding-tx:1")  # transaction 1 of block 100 (0 is the coinbase)
            self.assertIsNone(ch["100x9x0"]["p"])                # no such transaction in the block
            self.assertIsNone(ch["50x1x0"]["p"])                 # block 50 is not graded here
            self.assertEqual(ch["100x1x1"]["x"]["k"], "mutual")


if __name__ == "__main__":
    unittest.main()
