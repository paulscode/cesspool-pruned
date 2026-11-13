"""A small Electrum protocol client: enough to ask an indexer for transactions by id.

Newline-delimited JSON-RPC over plaintext TCP, standard library only. backfill.py uses it to read
the transactions whose outputs an old block spends, when the node has pruned its record of them.
"""
import json, socket

# The versions offered. An Electrum server on the BLAKE2b chain refuses anything below 1.8;
# servers on the SHA256 chain mostly speak 1.4.
PROTOCOL = ["1.4", "1.8"]
CLIENT = "cesspool-pruned"


class ElectrumError(Exception):
    """The server could not be reached, hung up, or answered something that is not the protocol."""


class Missing(Exception):
    """The server answered, and does not have the transaction asked for."""

    def __init__(self, txid, message):
        super().__init__(f"the Electrum server has no transaction {txid}: {message}")
        self.txid, self.message = txid, message


class Client:
    def __init__(self, host, port, timeout=300):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = self.file = None
        self.server = self.version = None
        self.next_id = 0

    def connect(self):
        if self.sock is not None:
            return
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=30)
            self.sock.settimeout(self.timeout)
            self.file = self.sock.makefile("rwb")
        except OSError as e:
            self.close()
            raise ElectrumError(f"cannot reach the Electrum server at {self.host}:{self.port}: {e}") from None
        reply = self._round({"jsonrpc": "2.0", "id": self._id(), "method": "server.version", "params": [CLIENT, PROTOCOL]})
        if not isinstance(reply, dict) or reply.get("error") or not isinstance(reply.get("result"), list):
            self.close()
            raise ElectrumError(f"the Electrum server at {self.host}:{self.port} did not agree a protocol version: {reply!r:.200}")
        self.server, self.version = reply["result"][0], reply["result"][1]

    def close(self):
        for x in (self.file, self.sock):
            try:
                if x is not None:
                    x.close()
            except OSError:
                pass
        self.sock = self.file = None

    def _id(self):
        self.next_id += 1
        return self.next_id

    def _round(self, payload):
        try:
            self.file.write(json.dumps(payload).encode() + b"\n")
            self.file.flush()
            line = self.file.readline()
        except (OSError, ValueError) as e:
            self.close()
            raise ElectrumError(f"the Electrum server at {self.host}:{self.port} stopped answering: {e}") from None
        if not line:
            self.close()
            raise ElectrumError(f"the Electrum server at {self.host}:{self.port} closed the connection")
        try:
            return json.loads(line)
        except ValueError:
            self.close()
            raise ElectrumError(f"the Electrum server's answer is not JSON: {line[:120]!r}") from None

    def transactions(self, txids):
        """{txid: raw hex} for every id in `txids`, asked for in one batch. Raises Missing for the first
        one the server does not have."""
        if not txids:
            return {}
        self.connect()
        ids = {self._id(): t for t in txids}
        reply = self._round([{"jsonrpc": "2.0", "id": i, "method": "blockchain.transaction.get", "params": [t, False]} for i, t in ids.items()])
        if isinstance(reply, dict):
            if len(ids) == 1:
                reply = [reply]
            else:
                # No batches here: ask one at a time on the same connection.
                reply = [self._round({"jsonrpc": "2.0", "id": i, "method": "blockchain.transaction.get", "params": [t, False]}) for i, t in ids.items()]
        out = {}
        for r in reply if isinstance(reply, list) else []:
            t = ids.get(r.get("id")) if isinstance(r, dict) else None
            if t is None:
                continue
            if r.get("error") or not isinstance(r.get("result"), str):
                err = r.get("error")
                raise Missing(t, str(err.get("message") if isinstance(err, dict) else err or "no transaction in the answer"))
            out[t] = r["result"]
        lost = [t for t in txids if t not in out]
        if lost:
            raise ElectrumError(f"the Electrum server left {len(lost)} of {len(txids)} requests unanswered")
        return out
