"""A Bitcoin node and an Electrum server that answer from stored blocks, for the tests.

The node speaks enough JSON-RPC for cesspool: getblockchaininfo, getbestblockhash, getblockhash,
getblock at verbosity 0, 1 and 3, decoderawtransaction and decodescript. It can be pruned (a block
below the prune height gives "Block not available (pruned data)" at verbosity 2 and 3, and is still
served at 0 and 1, as btc-rpc-proxy does in front of a pruned node), still syncing, or restarted
with a new cookie. The Electrum server answers server.version and blockchain.transaction.get for any
transaction a stored block holds or spends from.

Blocks are `getblock <hash> 3` objects, as the tests' fixtures hold them.

    python3 tests/fakenode.py --blocks DIR [--port 18332] [--prune HEIGHT] [--electrum-port 50001]
"""
import argparse, base64, glob, gzip, hashlib, json, os, socketserver, struct, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sha256d(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def varint(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def load_blocks(directory):
    """{height: block} from every <height>.json.gz in a directory."""
    out = {}
    for p in glob.glob(os.path.join(directory, "*.json.gz")):
        with gzip.open(p) as f:
            b = json.load(f)
        out[b["height"]] = b
    return out


class Chain:
    """What the fake node and the fake Electrum server answer from."""

    def __init__(self, blocks, tip=None, prune=0, ibd=False, header_bytes=80, fork_hash=None):
        self.blocks = dict(blocks)
        self.tip = max(self.blocks) if tip is None else tip
        self.prune, self.ibd, self.header_bytes, self.fork_hash = prune, ibd, header_bytes, fork_hash
        self.replaced = {}   # height -> hash, for a reorg the tests stage
        self.calls = []      # every method asked for, in order
        self.by_hash = {b["hash"]: b for b in self.blocks.values()}
        self.txs = {}        # txid -> raw hex, for everything a stored block holds
        self.spent = {}      # txid -> {vout: prevout}, for everything a stored block spends
        for b in self.blocks.values():
            for t in b["tx"]:
                self.txs[t["txid"]] = t["hex"]
                for v in t["vin"]:
                    if "prevout" in v:
                        self.spent.setdefault(v["txid"], {})[v["vout"]] = v["prevout"]

    def hash_at(self, h):
        if h in self.replaced:
            return self.replaced[h]
        if h in self.blocks:
            return self.blocks[h]["hash"]
        if h == 961640 and self.fork_hash:
            return self.fork_hash
        return f"{h:064x}"

    def raw_block(self, b):
        """The block as bytes: a header of the right length (its content is never read), then the transactions."""
        return (bytes(self.header_bytes) + varint(len(b["tx"])) + b"".join(bytes.fromhex(t["hex"]) for t in b["tx"])).hex()

    def raw_tx(self, txid):
        """A stored transaction, or one made up to carry the outputs a stored block spends from it."""
        if txid in self.txs:
            return self.txs[txid]
        outs = self.spent.get(txid)
        if not outs:
            return None
        return synthetic_tx(txid, outs)

    def rpc(self, method, params):
        self.calls.append(method)
        if method == "getblockchaininfo":
            info = {"chain": "main", "blocks": self.tip, "headers": self.tip, "bestblockhash": self.hash_at(self.tip),
                    "initialblockdownload": self.ibd, "verificationprogress": 0.5 if self.ibd else 1, "pruned": bool(self.prune)}
            if self.prune:
                info["pruneheight"] = self.prune
            return info
        if method == "getbestblockhash":
            return self.hash_at(self.tip)
        if method == "getblockhash":
            h = params[0]
            if not isinstance(h, int) or h < 0 or h > self.tip:
                raise RpcFault(-8, "Block height out of range")
            return self.hash_at(h)
        if method == "getblock":
            blockhash, verbosity = params[0], params[1] if len(params) > 1 else 1
            b = self.by_hash.get(blockhash)
            if b is None or self.replaced.get(b["height"], blockhash) != blockhash:
                # A height with no stored block, below the prune height: what a pruned node says of it.
                # (hash_at makes up a hash for such a height: the height itself, in hexadecimal.)
                if b is None and verbosity >= 2 and int(blockhash, 16) < self.prune:
                    raise RpcFault(-1, "Block not available (pruned data)")
                raise RpcFault(-5, "Block not found")
            if verbosity == 0:
                return self.raw_block(b)
            if verbosity == 1:
                return {k: v for k, v in b.items() if k != "tx"} | {"tx": [t["txid"] for t in b["tx"]]}
            if b["height"] < self.prune:
                raise RpcFault(-1, "Block not available (pruned data)")
            return b
        if method == "decoderawtransaction":
            t = self.decoded().get(params[0])
            if t is None:
                raise RpcFault(-22, "TX decode failed")
            return t
        if method == "decodescript":
            s = self.scripts().get(params[0])
            if s is None:
                raise RpcFault(-22, "unknown script")
            return s
        raise RpcFault(-32601, "Method not found")

    def decoded(self):
        """{raw hex: what decoderawtransaction says}: the stored transaction without what getblock adds."""
        if not hasattr(self, "_decoded"):
            self._decoded = {}
            for b in self.blocks.values():
                for t in b["tx"]:
                    d = {k: v for k, v in t.items() if k not in ("hex", "fee")}
                    d["vin"] = [{k: x for k, x in v.items() if k != "prevout"} for v in t["vin"]]
                    self._decoded[t["hex"]] = d
        return self._decoded

    def scripts(self):
        """{script hex: what decodescript says} for every script a stored block spends."""
        if not hasattr(self, "_scripts"):
            self._scripts = {}
            for outs in self.spent.values():
                for p in outs.values():
                    s = p["scriptPubKey"]
                    self._scripts[s["hex"]] = {k: v for k, v in s.items() if k != "hex"}
        return self._scripts


def synthetic_tx(txid, outs):
    """A transaction with the outputs `outs` ({index: prevout}) in place and empty ones between. Its
    bytes do not hash to `txid`; a test that checks the hash stores real blocks for both ends."""
    n = max(outs) + 1
    body = struct.pack("<i", 2) + varint(1) + bytes(32) + struct.pack("<I", 0xffffffff) + varint(0) + struct.pack("<I", 0xffffffff)
    body += varint(n)
    for i in range(n):
        p = outs.get(i)
        spk = bytes.fromhex(p["scriptPubKey"]["hex"]) if p else b""
        body += struct.pack("<q", round(p["value"] * 1e8) if p else 0) + varint(len(spk)) + spk
    return (body + struct.pack("<I", 0)).hex()


class RpcFault(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Node:
    """The JSON-RPC server. `auth` is "user:password", or a cookie file path it writes and can rotate."""

    def __init__(self, chain, user="", password="", cookie="", port=0, batches=True, host="127.0.0.1"):
        self.chain, self.user, self.password, self.cookie, self.batches = chain, user, password, cookie, batches
        self.secret = f"{user}:{password}" if user else None
        if cookie:
            self.rotate_cookie()
        node = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                given = self.headers.get("Authorization", "")
                want = "Basic " + base64.b64encode(node.secret.encode()).decode()
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if given != want:
                    return self.reply(401, b"")
                req = json.loads(body)
                if isinstance(req, list):
                    if not node.batches:
                        return self.reply(400, b'{"result":null,"error":{"code":-32600,"message":"batches are not supported"},"id":null}')
                    return self.reply(200, json.dumps([self.one(r) for r in req]).encode())
                out = self.one(req)
                self.reply(500 if out["error"] else 200, json.dumps(out).encode())

            def one(self, r):
                try:
                    return {"result": node.chain.rpc(r["method"], r.get("params") or []), "error": None, "id": r.get("id")}
                except RpcFault as e:
                    return {"result": None, "error": {"code": e.code, "message": e.message}, "id": r.get("id")}

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def rotate_cookie(self):
        """What a node does when it starts: a new secret in the cookie file."""
        self.secret = "__cookie__:" + os.urandom(16).hex()
        with open(self.cookie, "w") as f:
            f.write(self.secret)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Electrum:
    """Line-delimited JSON-RPC over TCP: server.version and blockchain.transaction.get, singly or in a batch."""

    def __init__(self, chain, port=0, protocol="1.4", missing=(), host="127.0.0.1"):
        self.chain, self.protocol, self.missing, self.requests = chain, protocol, set(missing), 0
        electrum = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    req = json.loads(line)
                    electrum.requests += 1
                    out = [self.one(r) for r in req] if isinstance(req, list) else self.one(req)
                    self.wfile.write(json.dumps(out).encode() + b"\n")

            def one(self, r):
                m, p = r["method"], r.get("params") or []
                if m == "server.version":
                    lo, hi = (p[1], p[1]) if isinstance(p[1], str) else p[1]
                    if not float(lo) <= float(electrum.protocol) <= float(hi):
                        return {"jsonrpc": "2.0", "id": r["id"], "error": {"code": 1, "message": "unsupported protocol version"}}
                    return {"jsonrpc": "2.0", "id": r["id"], "result": ["FakeElectrum 1.0", electrum.protocol]}
                if m == "blockchain.transaction.get":
                    raw = None if p[0] in electrum.missing else electrum.chain.raw_tx(p[0])
                    if raw is None:
                        return {"jsonrpc": "2.0", "id": r["id"], "error": {"code": 2, "message": "missing transaction"}}
                    return {"jsonrpc": "2.0", "id": r["id"], "result": raw}
                return {"jsonrpc": "2.0", "id": r["id"], "error": {"code": -32601, "message": "unknown method"}}

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server((host, port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Lnd:
    """LND's REST interface: /v1/getinfo and /v1/graph over TLS, for a client that sends the macaroon.
    The certificate is self-signed, as LND's is, and names a host nobody connects to by, so a client
    passes only by trusting this certificate itself. Needs the openssl command."""

    def __init__(self, workdir, info, nodes, edges, macaroon=b"\x02fake-readonly-macaroon", host="127.0.0.1", port=0):
        import http.server, ssl
        self.info, self.nodes, self.edges, self.requests = info, nodes, edges, []
        self.cert, key = os.path.join(workdir, "tls.cert"), os.path.join(workdir, "tls.key")
        self.macaroon = os.path.join(workdir, "readonly.macaroon")
        with open(self.macaroon, "wb") as f:
            f.write(macaroon)
        if not (os.path.exists(self.cert) and os.path.exists(key)):  # a pair made beforehand is used as it is
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", self.cert,
                            "-days", "2", "-subj", "/CN=lnd-test", "-addext", "subjectAltName=DNS:lnd-test"], check=True, capture_output=True)
        lnd, want = self, macaroon.hex()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                lnd.requests.append(self.path)
                if self.headers.get("Grpc-Metadata-macaroon") != want:
                    body, code = b'{"code":16,"message":"verification failed"}', 500
                elif self.path == "/v1/getinfo":
                    body, code = json.dumps(lnd.info).encode(), 200
                elif self.path.startswith("/v1/graph"):
                    body, code = json.dumps({"nodes": lnd.nodes, "edges": lnd.edges}).encode(), 200
                else:
                    body, code = b'{"code":5,"message":"Not Found"}', 404
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Cln:
    """Core Lightning's lightning-rpc: JSON-RPC over a unix socket, one request to a connection here,
    the reply ended by a blank line. Answers getinfo, listnodes and listchannels."""

    def __init__(self, path, info, nodes, channels):
        self.path, self.methods = path, []
        replies = {"getinfo": info, "listnodes": {"nodes": nodes}, "listchannels": {"channels": channels}}
        cln = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                buf = b""
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    try:
                        req = json.loads(buf)
                        break
                    except ValueError:
                        continue
                cln.methods.append(req["method"])
                self.request.sendall(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": replies.get(req["method"], {})}).encode() + b"\n\n")
                # lightningd leaves the connection open for another request; the client closes it.
                while self.request.recv(65536):
                    pass

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(path, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        try:
            os.remove(self.path)
        except OSError:
            pass


def demo_graph(chain):
    """(getinfo, nodes, edges) for a small made-up Lightning graph tied to the stored blocks, so the
    Lightning pages have something real to join: channels funded by transactions of the lowest block
    (at the place their short ids name, holding their capacity), and older channels whose funding
    outputs transactions of the highest block spend, which are found as closes once it is graded."""
    pubs = ["02" + f"{i:02x}" * 32 for i in range(1, 9)]
    nodes = [{"pub_key": p, "alias": f"demo-node-{i + 1}", "color": "#%02x9966" % (40 * i % 256), "last_update": 1,
              "addresses": [{"network": "tcp", "addr": f"{'abcdefgh'[i] * 16}.onion:9735" if i % 2 else f"203.0.113.{i + 1}:9735"}]} for i, p in enumerate(pubs)]
    policy = {"time_lock_delta": 80, "min_htlc": "1000", "fee_base_msat": "1000", "fee_rate_milli_msat": "100", "disabled": False, "max_htlc_msat": "990000000"}
    lo, hi = chain.blocks[min(chain.blocks)], chain.blocks[max(chain.blocks)]
    edges = []

    def edge(scid, point, cap, i):
        h, t, o = (int(x) for x in scid.split("x"))
        edges.append({"channel_id": str((h << 40) | (t << 16) | o), "chan_point": point, "capacity": str(cap), "last_update": 1,
                      "node1_pub": pubs[i % 8], "node2_pub": pubs[(i * 3 + 1) % 8], "node1_policy": policy, "node2_policy": policy if i % 3 else None})

    for i, t in enumerate(lo["tx"][1:9], 1):
        edge(f'{lo["height"]}x{i}x0', f'{t["txid"]}:0', round(t["vout"][0]["value"] * 1e8), i)
    for i, t in enumerate([t for t in hi["tx"][1:] if len(t["vin"]) == 1][:6]):
        v = t["vin"][0]
        edge(f"{900000 + i}x{i + 1}x{v['vout']}", f'{v["txid"]}:{v["vout"]}', round(v["prevout"]["value"] * 1e8), i + 2)
    info = {"identity_pubkey": pubs[0], "alias": "demo-node-1", "color": "#009966", "version": "0.21.0-demo", "block_height": chain.tip,
            "block_hash": chain.hash_at(chain.tip), "synced_to_chain": True, "synced_to_graph": True}
    return info, nodes, edges


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--blocks", required=True, help="directory of <height>.json.gz blocks (getblock verbosity 3)")
    ap.add_argument("--port", type=int, default=18332)
    ap.add_argument("--electrum-port", type=int, default=0, help="also serve Electrum on this port")
    ap.add_argument("--prune", type=int, default=0, help="answer as a node pruned below this height")
    ap.add_argument("--tip", type=int)
    ap.add_argument("--header-bytes", type=int, default=80, help="164 for BLAKE2b blocks")
    ap.add_argument("--blake2b", action="store_true", help="answer block 961640 with the BLAKE2b chain's hash, and use 164-byte headers")
    ap.add_argument("--user", default="test")
    ap.add_argument("--password", default="test")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on; 0.0.0.0 to be reached from a container")
    ap.add_argument("--lnd", metavar="DIR", help="also serve a made-up LND (tls.cert and readonly.macaroon are written to DIR)")
    ap.add_argument("--lnd-port", type=int, default=18080)
    a = ap.parse_args()
    chain = Chain(load_blocks(a.blocks), tip=a.tip, prune=a.prune, header_bytes=164 if a.blake2b else a.header_bytes,
                  fork_hash="0000000000000050c1e5f69672f459293be14f46e5a494e7a8c8541396f18eeb" if a.blake2b else None)
    node = Node(chain, a.user, a.password, port=a.port, host=a.host)
    print(f"node at {node.url} (user {a.user}), {len(chain.blocks)} blocks, tip {chain.tip}", flush=True)
    if a.electrum_port:
        e = Electrum(chain, port=a.electrum_port, host=a.host)
        print(f"electrum at 127.0.0.1:{e.port}", flush=True)
    if a.lnd:
        os.makedirs(a.lnd, exist_ok=True)
        lnd = Lnd(a.lnd, *demo_graph(chain), host=a.host, port=a.lnd_port)
        print(f"lnd at {lnd.url}, certificate and macaroon in {a.lnd}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
