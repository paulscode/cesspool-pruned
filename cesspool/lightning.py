"""The Lightning pages: the public channel graph as a Lightning node on this server sees it, joined to
the blocks this install has graded.

The Lightning node is read for three things only, all public: its view of the gossip graph (every
announced node and channel), its own identity, and whether it is in sync. Nothing here reads a
balance, a payment, an invoice or a private channel, and nothing is written to the node.

What cesspool adds to that graph comes from its own records:

  a channel's short id names the block, transaction and output that funded it, so a channel links
  to a graded block and transaction;
  every input of every graded transaction is on file (txd/<h>.json, "p"), so a channel's close is
  found by matching inputs against funding outpoints, with no index of spent outputs;
  classify.py already names Lightning's scripts, so the kind of close (cooperative, forced, forced
  and then punished) and what each node policy would do with a Lightning transaction are read from
  records that exist anyway.

A close is known only when it sits in a block graded here. A channel that leaves the graph with no
close seen is listed as no longer announced, not as closed.

Settings (environment):

  CESSPOOL_LN_BACKEND     lnd, cln, auto, or empty for no Lightning pages
  CESSPOOL_LND_REST_URL   LND's REST address (https://host:port); Lightning Fork is LND and uses the same
  CESSPOOL_LND_TLS_CERT   its tls.cert, which is the only certificate accepted
  CESSPOOL_LND_MACAROON   its readonly.macaroon
  CESSPOOL_CLN_SOCKET     Core Lightning's lightning-rpc socket
  CESSPOOL_LN_REFRESH     seconds between reads of the graph (default 3600)

  auto picks by the chain, for platforms where the user cannot choose (Umbrel): Lightning Fork on
  the BLAKE2b chain, LND and then Core Lightning on the SHA256 chain, from CESSPOOL_LN_FORK_DIR and
  CESSPOOL_LN_FORK_REST_URL, CESSPOOL_LN_LND_DIR and CESSPOOL_LN_LND_REST_URL, CESSPOOL_LN_CLN_DIR.

State, per chain: <HOME>/ln/nodes.json, channels.json, stats.json, info.json, scan.json.
Published: <out>/d/ln/ (static JSON for static/lightning.js) and <out>/lightning/ (pages).
"""
import base64, collections, datetime, json, os, re, shutil, socket, ssl, statistics, time, urllib.error, urllib.request

from . import found, local

DIR = f"{local.HOME}/ln"
NODES, CHANNELS, STATS, INFO, SCAN = (f"{DIR}/{n}.json" for n in ("nodes", "channels", "stats", "info", "scan"))
MACAROON = "data/chain/bitcoin/mainnet/readonly.macaroon"
REFRESH = int(local._number("CESSPOOL_LN_REFRESH", 3600))
GONE_DAYS = 30        # a channel that left the graph is kept this long, in case its close turns up
SCAN_SECONDS = 25     # how long one sync spends reading stored transaction files
TOP = 100             # rows in each ranking on file
NAMES = {"lnd": "LND", "fork": "Lightning Fork", "cln": "Core Lightning"}
# The transactions classify.known names that belong to Lightning, in the order the page lists them.
KINDS = ("ln-commitment", "two-of-two", "ln-to-local", "ln-to-remote", "ln-htlc", "ln-anchor", "ln-anchor-sweep", "ln-penalty")


class Unavailable(Exception):
    """The Lightning node cannot be read now; the message is for the status line."""


# ---------------------------------------------------------------- which node

def backend():
    """{"kind": "lnd"|"cln", "name", ...} for the Lightning node to read, or None for no Lightning pages."""
    want = local._env("CESSPOOL_LN_BACKEND").lower()
    if want == "lnd":
        url = local._env("CESSPOOL_LND_REST_URL")
        if not url:
            return None
        return {"kind": "lnd", "name": local._env("CESSPOOL_LN_NAME") or NAMES["lnd"], "url": url.rstrip("/"),
                "cert": local._env("CESSPOOL_LND_TLS_CERT"), "macaroon": local._env("CESSPOOL_LND_MACAROON")}
    if want == "cln":
        sock = local._env("CESSPOOL_CLN_SOCKET")
        return {"kind": "cln", "name": NAMES["cln"], "socket": sock} if sock else None
    if want == "auto":
        return _auto()
    return None


