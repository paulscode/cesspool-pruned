"""JSON-RPC to the Bitcoin node, in place of the REST interface cesspool.lol reads.

REST needs rest=1 on the node, carries no credentials, and is not what the btc-rpc-proxy in front of
a pruned node speaks. `getblock <hash> 3` returns the object REST's block/<hash>.json returns, so
everything downstream reads the same block.

Standard library only. Credentials are a user and password, or the node's cookie file, which the node
writes again on every start: a 401 reads it again and tries once more.
"""
import base64, http.client, json, os, socket, urllib.error, urllib.request

TIMEOUT = 60
# bitcoind keeps a chain other than mainnet in a directory of its own, cookie and all.
COOKIE_DIRS = ("", "regtest", "testnet4", "testnet3", "signet")


class RpcError(Exception):
    """The node answered, and the answer is an error."""

    def __init__(self, code, message, method=""):
        super().__init__(f"{method or 'rpc'}: {message} ({code})")
        self.code, self.message, self.method = code, message, method

    @property
    def pruned(self):
        """The node no longer has this block: 'Block not available (pruned data)', or the undo data."""
        return "pruned" in self.message.lower()


class Unreachable(Exception):
    """No usable answer: nothing listening, a timeout, refused credentials, a body that is not JSON-RPC."""


def find_cookie(path):
    """The cookie file for `path`: the file itself, or, when `path` is a data directory, the first
    cookie under it, mainnet's first. None when there is none to read yet."""
    if not path:
        return None
    if not os.path.isdir(path):
        return path if os.path.isfile(path) else None
    for sub in COOKIE_DIRS:
        p = os.path.join(path, sub, ".cookie")
        if os.path.isfile(p):
            return p
    return None


class Client:
    def __init__(self, url, user="", password="", cookie="", timeout=TIMEOUT):
        self.url, self.user, self.password, self.cookie, self.timeout = url.rstrip("/"), user, password, cookie, timeout
        self._auth = None

    def _credentials(self, fresh=False):
        if self._auth is None or fresh:
            if self.user:
                secret = f"{self.user}:{self.password}"
            else:
                path = find_cookie(self.cookie)
                if not path:
                    raise Unreachable(f"no RPC user is set and there is no cookie at {self.cookie or '(no path given)'}: is the node running?")
                try:
                    with open(path) as f:
                        secret = f.read().strip()
                except OSError as e:
                    raise Unreachable(f"cannot read the RPC cookie {path}: {e.strerror or e}") from None
            self._auth = "Basic " + base64.b64encode(secret.encode()).decode()
        return self._auth

    def _post(self, payload, timeout, fresh=False):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": self._credentials(fresh)})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 401:
                if not fresh and not self.user:
                    return self._post(payload, timeout, fresh=True)  # the node restarted and wrote a new cookie
                raise Unreachable("the node refused the RPC credentials (401)") from None
            if e.code == 403:
                raise Unreachable("the node does not allow RPC from this address (403): check rpcallowip") from None
            if body[:1] in (b"{", b"["):
                return body  # bitcoind sends an RPC error as HTTP 500 or 404 with the error in the body
            raise Unreachable(f"the node answered HTTP {e.code}: {body[:200].decode('utf-8', 'replace').strip() or e.reason}") from None
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise Unreachable(f"cannot reach the node's RPC at {self.url}: {reason}") from None

    @staticmethod
    def _decode(body):
        try:
            return json.loads(body)
        except ValueError:
            raise Unreachable(f"the node's answer is not JSON: {body[:120]!r}") from None

    def call(self, method, *params, timeout=None):
        reply = self._decode(self._post({"jsonrpc": "1.0", "id": "cesspool", "method": method, "params": list(params)}, timeout))
        if not isinstance(reply, dict):
            raise Unreachable(f"{method}: the node's answer is not a JSON-RPC reply")
        err = reply.get("error")
        if err:
            raise RpcError(err.get("code"), str(err.get("message", "")), method) if isinstance(err, dict) else RpcError(None, str(err), method)
        return reply.get("result")

    def batch(self, calls, timeout=None):
        """[(method, params), ...] in one request. Returns one item per call, in order: its result, or
        the RpcError it got. A server that does not take a batch gets the calls one at a time."""
        if not calls:
            return []
        payload = [{"jsonrpc": "1.0", "id": i, "method": m, "params": list(p)} for i, (m, p) in enumerate(calls)]
        reply = self._decode(self._post(payload, timeout))
        if not isinstance(reply, list) or len(reply) != len(calls):
            out = []
            for m, p in calls:
                try:
                    out.append(self.call(m, *p, timeout=timeout))
                except RpcError as e:
                    out.append(e)
            return out
        out = [None] * len(calls)
        for r in reply:
            i = r.get("id") if isinstance(r, dict) else None
            if not isinstance(i, int) or not 0 <= i < len(calls):
                raise Unreachable("a batch reply does not name the call it answers")
            err = r.get("error")
            out[i] = RpcError(err.get("code"), str(err.get("message", "")), calls[i][0]) if isinstance(err, dict) else r.get("result")
        return out

    # The calls cesspool makes, by name.

    def getblockchaininfo(self):
        return self.call("getblockchaininfo", timeout=20)

    def getbestblockhash(self):
        return self.call("getbestblockhash", timeout=8)

    def getblockhash(self, height):
        return self.call("getblockhash", height)

    def getblock(self, blockhash, verbosity):
        return self.call("getblock", blockhash, verbosity, timeout=300 if verbosity else 120)
