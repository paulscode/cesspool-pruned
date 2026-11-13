"""Rebuild a block the node has pruned, with the coins it spends, so it can be graded like any other.

A pruned node has neither the block nor its undo data, so `getblock <hash> 3` fails for it. Two
other sources together hold the same information:

  btc-rpc-proxy, in front of a pruned node, fetches the block from peers for `getblock <hash> 0`
  (the bytes) and `1` (the header fields and transaction ids);
  an Electrum server that works beside a pruned node (Electrs Pruned, Fulcrum Pruned) has every
  transaction by id, and so the outputs each input of the block spends.

rebuild() puts them together into the object `getblock <hash> 3` would have returned, with the keys
cesspool reads. Nothing in it is taken on trust from the two servers: every transaction of the block
must hash to the id the node lists for it, and every transaction fetched for its outputs must hash
to the id that names it. The node decodes scripts into types and addresses (decoderawtransaction,
decodescript), so no script reading is done here.

It is slow. Each transaction an input spends from sits in some older block, which the Electrum
server in turn has to fetch from peers. A block of a few thousand inputs can take many minutes, so:
one block is rebuilt at a time, a new block at the tip interrupts it, and the outputs fetched so far
are kept on disk for the next try.

State: <HOME>/backfill.json (why it stopped, and when to try again), <HOME>/backfill/<height>.json
(the outputs fetched for a block in progress).
"""
import hashlib, json, os, time

from . import electrum, local, rpc

STATE = f"{local.HOME}/backfill.json"
WORK = f"{local.HOME}/backfill"
ASK = 25               # transactions asked of the Electrum server at once
PAUSE = 0.5            # seconds between those requests: the server has wallets to answer too
DECODE = 150           # transactions, or scripts, the node decodes in one request
RETRY_MISSING = 6 * 3600   # after the server lacked a transaction: it may still be indexing
RETRY_DOWN = 300           # after it could not be reached
VERIFY = True          # the tests turn this off to serve made-up transactions


class Unusable(Exception):
    """This block cannot be rebuilt now; the reason is for the pages."""


# ---------------------------------------------------------------- transactions as bytes

def _varint(b, i):
    n = b[i]
    if n < 0xfd:
        return n, i + 1
    if n == 0xfd:
        return int.from_bytes(b[i + 1:i + 3], "little"), i + 3
    if n == 0xfe:
        return int.from_bytes(b[i + 1:i + 5], "little"), i + 5
    return int.from_bytes(b[i + 1:i + 9], "little"), i + 9


def parse_tx(b, i=0):
    """One serialized transaction starting at b[i]. Returns (end, txid, inputs, outputs): inputs as
    (previous txid, output index), outputs as (sats, script hex). IndexError or ValueError when the
    bytes are not a transaction."""
    start = i
    i += 4
    segwit = b[i] == 0 and b[i + 1] != 0
    if segwit:
        i += 2
    body = i
    n_in, i = _varint(b, i)
    ins = []
    for _ in range(n_in):
        prev, vout = b[i:i + 32][::-1].hex(), int.from_bytes(b[i + 32:i + 36], "little")
        n, i = _varint(b, i + 36)
        i += n + 4
        ins.append((prev, vout))
    n_out, i = _varint(b, i)
    outs = []
    for _ in range(n_out):
        sats = int.from_bytes(b[i:i + 8], "little")
        n, i = _varint(b, i + 8)
        outs.append((sats, b[i:i + n].hex()))
        i += n
    end_body = i
    if segwit:
        for _ in range(n_in):
            items, i = _varint(b, i)
            for _ in range(items):
                n, i = _varint(b, i)
                i += n
    locktime = b[i:i + 4]
    i += 4
    if i > len(b) or len(locktime) != 4:
        raise ValueError("transaction runs past the end of the data")
    # The id is the hash of the serialization without the segwit marker and witnesses.
    stripped = b[start:start + 4] + b[body:end_body] + locktime
    txid = hashlib.sha256(hashlib.sha256(stripped).digest()).digest()[::-1].hex()
    return i, txid, ins, outs