def _auto():
    """A Lightning node's channels exist on one chain, so the chain says which node to read: Lightning
    Fork on the BLAKE2b chain; on the SHA256 chain LND, and failing that Core Lightning. A node with no
    wallet yet has no macaroon, and is passed over until the next start."""
    def lnd(prefix, name):
        d, url = local._env(f"CESSPOOL_LN_{prefix}_DIR"), local._env(f"CESSPOOL_LN_{prefix}_REST_URL")
        if d and url and os.access(os.path.join(d, MACAROON), os.R_OK):
            return {"kind": "lnd", "name": name, "url": url.rstrip("/"), "cert": os.path.join(d, "tls.cert"), "macaroon": os.path.join(d, MACAROON)}
        return None

    chain = local.chain()
    if chain == "blake2b":
        return lnd("FORK", NAMES["fork"])
    if chain == "sha256":
        found_lnd = lnd("LND", NAMES["lnd"])
        if found_lnd:
            return found_lnd
        d = local._env("CESSPOOL_LN_CLN_DIR")
        sock = os.path.join(d, "bitcoin", "lightning-rpc") if d else ""
        if sock and _connects(sock):
            return {"kind": "cln", "name": NAMES["cln"], "socket": sock}
    return None


def _connects(path):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- reading a large JSON reply in pieces

def stream_arrays(read, keys, size=1 << 18):
    """Yield (key, element) for every element of the top-level arrays named in `keys`, reading the
    reply through `read(n)` a piece at a time. The whole graph of the SHA256 network is tens of
    megabytes of JSON and several times that once parsed, so it is never held all at once. Reading
    stops when the last of them closes: Core Lightning keeps its socket open after a reply, and
    waiting there for more would wait for nothing."""
    dec = json.JSONDecoder()
    buf, key, done = "", None, False
    left = set(keys)

    def more():
        nonlocal buf, done
        chunk = read(size)
        if not chunk:
            done = True
            return False
        buf += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        return True

    while True:
        if key is None:
            # Find the next wanted array's opening bracket.
            hit = None
            for k in keys:
                m = re.search(r'"%s"\s*:\s*\[' % re.escape(k), buf)
                if m and (hit is None or m.start() < hit[1].start()):
                    hit = (k, m)
            if hit is None:
                if done:
                    return
                buf = buf[-64:]  # keep a tail, in case a key straddles two pieces
                if not more():
                    return
                continue
            key, buf = hit[0], buf[hit[1].end():]
        buf = buf.lstrip(" \t\r\n,")
        if not buf:
            if not more():
                return
            continue
        if buf[0] == "]":
            left.discard(key)
            if not left:
                return
            key, buf = None, buf[1:]
            continue
        try:
            obj, end = dec.raw_decode(buf)
        except ValueError:
            if not more():
                raise Unavailable("the Lightning node's reply ended in the middle of the graph") from None
            continue
        if end == len(buf) and not done:
            # A number or string at the very end of the piece may not be whole yet.
            if not isinstance(obj, (dict, list)) and more():
                continue
        yield key, obj
        buf = buf[end:]


# ---------------------------------------------------------------- LND (and Lightning Fork) over REST

class Lnd:
    def __init__(self, cfg):
        self.cfg = cfg

    def _open(self, path, timeout):
        cfg = self.cfg
        try:
            with open(cfg["macaroon"], "rb") as f:
                macaroon = f.read().hex()
        except OSError as e:
            raise Unavailable(f"cannot read the macaroon: {e.strerror or e} (has the Lightning node's wallet been created?)") from None
        # tls.cert is the node's own self-signed certificate, so it is the only trust anchor: the
        # connection is accepted only from the holder of that certificate's key. The address the
        # platform gives for the node is often not among the names the certificate lists, and with
        # the certificate itself pinned the name adds nothing, so it is not checked.
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=cfg["cert"])
        except (OSError, ssl.SSLError) as e:
            raise Unavailable(f"cannot read the node's tls.cert: {e}") from None
        req = urllib.request.Request(cfg["url"] + path, headers={"Grpc-Metadata-macaroon": macaroon})
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except urllib.error.HTTPError as e:
            raise Unavailable(f"the Lightning node answered {e.code} for {path}: {e.read(200).decode('utf-8', 'replace').strip()}") from None
        except (urllib.error.URLError, OSError) as e:
            raise Unavailable(f"cannot reach the Lightning node at {cfg['url']}: {getattr(e, 'reason', e)}") from None

    def info(self):
        with self._open("/v1/getinfo", 30) as r:
            i = json.load(r)
        return {"pub": i.get("identity_pubkey", ""), "alias": i.get("alias", ""), "color": i.get("color", ""),
                "version": i.get("version", ""), "height": int(i.get("block_height") or 0), "hash": i.get("block_hash", ""),
                "chain_synced": bool(i.get("synced_to_chain")), "graph_synced": bool(i.get("synced_to_graph"))}

    def graph(self):
        """Yield ("node", {...}) and ("channel", {...}) in the shape normal_node and normal_channel return."""
        with self._open("/v1/graph", 600) as r:
            for key, x in stream_arrays(r.read, ("nodes", "edges")):
                if not isinstance(x, dict):
                    continue
                if key == "nodes":
                    yield "node", normal_node(x.get("pub_key"), x.get("alias"), x.get("color"),
                                              [a.get("addr", "") for a in x.get("addresses") or [] if isinstance(a, dict)], x.get("last_update"))
                else:
                    scid = scid_from_lnd(x.get("channel_id"))
                    if scid:
                        yield "channel", normal_channel(scid, x.get("node1_pub"), x.get("node2_pub"), x.get("capacity"), x.get("chan_point"),
                                                        lnd_policy(x.get("node1_policy")), lnd_policy(x.get("node2_policy")), x.get("last_update"))


