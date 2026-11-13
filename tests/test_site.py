"""The rendered site: what this build takes off cesspool.lol's pages, what it adds, and the words that
depend on where an install starts. Pages are rendered from the engine's stored answers for the fixture
blocks, so the engine itself is not needed; reorg-watch is, for the pool names."""
import glob, gzip, json, os, re, subprocess, sys, unittest

import support

NEEDS = unittest.skipUnless(support.HAVE_REORG_WATCH, "needs reorg-watch (REORG_WATCH_REAL)")


def built(ns, names):
    recs = support.grade(ns, names)
    site = ns.module("site")
    site.build(ns.out, heights=[r["h"] for r in recs], og=False)
    return site


def read(ns, rel):
    with open(os.path.join(ns.out, rel)) as f:
        return f.read()


def pages(ns):
    return sorted(glob.glob(os.path.join(ns.out, "**", "*.html"), recursive=True))


@NEEDS
class RemovedTests(unittest.TestCase):
    def test_home_has_no_hero_and_no_latest_sample_and_leads_with_the_newest_block(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-980000", "blake2b-981499"])
            home = read(ns, "index.html")
            for gone in ("herogrid", "Latest sample", "Clean blocks carry payments", "Bitcoin water quality, block by block</div>", "bigcube"):
                self.assertFalse(gone in home, f"did not expect {gone!r:.80}")
            strip = home[home.index('<div class="strip home">'):home.index("<h2")]
            self.assertEqual(re.findall(r'href="/block/(\d+)/"', strip), ["981499", "980000"])

    def test_no_page_has_the_plumb_card_or_the_discord_link(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-980000", "blake2b-981499"])
            self.assertGreater(len(pages(ns)), 12)
            for p in pages(ns):
                html = open(p).read()
                self.assertFalse("plumbcta" in html, f"did not expect {"plumbcta"!r:.80}")
                self.assertFalse("Get Plumb" in html, f"did not expect {"Get Plumb"!r:.80}")
                self.assertFalse("discord" in html.lower(), f"did not expect {"discord"!r:.80}")
            self.assertFalse(os.path.exists(os.path.join(ns.out, "static", "beh-discord.png")))
            self.assertTrue(os.path.exists(os.path.join(ns.out, "plumb", "index.html")))  # the page itself stays
            self.assertTrue('href="/plumb/"' in read(ns, "index.html"), f"expected to find {'href="/plumb/"'!r:.80}")

    def test_nothing_points_at_cesspool_lol_as_if_it_were_this_site(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-980000", "blake2b-981499"])
            for p in pages(ns):
                html = open(p).read()
                self.assertFalse('content="https://cesspool.lol' in html, f"did not expect {'content="https://cesspool.lol'!r:.80}")
                self.assertNotIn("og:image", html, p)  # share images are off
                for copied in re.findall(r'data-copy="([^"]*)"', html):
                    if "bitcoin.conf" not in copied and "corepolicy" not in copied:  # the conf files name it on purpose
                        self.assertFalse("cesspool.lol" in copied, f"did not expect {"cesspool.lol"!r:.80}")
                self.assertTrue("<title>" in html, f"expected to find {"<title>"!r:.80}")
                self.assertFalse("· cesspool.lol</title>" in html, f"did not expect {"· cesspool.lol</title>"!r:.80}")
            feed = read(ns, "feed.xml")
            self.assertFalse("cesspool.lol" in feed, f"did not expect {"cesspool.lol"!r:.80}")
            api = json.loads(read(ns, "api/v1/block/981499.json"))
            self.assertEqual(api["url"], "/block/981499/")
            # A bitcoin.conf is read away from the site, so its comments carry a whole address.
            self.assertTrue("# https://cesspool.lol/check/" in read(ns, "check/plumb.conf"), f"expected to find {"# https://cesspool.lol/check/"!r:.80}")

    def test_an_install_with_an_address_uses_it(self):
        with support.cesspool({"CESSPOOL_SITE": "https://example.org/"}) as ns:
            built(ns, ["blake2b-981499"])
            self.assertEqual(json.loads(read(ns, "api/v1/block/981499.json"))["url"], "https://example.org/block/981499/")
            self.assertTrue("https://example.org/block/981499/" in read(ns, "block/981499/index.html"), f"expected to find {"https://example.org/block/981499/"!r:.80}")
            self.assertTrue("# https://example.org/check/" in read(ns, "check/plumb.conf"), f"expected to find {"# https://example.org/check/"!r:.80}")

    def test_privacy_page_is_about_this_install(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-980000"])
            html = read(ns, "privacy/index.html")
            self.assertFalse("Cloudflare" in html, f"did not expect {"Cloudflare"!r:.80}")
            self.assertTrue("your own server" in html, f"expected to find {"your own server"!r:.80}")


@NEEDS
class CoverageTests(unittest.TestCase):
    def test_words_follow_where_the_install_starts(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-980000", "blake2b-981499"])
            about = read(ns, "about/index.html")
            self.assertTrue("since block 980000" in about, f"expected to find {"since block 980000"!r:.80}")
            self.assertTrue("This install has graded 2 blocks on the BLAKE2b chain, from 980000 to 981499" in about, f"expected to find {"This install has graded 2 blocks on the BLAKE2b chain, from 980000 to 981499"!r:.80}")
            self.assertTrue("blocks 980001 to 981498" in about, "the gap between them is named")
            self.assertTrue('data-fork="980000" data-since="since block 980000"' in read(ns, "found/index.html"), f"expected to find {'data-fork="980000" data-since="since block 980000"'!r:.80}")
            self.assertTrue("This install has graded blocks 980000 to 981499" in read(ns, "404.html"), f"expected to find {"This install has graded blocks 980000 to 981499"!r:.80}")
            self.assertNotIn("since the fork", re.sub(r"<[^>]+>", " ", read(ns, "shame/index.html")).replace("since the fork at", ""))
            cov = json.load(open(os.path.join(ns.home, "coverage.json")))
            self.assertEqual((cov["start"], cov["top"], cov["blocks"], cov["gaps"]), (980000, 981499, 2, [[980001, 981498]]))

    def test_the_rewording_tool_has_nothing_left_to_do(self):
        r = subprocess.run([sys.executable, os.path.join(support.ROOT, "tools", "since.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_measured_figures_are_marked_as_cesspool_lols(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-980000"])
            self.assertTrue("were measured by" in read(ns, "check/index.html"), f"expected to find {"were measured by"!r:.80}")
            self.assertTrue("taken on the BLAKE2b chain" in read(ns, "about/index.html"), f"expected to find {"taken on the BLAKE2b chain"!r:.80}")


@NEEDS
class ChainTests(unittest.TestCase):
    def test_blake2b_shows_two_verdicts(self):
        with support.cesspool(chain="blake2b") as ns:
            site = built(ns, ["blake2b-981499"])
            self.assertEqual([k for k, _ in site.VERDICT_NAMES], ["knots", "plumb"])
            block = read(ns, "block/981499/index.html")
            self.assertFalse("Bitcoin Core 31" in block, f"did not expect {"Bitcoin Core 31"!r:.80}")
            self.assertTrue("BLAKE2b" in read(ns, "index.html"), f"expected to find {"BLAKE2b"!r:.80}")
            self.assertTrue("Bitcoin Core is not compared" in read(ns, "about/index.html"), f"expected to find {"Bitcoin Core is not compared"!r:.80}")

    def test_sha256_adds_bitcoin_core(self):
        with support.cesspool(chain="sha256") as ns:
            site = built(ns, ["sha256-975450-cut"])
            self.assertEqual([k for k, _ in site.VERDICT_NAMES], ["core", "knots", "plumb"])
            block = read(ns, "block/975450/index.html")
            self.assertTrue("Bitcoin Core 31" in block, f"expected to find {"Bitcoin Core 31"!r:.80}")
            self.assertTrue("Bitcoin Core 31 is compared too" in read(ns, "about/index.html"), f"expected to find {"Bitcoin Core 31 is compared too"!r:.80}")
            with gzip.open(os.path.join(ns.out, "d", "catalog.json.gz")) as f:
                self.assertEqual([v[0] for v in json.load(f)["verdicts"]], ["core", "knots", "plumb"])
            self.assertTrue("mempool" in read(ns, "about/index.html"), f"expected to find {"mempool"!r:.80}")  # whose pool list
            self.assertTrue("SHA256" in read(ns, "index.html"), f"expected to find {"SHA256"!r:.80}")

    def test_chains_keep_separate_state(self):
        with support.cesspool(chain="blake2b") as a:
            built(a, ["blake2b-981499"])
            self.assertTrue(a.home.endswith("/blake2b") and a.out.endswith("/www/blake2b"))
            self.assertEqual(os.listdir(os.path.join(a.home, "blocks")), ["981499.json"])


@NEEDS
class LightningPagesTests(unittest.TestCase):
    def test_no_lightning_means_no_lightning_pages(self):
        with support.cesspool() as ns:
            built(ns, ["blake2b-981499"])
            self.assertFalse(os.path.exists(os.path.join(ns.out, "lightning")))
            self.assertFalse(os.path.exists(os.path.join(ns.out, "d", "ln")))
            self.assertFalse('href="/lightning/"' in read(ns, "index.html"), f"did not expect {'href="/lightning/"'!r:.80}")
            with gzip.open(os.path.join(ns.out, "d", "features.json.gz")) as f:
                self.assertEqual(json.load(f), {"lightning": False})

    def test_pages_from_a_read_graph_and_an_alias_stays_text(self):
        a, b = "02" + "a" * 64, "02" + "b" * 64
        env = {"CESSPOOL_LN_BACKEND": "cln", "CESSPOOL_CLN_SOCKET": "/nonexistent/lightning-rpc"}
        with support.cesspool(env) as ns:
            ln = ns.module("lightning")
            evil = '<img src=x onerror=alert(1)>'
            ln._write(ln.NODES, {a: {"al": evil, "co": "#ff9900", "ad": ["1.2.3.4:9735"], "u": 1, "f": 1, "l": 2}, b: {"al": "bob", "co": "", "ad": [], "u": 1, "f": 1, "l": 2}})
            ln._write(ln.CHANNELS, {"981499x1x0": {"a": a, "b": b, "c": 5000000, "p": None, "pa": None, "pb": None, "u": 1, "f": 1, "l": 2, "s": "open"}})
            ln._write(ln.INFO, {"backend": "cln", "name": "Core Lightning", "ok": True, "read": 1794000000, "tried": 1794000000,
                                "node": {"pub": a, "alias": evil, "color": "#ff9900", "version": "v26", "height": 981499, "chain_synced": True, "graph_synced": True}})
            ln._write(ln.STATS, [{"d": "2026-11-10", "t": 1, "n": 2, "c": 1, "cap": 5000000, "med": 5000000, "tor": 0, "clear": 1},
                                 {"d": "2026-11-11", "t": 2, "n": 2, "c": 1, "cap": 5000000, "med": 5000000, "tor": 0, "clear": 1}])
            built(ns, ["blake2b-981499"])
            html = read(ns, "lightning/index.html")
            self.assertFalse("<img src=x" in html, f"did not expect {"<img src=x"!r:.80}")
            self.assertTrue("&lt;img src=x onerror=alert(1)&gt;" in html, f"expected to find {"&lt;img src=x onerror=alert(1)&gt;"!r:.80}")
            self.assertTrue('href="/lightning/"' in read(ns, "index.html"), f"expected to find {'href="/lightning/"'!r:.80}")
            for rel in ("lightning/node/index.html", "lightning/channel/index.html", "lightning/penalties/index.html", "lightning/closed/index.html",
                        "lightning/nodes/capacity/index.html"):
                self.assertTrue(os.path.exists(os.path.join(ns.out, rel)), rel)
            with gzip.open(os.path.join(ns.out, "d", "ln", "n", "aa.json.gz")) as f:
                node = json.load(f)[a]
            self.assertEqual((node["al"], node["n"], node["cap"]), (evil, 1, 5000000))  # raw in the data; the script sets it as text
            with gzip.open(os.path.join(ns.out, "d", "ln", "c", "981.json.gz")) as f:
                self.assertEqual(json.load(f)["981499x1x0"]["s"], "open")
            # The funding transaction is transaction 1 of the graded block: scan() resolves it.
            idx = {981499: {"_m": os.path.getmtime(os.path.join(ns.home, "blocks", "981499.json"))}}
            ln.scan(idx)
            want = json.load(open(os.path.join(ns.home, "txd", "981499.json")))["tx"][0]["id"]
            self.assertEqual(json.load(open(ln.CHANNELS))["981499x1x0"]["p"], want + ":0")


if __name__ == "__main__":
    unittest.main()