def split_block(raw, txids):
    """[(raw hex, inputs, outputs), ...] for the transactions of a serialized block, checked against
    the ids the node lists for it. The header is 80 bytes, or 164 from the BLAKE2b fork on; whichever
    length makes every transaction hash to its id is the right one."""
    for header in (80, 164):
        try:
            count, i = _varint(raw, header)
            if count != len(txids):
                continue
            out = []
            for want in txids:
                start = i
                i, txid, ins, outs = parse_tx(raw, i)
                if txid != want:
                    raise ValueError("transaction id does not match")
                out.append((raw[start:i].hex(), ins, outs))
            if i == len(raw):
                return out
        except (IndexError, ValueError):
            continue
    raise Unusable("the block's bytes do not match the transaction ids the node lists for it")


# ---------------------------------------------------------------- whether to, and why not

def wanted():
    return local.BACKFILL


def configured():
    return bool(local.ELECTRUM[0])


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def usable():
    """Whether a pruned block can be rebuilt now: turned on, an Electrum server set, and not waiting
    out a failure."""
    if not (wanted() and configured()):
        return False
    return time.time() >= _load().get("retry", 0)


def report():
    """For coverage.json and the pages: off, none (no Electrum server), ready, or failed with the reason."""
    if not wanted():
        return {"state": "off"}
    if not configured():
        return {"state": "none"}
    s = _load()
    out = {"state": "failed" if time.time() < s.get("retry", 0) else "ready"}
    for k in ("reason", "height", "retry", "rebuilt", "last"):
        if k in s:
            out[k] = s[k]
    return out


def _fail(h, reason, wait):
    s = _load()
    if s.get("reason") != reason:
        print(f"backfill: stopped at block {h}: {reason}", flush=True)  # once, not on every retry
    s.update(reason=reason, height=h, retry=int(time.time()) + wait)
    _save(s)


# ---------------------------------------------------------------- rebuilding

_client = None


def _electrum():
    global _client
    if _client is None:
        _client = electrum.Client(*local.ELECTRUM)
    return _client


def _work_path(h):
    return f"{WORK}/{h}.json"


def _load_work(h, blockhash):
    try:
        with open(_work_path(h)) as f:
            w = json.load(f)
        if w.get("hash") == blockhash:
            return w["outs"]
    except (OSError, ValueError, KeyError):
        pass
    return {}


def _save_work(h, blockhash, outs):
    os.makedirs(WORK, exist_ok=True)
    tmp = _work_path(h) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"hash": blockhash, "outs": outs}, f, separators=(",", ":"))
    os.replace(tmp, _work_path(h))


def _decode(node, calls):
    """The node's answers to `calls` ([(method, params), ...]: transactions or scripts to decode), in
    order, a request holding at most DECODE of them and a few megabytes; a huge transaction goes alone."""
    out, i = [], 0
    while i < len(calls):
        n = size = 0
        while i + n < len(calls) and n < DECODE and (not n or size + len(calls[i + n][1][0]) <= 4_000_000):
            size += len(calls[i + n][1][0])
            n += 1
        for r in node.batch(calls[i:i + n]):
            if isinstance(r, rpc.RpcError):
                raise Unusable(f"the node could not decode part of the block ({r.message})")
            out.append(r)
        i += n
    return out


def assemble(header, txs, decoded, spent, scripts):
    """The `getblock <hash> 3` object: `header` is the verbosity-1 block, `txs` what split_block gave,
    `decoded` the node's decoderawtransaction of each, `spent` {"txid:n": [sats, script hex]} for every
    output the block spends, `scripts` {script hex: its decoding}."""
    block = {k: v for k, v in header.items() if k != "tx"}
    block["tx"] = []
    for (raw, ins, outs), d in zip(txs, decoded):
        t = dict(d)
        t["hex"] = raw
        if not ("coinbase" in t["vin"][0]):
            paid = 0
            for v in t["vin"]:
                sats, script = spent[f'{v["txid"]}:{v["vout"]}']
                paid += sats
                v["prevout"] = {"value": sats / 1e8, "scriptPubKey": dict(scripts[script], hex=script)}
            t["fee"] = (paid - sum(s for s, _ in outs)) / 1e8
        block["tx"].append(t)
    return block