def lnd_policy(p):
    if not isinstance(p, dict):
        return None
    return [_int(p.get("fee_base_msat")), _int(p.get("fee_rate_milli_msat")), _int(p.get("time_lock_delta")),
            _int(p.get("min_htlc")), _int(p.get("max_htlc_msat")), 1 if p.get("disabled") else 0]


# ---------------------------------------------------------------- Core Lightning over its socket

class Cln:
    def __init__(self, cfg):
        self.cfg = cfg

    def _call(self, method, timeout=600):
        """The open socket's file after sending one request; the caller reads the reply from it."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.cfg["socket"])
            s.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode())
            return s
        except OSError as e:
            raise Unavailable(f"cannot reach Core Lightning at {self.cfg['socket']}: {e.strerror or e}") from None

    def info(self):
        s = self._call("getinfo", 30)
        buf = b""
        try:
            while not buf.endswith(b"\n\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            i = json.loads(buf).get("result") or {}
        except (OSError, ValueError) as e:
            raise Unavailable(f"Core Lightning's answer could not be read: {e}") from None
        finally:
            s.close()
        return {"pub": i.get("id", ""), "alias": i.get("alias", ""), "color": "#" + str(i.get("color", "")),
                "version": i.get("version", ""), "height": int(i.get("blockheight") or 0), "hash": "",
                "chain_synced": not i.get("warning_bitcoind_sync"), "graph_synced": not i.get("warning_lightningd_sync")}

    def graph(self):
        s = self._call("listnodes")
        try:
            for _, x in stream_arrays(s.recv, ("nodes",)):
                if isinstance(x, dict) and x.get("nodeid"):
                    addrs = []
                    for a in x.get("addresses") or []:
                        host = f"[{a.get('address')}]" if a.get("type") == "ipv6" else a.get("address")
                        addrs.append(f"{host}:{a.get('port')}")
                    yield "node", normal_node(x["nodeid"], x.get("alias"), "#" + str(x.get("color", "")), addrs, x.get("last_timestamp"))
        finally:
            s.close()
        # listchannels has one entry for each direction of a channel: the first seen opens the
        # channel with that side's policy, the second fills in the other side's.
        s = self._call("listchannels")
        half = {}
        try:
            for _, x in stream_arrays(s.recv, ("channels",)):
                if not isinstance(x, dict):
                    continue
                scid = scid_parse(x.get("short_channel_id"))
                if not scid:
                    continue
                policy = [_int(x.get("base_fee_millisatoshi")), _int(x.get("fee_per_millionth")), _int(x.get("delay")),
                          _msat(x.get("htlc_minimum_msat")), _msat(x.get("htlc_maximum_msat")), 0 if x.get("active") else 1]
                first = half.pop(scid, None)
                if first is None:
                    half[scid] = (x.get("source"), x.get("destination"), _msat(x.get("amount_msat")) // 1000, policy, _int(x.get("last_update")))
                else:
                    a, b, cap, pa, upd = first
                    yield "channel", normal_channel(scid, a, b, cap, None, pa, policy, max(upd, _int(x.get("last_update"))))
        finally:
            s.close()
        for scid, (a, b, cap, pa, upd) in half.items():
            yield "channel", normal_channel(scid, a, b, cap, None, pa, None, upd)


def client(cfg):
    return Lnd(cfg) if cfg["kind"] == "lnd" else Cln(cfg)


# ---------------------------------------------------------------- one shape for both

HEX66 = re.compile(r"^0[23][0-9a-f]{64}$")
COLOR = re.compile(r"^#[0-9a-f]{6}$")
OUTPOINT = re.compile(r"^[0-9a-f]{64}:\d{1,5}$")


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _msat(x):
    """Core Lightning writes millisatoshi as a number, or in older releases as '1000msat'."""
    if isinstance(x, str) and x.endswith("msat"):
        x = x[:-4]
    return _int(x)


def clean_alias(alias):
    """An alias is text its node's operator chose, and anyone can announce anything: keep printable
    characters only, drop the direction controls that reorder a line, and cap the length."""
    if not isinstance(alias, str):
        return ""
    out = "".join(c for c in alias if c.isprintable() and not ("‪" <= c <= "‮" or "⁦" <= c <= "⁩" or c in "‎‏﻿"))
    return out.strip()[:40]


def scid_from_lnd(channel_id):
    """LND's numeric channel id packs block height, transaction index and output index: 'HxTxO'."""
    n = _int(channel_id)
    if n <= 0 or n >= 1 << 64:
        return None
    return f"{n >> 40}x{(n >> 16) & 0xffffff}x{n & 0xffff}"


