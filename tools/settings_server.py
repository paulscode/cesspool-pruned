#!/usr/bin/env python3
"""Takes the settings form on /about/ and writes <data>/settings.json (cesspool/settings.py).

Runs beside nginx in the web container, on loopback only, when CESSPOOL_SETTINGS_PAGE=1; nginx
passes it POST /settings and nothing else. It accepts only what a page of this site sends: JSON, with
a header a cross-site form cannot set, and not from another site when the browser says where it is from.
Who may open the site at all is the platform's business (Umbrel puts its login in front).

settings_server.py [--port 8081]
"""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cesspool import settings  # noqa: E402

HEADER = "X-Cesspool-Settings"


class Handler(BaseHTTPRequestHandler):
    server_version = "cesspool-settings"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # no access log, as nginx keeps none

    def reply(self, code, obj):
        body = (json.dumps(obj) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(405, {"error": "use POST"})

    def do_POST(self):
        if urlsplit(self.path).path != "/settings":
            return self.reply(404, {"error": "not found"})
        if self.headers.get(HEADER) != "1" or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.reply(400, {"error": "not a request from this site's settings form"})
        # The header above already keeps a cross-site page out: a browser sends a custom header to
        # another origin only after a CORS preflight, and nothing here answers one. The browser's own
        # word on where the request comes from is checked too. Host and Origin are not compared,
        # because the platform's proxy in front may rewrite Host.
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return self.reply(403, {"error": "another site cannot change these settings"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= 1024:
            return self.reply(400, {"error": "bad length"})
        try:
            doc = json.loads(self.rfile.read(n))
        except ValueError:
            return self.reply(400, {"error": "not JSON"})
        good = settings.valid(doc)
        if not good or set(good) != set(k for k in doc if k in settings.KEYS) or set(doc) - set(settings.KEYS):
            return self.reply(400, {"error": "unknown setting or value", "choices": {"max_gb": list(settings.MAX_GB_CHOICES)}})
        try:
            stored = settings.write(good)
        except OSError as e:
            return self.reply(500, {"error": f"could not save: {e.strerror or e}"})
        self.reply(200, {"settings": stored})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    a = ap.parse_args()
    if not settings.enabled():
        sys.exit("CESSPOOL_SETTINGS_PAGE is not set; there is no settings form to serve")
    os.umask(0o022)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