def rebuild(h, blockhash, interrupted=None):
    """Block h as `getblock <hash> 3` would give it, or None when it cannot be rebuilt now (why is
    kept for report()). `interrupted()` is asked between requests and stops the work when it says so;
    what was fetched stays on disk."""
    node = local.node()
    try:
        header = node.getblock(blockhash, 1)
        raw = bytes.fromhex(node.getblock(blockhash, 0))
        if header.get("height") != h or header.get("hash") != blockhash:
            raise Unusable("the node answered with a different block")
        txs = split_block(raw, header["tx"])
        del raw
        # The second argument says outright whether the bytes carry witnesses, which the node would
        # otherwise guess at.
        decoded = _decode(node, [("decoderawtransaction", [t[0], t[0][8:12] == "0001"]) for t in txs])
        for (_, _, _), d, want in zip(txs, decoded, header["tx"]):
            if d.get("txid") != want:
                raise Unusable("the node decoded a different transaction than the block holds")

        # Every output the block spends: those of its own transactions first, the rest by id.
        own = {txid: outs for txid, (_, _, outs) in zip(header["tx"], txs)}
        spent, need = {}, {}
        for _, ins, _ in txs[1:]:
            for prev, vout in ins:
                if prev in own:
                    if vout >= len(own[prev]):
                        raise Unusable("an input spends an output its transaction does not have")
                    spent[f"{prev}:{vout}"] = list(own[prev][vout])
                else:
                    need.setdefault(prev, set()).add(vout)
        have = _load_work(h, blockhash)
        for key in have:
            prev = key.split(":")[0]
            if prev in need:
                spent[key] = have[key]
        todo = [t for t in need if any(f"{t}:{v}" not in spent for v in need[t])]
        client = _electrum()
        for i in range(0, len(todo), ASK):
            if interrupted and interrupted():
                _save_work(h, blockhash, have)
                return None
            chunk = todo[i:i + ASK]
            got = client.transactions(chunk)
            for prev in chunk:
                try:
                    _, txid, _, outs = parse_tx(bytes.fromhex(got[prev]))
                except (IndexError, ValueError):
                    raise Unusable(f"the Electrum server sent bytes that are not a transaction for {prev}") from None
                if VERIFY and txid != prev:
                    raise Unusable(f"the Electrum server sent a different transaction for {prev}")
                for vout in need[prev]:
                    if vout >= len(outs):
                        raise Unusable(f"transaction {prev} has no output {vout}")
                    spent[f"{prev}:{vout}"] = have[f"{prev}:{vout}"] = list(outs[vout])
            if i // ASK % 8 == 7:
                _save_work(h, blockhash, have)
            if PAUSE and i + ASK < len(todo):
                time.sleep(PAUSE)
        # Types and addresses for the scripts spent, from the node. Scripts of the block's own
        # transactions are decoded already.
        scripts = {}
        for d in decoded:
            for o in d["vout"]:
                s = o["scriptPubKey"]
                scripts[s["hex"]] = {k: v for k, v in s.items() if k != "hex"}
        unknown = sorted({script for _, script in spent.values()} - set(scripts))
        for script, d in zip(unknown, _decode(node, [("decodescript", [x]) for x in unknown])):
            scripts[script] = {k: d[k] for k in ("asm", "desc", "address", "type") if k in d}
        block = assemble(header, txs, decoded, spent, scripts)
    except Unusable as e:
        _fail(h, str(e), RETRY_MISSING)
        return None
    except electrum.Missing as e:
        _fail(h, f"your Electrum server does not have transaction {e.txid[:16]}… "
                 "(an Electrum server that needs an archival node cannot serve one below a pruned node's blocks)", RETRY_MISSING)
        return None
    except electrum.ElectrumError as e:
        _fail(h, str(e), RETRY_DOWN)
        return None
    except rpc.RpcError as e:
        # The proxy could not get the block from peers, or there is no proxy and the node has nothing.
        _fail(h, f"the node could not supply block {h} ({e.message})", RETRY_MISSING if e.pruned else RETRY_DOWN)
        return None
    block["cesspool"] = {"rebuilt": int(time.time())}  # nothing reads it; it says where this copy came from
    try:
        os.remove(_work_path(h))
    except OSError:
        pass
    s = _load()
    s = {"rebuilt": s.get("rebuilt", 0) + 1, "last": h}
    _save(s)
    return block