def scid_parse(s):
    """'HxTxO' in canonical form from that form or from LND's number, else None."""
    if isinstance(s, str) and re.fullmatch(r"\d{1,8}x\d{1,8}x\d{1,5}", s):
        h, t, o = (int(x) for x in s.split("x"))
        return f"{h}x{t}x{o}" if t < 1 << 24 and o < 1 << 16 else None
    if isinstance(s, (int, str)) and re.fullmatch(r"\d{9,20}", str(s)):
        return scid_from_lnd(s)
    return None


def scid_to_lnd(scid):
    h, t, o = (int(x) for x in scid.split("x"))
    return (h << 40) | (t << 16) | o


def scid_height(scid):
    return int(scid.split("x")[0])


def normal_node(pub, alias, color, addrs, upd):
    pub = str(pub or "").lower()
    if not HEX66.match(pub):
        return None
    color = str(color or "").lower()
    return {"pub": pub, "alias": clean_alias(alias), "color": color if COLOR.match(color) else "",
            "addrs": [a for a in (str(x)[:120] for x in addrs or []) if a.isprintable()][:8], "upd": _int(upd)}


def normal_channel(scid, a, b, cap, point, pa, pb, upd):
    a, b = str(a or "").lower(), str(b or "").lower()
    if not (HEX66.match(a) and HEX66.match(b)):
        return None
    point = str(point or "").lower()
    return {"id": scid, "a": a, "b": b, "cap": max(0, _int(cap)), "point": point if OUTPOINT.match(point) else None,
            "pa": pa, "pb": pb, "upd": _int(upd)}


# ---------------------------------------------------------------- state

def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, path)


def info():
    return _read(INFO, {})


def shown():
    """Whether the site has Lightning pages: a Lightning node is set up and its graph has been read
    at least once for this chain."""
    return bool(backend()) and bool(info().get("read"))


def due():
    i = info()
    wait = REFRESH if i.get("ok") else min(REFRESH, 300)  # a node that was not ready is asked again sooner
    return bool(backend()) and time.time() - i.get("tried", 0) >= wait


def _fail(cfg, message):
    i = info()
    if i.get("error") != message:
        print(f"lightning: {message}", flush=True)
    i.update(backend=cfg["kind"], name=cfg["name"], ok=False, error=message, tried=int(time.time()))
    _write(INFO, i)
    return False


def sync(index=None):
    """Read the graph and bring nodes.json, channels.json and stats.json in line with it. Returns
    True when the graph was read. `index` is the block index ({height: summary}), for the chain check
    and for a Core Lightning channel's funding transaction."""
    cfg = backend()
    if not cfg:
        return False
    now = int(time.time())
    try:
        c = client(cfg)
        me = c.info()
        # The Lightning node has to follow the chain this install grades: its channels mean nothing
        # beside another chain's blocks. Its best block is checked against the Bitcoin node's.
        if me["hash"] and me["height"] >= local.FORK:
            try:
                ours = local.node().getblockhash(me["height"])
            except Exception:  # noqa: BLE001 - our node is behind, or away: no verdict, try next time
                ours = None
            if ours and ours != me["hash"]:
                return _fail(cfg, f"{cfg['name']} is on a different chain from this install's Bitcoin node (their blocks at height {me['height']} differ)")
        nodes, chans = {}, {}
        for kind, x in c.graph():
            if x is None:
                continue
            if kind == "node":
                nodes[x["pub"]] = x
            else:
                chans[x["id"]] = x
    except Unavailable as e:
        return _fail(cfg, str(e))
    except (OSError, ValueError) as e:
        return _fail(cfg, f"reading the graph from {cfg['name']} failed partway: {e}")
    if not chans or not nodes:
        # A node still collecting gossip has an empty graph. That is "not ready", not "no channels".
        return _fail(cfg, f"{cfg['name']} has no channels in its graph yet: it is probably still syncing")

    old_nodes, old_chans = _read(NODES, {}), _read(CHANNELS, {})
    out_nodes = {}
    for pub, x in nodes.items():
        prev = old_nodes.get(pub) or {}
        out_nodes[pub] = {"al": x["alias"], "co": x["color"], "ad": x["addrs"], "u": x["upd"], "f": prev.get("f", now), "l": now}
    for pub, prev in old_nodes.items():
        if pub not in out_nodes and now - prev.get("l", 0) < GONE_DAYS * 86400:
            out_nodes[pub] = prev
    out_chans = {}
    for scid, x in chans.items():
        prev = old_chans.get(scid) or {}
        ch = {"a": x["a"], "b": x["b"], "c": x["cap"], "p": x["point"] or prev.get("p"), "pa": x["pa"], "pb": x["pb"], "u": x["upd"],
              "f": prev.get("f", now), "l": now, "s": "open"}
        if "v" in prev and prev.get("c") == ch["c"] and prev.get("p") == ch["p"]:
            ch["v"] = prev["v"]
        if prev.get("x"):
            ch["x"], ch["s"] = prev["x"], "closed"  # its close is on file; the graph has not caught up
        out_chans[scid] = ch
    for scid, prev in old_chans.items():
        if scid in out_chans:
            continue
        if prev.get("x"):
            out_chans[scid] = dict(prev, s="closed")
        elif now - prev.get("l", 0) < GONE_DAYS * 86400:
            out_chans[scid] = dict(prev, s="gone")
    _write(NODES, out_nodes)
    _write(CHANNELS, out_chans)

    # One line a day for the charts; a later read on the same day replaces it.
    open_caps = sorted(ch["c"] for ch in out_chans.values() if ch["s"] == "open")
    active = {p for ch in out_chans.values() if ch["s"] == "open" for p in (ch["a"], ch["b"])}
    tor = sum(1 for p in active if (out_nodes.get(p) or {}).get("ad") and all(".onion" in a for a in out_nodes[p]["ad"]))
    clear = sum(1 for p in active if any(".onion" not in a for a in (out_nodes.get(p) or {}).get("ad") or []))
    day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d")
    row = {"d": day, "t": now, "n": len(active), "c": len(open_caps), "cap": sum(open_caps),
           "med": int(statistics.median(open_caps)) if open_caps else 0, "tor": tor, "clear": clear}
    stats = [s for s in _read(STATS, []) if s.get("d") != day] + [row]
    _write(STATS, stats[-730:])

    i = info()
    i.update(backend=cfg["kind"], name=cfg["name"], ok=True, error=None, tried=now, read=now,
             node={k: me[k] for k in ("pub", "alias", "color", "version", "height", "chain_synced", "graph_synced")},
             graph={"nodes": len(nodes), "channels": len(chans)})
    i["node"]["alias"] = clean_alias(i["node"]["alias"])
    i["node"]["color"] = i["node"]["color"].lower() if COLOR.match(str(i["node"]["color"]).lower()) else ""
    _write(INFO, i)
    return True


