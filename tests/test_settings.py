"""The settings form for platforms with no form of their own: what may be stored, who may store it,
and that the updater and the pages follow it, there and nowhere else."""
import http.client, importlib.util, json, os, threading, unittest
from http.server import ThreadingHTTPServer

import support


def load_server():
    spec = importlib.util.spec_from_file_location("cesspool_settings_server", os.path.join(support.ROOT, "tools", "settings_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StoreTests(unittest.TestCase):
    def test_only_known_values_are_kept(self):
        with support.cesspool({"CESSPOOL_SETTINGS_PAGE": "1"}) as ns:
            s = ns.module("settings")
            self.assertEqual(s.valid({"backfill": False, "max_gb": 25, "other": 1}), {"backfill": False, "max_gb": 25})
            self.assertEqual(s.valid({"backfill": "yes", "max_gb": 33}), {})
            self.assertEqual(s.valid({"max_gb": True}), {})        # a bool is an int in Python; not here
            self.assertEqual(s.valid([1, 2]), {})
            self.assertEqual(s.write({"backfill": True}), {"backfill": True})
            self.assertEqual(s.write({"max_gb": 0}), {"backfill": True, "max_gb": 0})   # merged over what is there
            self.assertEqual(s.read(), {"backfill": True, "max_gb": 0})
            self.assertEqual(oct(os.stat(s.path()).st_mode & 0o777), "0o644")

    def test_without_the_page_the_file_is_ignored(self):
        with support.cesspool({"CESSPOOL_BACKFILL": "1", "CESSPOOL_MAX_GB": "50"}) as ns:
            with open(os.path.join(ns.data, "settings.json"), "w") as f:
                json.dump({"backfill": False, "max_gb": 10}, f)
            self.assertEqual(ns.module("settings").read(), {})
            local = ns.module("local")
            self.assertTrue(local.BACKFILL)
            self.assertEqual(local.MAX_BYTES, 50 * 2**30)

    def test_with_the_page_the_choice_wins_over_the_environment(self):
        with support.cesspool({"CESSPOOL_SETTINGS_PAGE": "1", "CESSPOOL_BACKFILL": "0", "CESSPOOL_MAX_GB": "50"}) as ns:
            local = ns.module("local")
            self.assertFalse(local.BACKFILL)                      # nothing chosen yet: the environment
            self.assertEqual(local.MAX_BYTES, 50 * 2**30)
        with support.cesspool({"CESSPOOL_SETTINGS_PAGE": "1", "CESSPOOL_BACKFILL": "0", "CESSPOOL_MAX_GB": "50"}) as ns:
            with open(os.path.join(ns.data, "settings.json"), "w") as f:
                json.dump({"backfill": True, "max_gb": 100}, f)
            local = ns.module("local")
            self.assertTrue(local.BACKFILL)
            self.assertEqual(local.MAX_BYTES, 100 * 2**30)
            self.assertEqual(local.status()["settings"], {"backfill": True, "max_gb": 100})


class ServerTests(unittest.TestCase):
    def post(self, body, headers=None, method="POST", path="/settings"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h = {"Content-Type": "application/json", "X-Cesspool-Settings": "1"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c.request(method, path, body=data if method == "POST" else None, headers=h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out

    def test_the_endpoint(self):
        with support.cesspool({"CESSPOOL_SETTINGS_PAGE": "1"}) as ns:
            server = load_server()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            self.port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                self.assertEqual(self.post({"backfill": True, "max_gb": 25}), (200, {"settings": {"backfill": True, "max_gb": 25}}))
                with open(os.path.join(ns.data, "settings.json")) as f:
                    self.assertEqual(json.load(f), {"backfill": True, "max_gb": 25})
                code, _ = self.post({"backfill": False}, {"X-Cesspool-Settings": None})      # a plain form post cannot set the header
                self.assertEqual(code, 400)
                code, _ = self.post({"backfill": False}, {"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(code, 400)
                code, _ = self.post({"backfill": False}, {"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(code, 403)
                self.assertEqual(self.post({"max_gb": 33})[0], 400)                             # not one of the choices
                self.assertEqual(self.post({"backfill": False, "delete": "everything"})[0], 400)
                self.assertEqual(self.post(b"not json")[0], 400)
                self.assertEqual(self.post(b"x" * 2000)[0], 400)
                self.assertEqual(self.post({}, method="GET")[0], 405)
                self.assertEqual(self.post({"backfill": False}, path="/elsewhere")[0], 404)
                self.assertEqual(self.post({"backfill": False}, {"Sec-Fetch-Site": "same-origin"}), (200, {"settings": {"backfill": False, "max_gb": 25}}))
            finally:
                httpd.shutdown()
                httpd.server_close()


class BuildTests(unittest.TestCase):
    def test_a_saved_change_brings_a_build(self):
        with support.cesspool({"CESSPOOL_SETTINGS_PAGE": "1"}) as ns:
            local, s = ns.module("local"), ns.module("settings")
            os.makedirs(os.path.join(ns.out, "block"))
            os.makedirs(os.path.join(ns.home, "blocks"))
            with open(os.path.join(ns.out, "tip.json"), "w") as f:
                f.write("{}")
            os.utime(os.path.join(ns.out, "tip.json"), (1000, 1000))
            local._run.clear()
            self.assertFalse(local.build_due(ns.out, [], 0))
            s.write({"backfill": True})
            self.assertTrue(local.build_due(ns.out, [], 0))


@unittest.skipUnless(support.HAVE_REORG_WATCH, "needs reorg-watch (REORG_WATCH_REAL)")
class PageTests(unittest.TestCase):
    def build(self, env):
        with support.cesspool(env) as ns:
            support.grade(ns, ["blake2b-981499"])
            ns.module("site").build(ns.out, heights=[981499], og=False)
            with open(os.path.join(ns.out, "about", "index.html")) as f:
                about = f.read()
            with open(os.path.join(ns.out, "index.html")) as f:
                home = f.read()
            return about, home, os.path.exists(os.path.join(ns.out, "static", "settings.js"))

    def test_the_form_is_on_about_where_the_platform_has_no_form(self):
        about, home, copied = self.build({"CESSPOOL_SETTINGS_PAGE": "1", "CESSPOOL_BACKFILL": "0", "CESSPOOL_MAX_GB": "50"})
        self.assertTrue('<form class="settingsform' in about, "the form is drawn")
        self.assertTrue('value="0" checked' in about, "backfill shows off")
        self.assertTrue('<option value="50" selected>' in about, "the limit shows 50 GB")
        self.assertTrue("/static/settings.js" in about and "/static/settings.js" not in home, "the script is on /about/ only")
        self.assertTrue(copied)

    def test_and_nowhere_else(self):
        about, _, _ = self.build({"CESSPOOL_BACKFILL": "1"})
        self.assertFalse("settingsform" in about or "/static/settings.js" in about, "no form without CESSPOOL_SETTINGS_PAGE")


if __name__ == "__main__":
    unittest.main()
