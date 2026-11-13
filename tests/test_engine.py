"""The verdict engine itself, when it is there (PLUMB_CHECK_BIN): its answers for the fixture blocks are
the ones stored beside them, and the chain switch changes what it should and nothing else."""
import os, subprocess, tempfile, unittest, json

import support


def run(name, chain):
    b = support.block(name)
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "in.txt"), os.path.join(d, "out.txt")
        with open(inp, "w") as f:
            for t in b["tx"][1:]:
                prev = ",".join(f'{v["prevout"]["scriptPubKey"]["hex"]}:{round(v["prevout"]["value"] * 1e8)}' for v in t["vin"])
                f.write(f'{t["txid"]} {t["hex"]} {prev}\n')
        env = dict(os.environ, PLUMB_CHECK_IN=inp, PLUMB_CHECK_OUT=out, PLUMB_CHECK_CHAIN=chain)
        r = subprocess.run([support.CHECKER, "--run_test=plumb_check_tests", "--log_level=error"], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-800:]
        with open(out) as f:
            return {row["txid"]: row for row in map(json.loads, f)}


@unittest.skipUnless(support.HAVE_CHECKER, "needs the engine (PLUMB_CHECK_BIN)")
class EngineTests(unittest.TestCase):
    def test_stored_answers_are_the_engines(self):
        for name, chain in (("blake2b-980000", "blake2b"), ("blake2b-981499", "blake2b"), ("sha256-975450-cut", "sha256")):
            self.assertEqual(run(name, chain), support.rows(name), name)

    def test_core_relays_a_large_op_return_on_sha256_only(self):
        b = support.block("sha256-975450-cut")
        big = [t["txid"] for t in b["tx"] if any(o["scriptPubKey"].get("type") == "nulldata" and len(o["scriptPubKey"]["hex"]) > 166 for o in t["vout"])]
        self.assertEqual(len(big), 4)
        sha, b2b = run("sha256-975450-cut", "sha256"), run("sha256-975450-cut", "blake2b")
        for txid in big:
            self.assertEqual(sha[txid]["v"]["core"]["reasons"], [])
            self.assertEqual(b2b[txid]["v"]["core"]["reasons"], ["scriptpubkey"])
            # Knots and Plumb cap an OP_RETURN at 83 and 42 bytes whatever the chain.
            self.assertTrue(sha[txid]["v"]["knots"]["reasons"])
            self.assertEqual(sha[txid]["v"]["knots"], b2b[txid]["v"]["knots"])
            self.assertEqual(sha[txid]["v"]["plumb"], b2b[txid]["v"]["plumb"])
        for txid in set(sha) - set(big):
            self.assertEqual(sha[txid]["v"], b2b[txid]["v"], txid)

    def test_the_default_is_the_blake2b_chain(self):
        b = support.block("blake2b-981499")
        self.assertEqual(run("blake2b-981499", ""), run("blake2b-981499", "blake2b"))


if __name__ == "__main__":
    unittest.main()