# ---------------------------------------------------------------- closes, and Lightning under the filters

def _txd(h):
    with open(f"{local.TXD}/{h}.json") as f:
        return json.load(f)


def _tx_at(h, index):
    """The id of transaction `index` of block h, from its transaction file; None when it is not on file."""
    try:
        d = _txd(h)
        return d["cb"]["id"] if index == 0 else d["tx"][index - 1]["id"]
    except (OSError, ValueError, KeyError, IndexError):
        return None


def close_kind(tx, raw):
    """How a transaction that spends a channel's funding output closed it. `tx` is its record from the
    transaction file, `raw` the same transaction as the node gave it, or None when that copy is gone.

    A commitment transaction is recognizable whatever the funding script: BOLT 3 puts 0x20 in the top
    byte of its lock time and 0x80 in the top byte of its sequence. One of those is a forced close.
    Anything else that spends the funding output took both parties' signatures: a cooperative close,
    or a splice that moves the funds to a new funding output. A forced close becomes a penalty when a
    later transaction takes an output through the revocation key; scan() looks for that afterwards."""
    if tx.get("kp") == "ln-commitment":
        return "force"
    if raw is None:
        return "mutual" if tx.get("kp") == "two-of-two" else "unknown"
    seq = raw["vin"][0].get("sequence", 0) if len(raw.get("vin", [])) == 1 else 0
    if raw.get("locktime", 0) >> 24 == 0x20 and seq >> 24 == 0x80:
        return "force"
    return "mutual"


def _raw_tx(h, txid):
    try:
        import gzip
        with gzip.open(f"{local.RAW}/{h}.json.gz") as f:
            for t in json.load(f)["tx"]:
                if t["txid"] == txid:
                    return t
    except (OSError, ValueError, KeyError, EOFError):
        pass
    return None


def scan(index, budget=SCAN_SECONDS):
    """Read the transaction files of graded blocks for two things: inputs that spend a channel's
    funding output (its close) or an output of a forced close (a sweep, possibly a penalty), and
    every transaction classify.known named as Lightning, with what each policy does with it.

    Each block is read once, and again when its record changes (a reorg). Newest first, for as long
    as `budget` seconds allow; the rest waits for the next sync. Returns True when anything changed."""
    chans = _read(CHANNELS, {})
    state = _read(SCAN, {"h": {}})
    seen = state["h"]
    heights = sorted(index, reverse=True)
    changed = False
    # A record that is gone or replaced takes back what it said: closes at that height are undone.
    live = {str(h): index[h].get("_m") for h in heights}
    for k in [k for k in seen if live.get(k) != seen[k].get("m")]:
        del seen[k]
        changed = True
    for scid, ch in chans.items():
        x = ch.get("x")
        if x and str(x["h"]) not in seen:
            del ch["x"]
            ch["s"] = "open" if time.time() - ch.get("l", 0) < 2 * REFRESH else "gone"
            changed = True

    # Resolve funding outpoints the graph did not give (Core Lightning's gossip has only the short id).
    by_height = collections.defaultdict(list)
    for scid, ch in chans.items():
        if not ch.get("p"):
            by_height[scid_height(scid)].append(scid)
    for h, scids in by_height.items():
        if h in index:
            for scid in scids:
                _, t, o = (int(v) for v in scid.split("x"))
                txid = _tx_at(h, t)
                if txid:
                    chans[scid]["p"] = f"{txid}:{o}"
                    changed = True

    changed = _verify(chans, index) or changed
    funding = {ch["p"]: scid for scid, ch in chans.items() if ch.get("p") and not ch.get("x")}
    stop = time.monotonic() + budget
    for h in heights:
        k = str(h)
        if k in seen:
            continue
        if time.monotonic() > stop:
            break
        try:
            d = _txd(h)
        except (OSError, ValueError):
            continue
        tally, flagged, penalties = {}, [], []
        for t in d.get("tx", []):
            kp = t.get("kp")
            if kp == "ln-penalty":
                # Which transactions it takes outputs from: one of them may be a forced close.
                penalties.append([t["id"], sorted({i["p"].split(":")[0] for i in t.get("i", []) if i.get("p")})])
            if kp in KINDS:
                x = t.get("x") or {}
                e = tally.setdefault(kp, [0, 0, 0, 0, 0, 0])  # seen, Core refuses, Knots refuses, Plumb refuses, gray, sewage
                e[0] += 1
                for i, name in enumerate(("core", "knots", "plumb")):
                    e[1 + i] += bool(x.get(name))
                e[4] += t.get("k") == "gray"
                e[5] += t.get("k") == "sewage"
                if any(x.get(name) for name in ("core", "knots", "plumb")) or t.get("k") in ("gray", "sewage"):
                    flagged.append([t["id"], kp, t.get("k") or "clean", {n: x[n] for n in ("core", "knots", "plumb") if x.get(n)}])
            for inp in t.get("i", []):
                p = inp.get("p")
                scid = funding.get(p)
                if scid:
                    kind = close_kind(t, None if t.get("kp") in ("ln-commitment", "two-of-two") else _raw_tx(h, t["id"]))
                    chans[scid]["x"] = {"t": t["id"], "h": h, "bh": d.get("hash"), "k": kind, "at": d.get("t")}
                    chans[scid]["s"] = "closed"
                    del funding[p]
                    changed = True
        seen[k] = {"m": index[h].get("_m"), "t": tally, "f": flagged[:50], **({"pen": penalties} if penalties else {})}
        changed = True
    # A forced close becomes a penalty when a transaction takes one of its outputs through the
    # revocation branch. Blocks are read newest first, so the penalty is often on file before the
    # close it punishes: the two are matched here, from every block read so far.
    took = {}
    for k, s in seen.items():
        for txid, prevs in s.get("pen") or []:
            for prev in prevs:
                took[prev] = (txid, int(k))
    for ch in chans.values():
        x = ch.get("x")
        if x and x["k"] in ("force", "penalty"):
            hit = took.get(x["t"])
            if hit and x.get("pen") != hit[0]:
                x.update(k="penalty", pen=hit[0], penh=hit[1])
                changed = True
            elif not hit and x["k"] == "penalty":
                x.pop("pen", None)
                x.pop("penh", None)
                x["k"] = "force"  # the block that held the penalty was replaced
                changed = True
    if changed:
        _write(CHANNELS, chans)
        _write(SCAN, state)
    return changed


def _verify(chans, index, most=60):
    """Check channels against the blocks graded here: the transaction at the place a channel's short
    id names has to be the one its funding outpoint names, and that output has to hold the channel's
    capacity. Each channel is checked once ("v": 1 it matches, 0 it does not), a few blocks' worth a
    pass. A mismatch means the Lightning node and this install disagree about what is in a block."""
    todo = collections.defaultdict(list)
    for scid, ch in chans.items():
        if "v" not in ch and ch.get("p") and scid_height(scid) in index:
            todo[scid_height(scid)].append(scid)
    for h in sorted(todo, reverse=True)[:most]:
        try:
            d = _txd(h)
        except (OSError, ValueError):
            continue
        for scid in todo[h]:
            ch = chans[scid]
            _, t, o = (int(v) for v in scid.split("x"))
            txs = d.get("tx", [])
            tx = d.get("cb") if t == 0 else txs[t - 1] if t - 1 < len(txs) else None
            outs = (tx or {}).get("o", [])
            ch["v"] = 1 if tx and ch["p"] == f'{tx["id"]}:{o}' and o < len(outs) and outs[o].get("a") == ch["c"] else 0
    return bool(todo)


def _unread(index):
    """True while some graded block's transaction file has not been read yet (scan() works in turns)."""
    seen = _read(SCAN, {"h": {}})["h"]
    return any(str(h) not in seen for h in index)


def filters(index):
    """{kind: [seen, Core refuses, Knots refuses, Plumb refuses, gray, sewage]} over every block read,
    and the newest transactions any policy refuses or the site lists, as [height, txid, kind, tier, reasons]."""
    state = _read(SCAN, {"h": {}})
    total = {}
    flagged = []
    for k, s in state["h"].items():
        for kind, e in (s.get("t") or {}).items():
            cur = total.setdefault(kind, [0, 0, 0, 0, 0, 0])
            for i, v in enumerate(e):
                cur[i] += v
        for f in s.get("f") or []:
            flagged.append([int(k)] + f)
    flagged.sort(key=lambda f: -f[0])
    return total, flagged, len(state["h"])


# ---------------------------------------------------------------- what the pages are drawn from

def view():
    """Everything the pages and the JSON files need, worked out once from the state files."""
    nodes, chans = _read(NODES, {}), _read(CHANNELS, {})
    per = collections.defaultdict(lambda: {"cap": 0, "n": 0, "old": None, "closed": 0})
    for scid, ch in chans.items():
        for pub in (ch["a"], ch["b"]):
            e = per[pub]
            if ch["s"] == "open":
                e["cap"] += ch["c"]
                e["n"] += 1
                h = scid_height(scid)
                e["old"] = h if e["old"] is None or h < e["old"] else e["old"]
            elif ch["s"] == "closed":
                e["closed"] += 1
    return nodes, chans, per


def alias_of(nodes, pub):
    return (nodes.get(pub) or {}).get("al") or pub[:16] + "…"


def publish_data(out, nodes, chans, per, index):
    """The static JSON under <out>/d/ln/ that static/lightning.js reads. Files are written only when
    their content changes, and none is looked at when the state has not changed since the last time."""
    root = f"{out}/d/ln"
    stamp, stamp_file = _stamp(), f"{out}/d/ln/.stamp"
    fresh = _read(stamp_file, None) == stamp and os.path.exists(f"{root}/summary.json.gz")
    stats = _read(STATS, [])
    i = info()
    open_chans = {s: c for s, c in chans.items() if c["s"] == "open"}
    caps = sorted(c["c"] for c in open_chans.values())
    live = [p for p, e in per.items() if e["n"]]

    def row(pub):
        e = per[pub]
        return [pub, alias_of(nodes, pub), (nodes.get(pub) or {}).get("co", ""), e["cap"], e["n"], e["old"]]

    tops = {"capacity": [row(p) for p in sorted(live, key=lambda p: (-per[p]["cap"], p))[:TOP]],
            "channels": [row(p) for p in sorted(live, key=lambda p: (-per[p]["n"], p))[:TOP]],
            "oldest": [row(p) for p in sorted(live, key=lambda p: (per[p]["old"], p))[:TOP]]}
    closes = sorted(([s, c["a"], c["b"], c["c"], c["x"]] for s, c in chans.items() if c.get("x")), key=lambda r: (-r[4]["h"], r[0]))
    by_kind = collections.Counter(r[4]["k"] for r in closes)
    summary = {"name": i.get("name"), "read": i.get("read"), "node": i.get("node"), "ok": i.get("ok"), "error": i.get("error"),
               "totals": {"nodes": len(live), "channels": len(open_chans), "capacity": sum(caps),
                          "avg": sum(caps) // len(caps) if caps else 0, "median": int(statistics.median(caps)) if caps else 0,
                          "gone": sum(1 for c in chans.values() if c["s"] == "gone")},
               "stats": [[s["d"], s["n"], s["c"], s["cap"]] for s in stats], "tops": tops,
               "closes": {"mutual": by_kind["mutual"], "force": by_kind["force"], "penalty": by_kind["penalty"], "unknown": by_kind["unknown"]},
               "checked": {"ok": sum(1 for c in chans.values() if c.get("v") == 1), "bad": sum(1 for c in chans.values() if c.get("v") == 0)}}
    if fresh:
        return summary, closes
    found.write_gz_if_changed(f"{root}/summary.json.gz", summary)

    # Nodes, in 256 files by the byte after the 02 or 03 that starts every key.
    shards = collections.defaultdict(dict)
    links = collections.defaultdict(list)
    for scid, c in chans.items():
        x = c.get("x")
        for me, peer in ((c["a"], c["b"]), (c["b"], c["a"])):
            links[me].append([scid, peer, alias_of(nodes, peer), c["c"], c["s"], x["k"] if x else "", x["h"] if x else 0])
    for pub in set(nodes) | set(links):
        nd = nodes.get(pub) or {}
        e = per.get(pub) or {"cap": 0, "n": 0, "old": None, "closed": 0}
        shards[pub[2:4]][pub] = {"al": nd.get("al", ""), "co": nd.get("co", ""), "ad": nd.get("ad", []), "f": nd.get("f"), "l": nd.get("l"),
                                 "cap": e["cap"], "n": e["n"], "ch": sorted(links.get(pub, []), key=lambda r: -scid_height(r[0]))}
    for b in range(256):
        key = f"{b:02x}"
        found.write_gz_if_changed(f"{root}/n/{key}.json.gz", shards.get(key, {}))

    # Channels by the thousand of their funding height, as d/s/ and the API group blocks.
    groups = collections.defaultdict(dict)
    for scid, c in chans.items():
        x = c.get("x")
        groups[scid_height(scid) // 1000][scid] = {
            "a": c["a"], "b": c["b"], "aa": alias_of(nodes, c["a"]), "ba": alias_of(nodes, c["b"]), "c": c["c"], "p": c.get("p"),
            "pa": c.get("pa"), "pb": c.get("pb"), "s": c["s"], "f": c.get("f"), "l": c.get("l"),
            **({"x": {k: x[k] for k in ("t", "h", "k", "at", "pen", "penh") if k in x}} if x else {})}
    _sync_dir(f"{root}/c", {f"{k}.json.gz": v for k, v in groups.items()})

    # Closes by the thousand of the height they were mined at, for a block page's count.
    closed = collections.defaultdict(lambda: collections.defaultdict(list))
    for scid, a, b, cap, x in closes:
        closed[x["h"] // 1000][str(x["h"])].append([scid, x["k"]])
    _sync_dir(f"{root}/x", {f"{k}.json.gz": v for k, v in closed.items()})

    # Transaction id to channel and role, for the note on a transaction's page.
    by_tx = collections.defaultdict(lambda: collections.defaultdict(list))
    for scid, c in chans.items():
        names = [alias_of(nodes, c["a"]), alias_of(nodes, c["b"])]
        if c.get("p"):
            by_tx[found.shard(c["p"].split(":")[0])][c["p"].split(":")[0]].append([scid, "open", names, ""])
        x = c.get("x")
        if x:
            by_tx[found.shard(x["t"])][x["t"]].append([scid, "close", names, x["k"]])
            if x.get("pen"):
                by_tx[found.shard(x["pen"])][x["pen"]].append([scid, "penalty", names, ""])
    for b in range(256):
        key = f"{b:02x}"
        found.write_gz_if_changed(f"{root}/tx/{key}.json.gz", by_tx.get(key, {}))

    # Names for the search box: alias in lower case to the keys that announce it.
    names = collections.defaultdict(list)
    for pub in live:
        al = alias_of(nodes, pub).lower()
        names[al].append(pub)
    found.write_gz_if_changed(f"{root}/alias.json.gz", dict(sorted(names.items())))
    _write(stamp_file, stamp)
    return summary, closes


def _sync_dir(directory, files):
    """Write each of `files` ({name: object}) under `directory`, and remove the files no longer named."""
    for name, obj in files.items():
        found.write_gz_if_changed(f"{directory}/{name}", obj)
    try:
        for name in os.listdir(directory):
            if name.endswith(".json.gz") and name not in files:
                os.remove(f"{directory}/{name}")
    except OSError:
        pass


# ---------------------------------------------------------------- pages

def publish(out, idx, tip, page):
    """Called at the end of every build, and after every read of the graph: the Lightning pages and
    their data. Without a Lightning node there are no such pages, and any left from before go."""
    # Pages that were drawn before Lightning was set up, or after it was taken away, learn from this
    # file whether to link to the Lightning pages (static/lightning.js).
    found.write_gz_if_changed(f"{out}/d/features.json.gz", {"lightning": shown()})
    if not shown():
        for d in (f"{out}/lightning", f"{out}/d/ln"):
            shutil.rmtree(d, ignore_errors=True)
        return
    from . import pages_ln
    nodes, chans, per = view()
    summary, closes = publish_data(out, nodes, chans, per, idx)
    pages_ln.write_all(out, idx, tip, page, summary, closes, nodes, chans, per, filters(idx), info())


def _stamp():
    """The state files' dates: when none has moved, nothing drawn from them has either."""
    out = []
    for p in (NODES, CHANNELS, STATS, INFO, SCAN):
        try:
            out.append(os.path.getmtime(p))
        except OSError:
            out.append(0)
    return out


def main(out):
    """One pass, for tools/lightning_sync.py: read the graph when it is due, look for closes, and
    publish. The caller holds the update lock."""
    if not backend():
        return
    try:
        with open(f"{local.HOME}/index.json") as f:
            index = {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        index = {}
    read = sync(index) if due() else False
    if not info().get("read"):
        return
    # Reading the transaction files again is only worth it when a block has been graded since.
    seen = _read(f"{DIR}/seen.json", {})
    now = {"index": os.path.getmtime(f"{local.HOME}/index.json") if index else 0, "read": info().get("read")}
    if not read and seen == now and not _unread(index):
        return
    changed = scan(index)
    _write(f"{DIR}/seen.json", now)
    if (read or changed) and index:
        from . import site
        tip = index[max(index)]
        local.begin_build(index)
        publish(out, index, tip, site.page)
