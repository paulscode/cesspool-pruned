"""What Cesspool Pruned adds to cesspool for an install on someone's own server: its settings, which
chain the node is on, which blocks it has graded, and the words on the pages that depend on those.

cesspool.lol grades one chain from one archival node, from the fork to the tip, with no block missing.
An install here may sit behind a pruned node, on either chain, and start anywhere. The upstream modules
reach this file through one-line hooks, so that merging upstream stays cheap; nothing here is imported
by a module it imports.

Settings (environment):

  CESSPOOL_RPC_URL        http://host:port of the node's RPC, or of the btc-rpc-proxy in front of it
  CESSPOOL_RPC_USER       RPC user, with CESSPOOL_RPC_PASS; empty to use the cookie
  CESSPOOL_RPC_COOKIE     the node's .cookie file, or its data directory
  CESSPOOL_ELECTRUM_HOST  an Electrum server (plaintext TCP), with CESSPOOL_ELECTRUM_PORT; it lets
                          history below the node's prune height be rebuilt (backfill.py)
  CESSPOOL_BACKFILL       1 (the default) to rebuild that history when there is an Electrum server
  CESSPOOL_SITE           the site's own address, for share images and the feed; empty for relative links
  CESSPOOL_OG             1 to draw share images; off by default
  CESSPOOL_MAX_GB         how much disk this chain's blocks and pages may take (default 50; 0 for no
                          limit). History fills in until it is reached, and from then on the oldest
                          blocks are dropped as new ones arrive
  CESSPOOL_MIN_FREE_MB    stop storing blocks when the disk has less than this free (default 2048)
  CESSPOOL_HOME           the state directory, one per chain (the supervisor sets it)
  CESSPOOL_CHAIN          blake2b or sha256 (the supervisor sets it, after asking the node)
  CESSPOOL_SETTINGS_PAGE  1 to offer backfill and the disk ceiling as a form on /about/, for
                          platforms with no settings form (cesspool/settings.py)
"""
import gzip, json, os, shutil, time

from . import rpc, settings

FORK = 961640  # the first block cesspool grades, on either chain; site.FORK is the same number
# Block 961640 on the BLAKE2b chain. The chains share every block up to 961631, and the node reports
# "main" for both, so the hash at this height is what tells them apart.
BLAKE2B_961640 = "0000000000000050c1e5f69672f459293be14f46e5a494e7a8c8541396f18eeb"
CHAINS = {"blake2b": "BLAKE2b", "sha256": "SHA256"}

REPO = "https://github.com/paulscode/cesspool-pruned"
UPSTREAM = "https://cesspool.lol"
UPSTREAM_REPO = "https://github.com/jasonsopko/cesspool"
BRAND = "Cesspool Pruned"


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _flag(name, default):
    v = _env(name).lower()
    return default if not v else v in ("1", "true", "yes", "on")


def _number(name, default):
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


HOME = os.environ.get("CESSPOOL_HOME", os.path.expanduser("~/.cesspool"))  # as every upstream module reads it
BLOCKS, RAW, TXD = f"{HOME}/blocks", f"{HOME}/raw", f"{HOME}/txd"
COVERAGE = f"{HOME}/coverage.json"
SITE = _env("CESSPOOL_SITE").rstrip("/")
OG = _flag("CESSPOOL_OG", False)
FONTS = _env("CESSPOOL_FONTS") or os.path.expanduser("~/.local/share/fonts/plumb")
POOLS = _env("CESSPOOL_POOLS")  # the directory holding each chain's pool list
# Backfill and the disk ceiling can also be set from the site (cesspool/settings.py), where a
# platform has no settings form; what the user chose there wins over the environment.
_chosen = settings.read()
BACKFILL = _chosen["backfill"] if "backfill" in _chosen else _flag("CESSPOOL_BACKFILL", True)
ELECTRUM = (_env("CESSPOOL_ELECTRUM_HOST"), int(_number("CESSPOOL_ELECTRUM_PORT", 50001)))
MIN_FREE = int(_number("CESSPOOL_MIN_FREE_MB", 2048)) * 2**20
# A block with its records and pages takes about 0.6 MB on the BLAKE2b chain and about 8 MB on the
# SHA256 chain, where blocks are full: over a gigabyte a day. So there is a ceiling, and it is a size.
MAX_BYTES = int((_chosen["max_gb"] if "max_gb" in _chosen else _number("CESSPOOL_MAX_GB", 50)) * 2**30)
KEEP_LEAST = 288  # blocks no ceiling ever takes: two days of them
STATUS = _env("CESSPOOL_STATUS") or os.path.join(_env("CESSPOOL_DATA") or HOME, "status.json")

# A first run, or a run after a long stop, begins this close to the tip, so the site shows the newest
# blocks within minutes; the blocks below are filled in afterwards, newest first.
RECENT = 12
# One update fills history for about this long, so a new block never waits much longer than that.
FILL_SECONDS = 45
FILL_MOST = 100
# While history fills in, the summary pages are rendered again this often, not for every batch.
BUILD_EVERY = 300
BUILD_BLOCKS = 300  # ... or once this many blocks wait for their pages (address.py rebuilds everything over 500)
# process_range holds every block of a batch in memory; a SHA256 block is some 12 MB of JSON.
BATCH_BYTES = 24 * 2**20


# ---------------------------------------------------------------- the node

_node = None
_info = None  # getblockchaininfo, as of this update


def node():
    global _node
    if _node is None:
        url = _env("CESSPOOL_RPC_URL")
        if not url:
            raise rpc.Unreachable("CESSPOOL_RPC_URL is not set: there is no node to ask")
        _node = rpc.Client(url, _env("CESSPOOL_RPC_USER"), os.environ.get("CESSPOOL_RPC_PASS", ""), _env("CESSPOOL_RPC_COOKIE"))
    return _node


def detect_chain(client):
    """'blake2b' or 'sha256' from the block at the fork height, None while the node has not reached it."""
    try:
        got = client.getblockhash(FORK)
    except rpc.RpcError:
        return None  # "Block height out of range": still syncing towards it
    return "blake2b" if got == BLAKE2B_961640 else "sha256"


def chain():
    """The chain this state directory holds: what the supervisor found, else what an earlier run wrote down."""
    c = _env("CESSPOOL_CHAIN") or coverage().get("chain") or ""
    return c if c in CHAINS else ""


def chain_name():
    return CHAINS.get(chain(), "")


def best():
    return node().getbestblockhash()


def tip():
    """The node's height, and its pruning state for the rest of this update."""
    global _info
    _run.clear()
    _info = node().getblockchaininfo()
    return _info["blocks"]


def tip_moved():
    """True once the node has a new best block since this update began: history gives way to it."""
    try:
        return best() != (_info or {}).get("bestblockhash")
    except rpc.Unreachable:
        return False


def prune_height():
    """The lowest block the node still has; 0 for a node that keeps them all."""
    i = _info or {}
    return int(i.get("pruneheight") or 0) if i.get("pruned") else 0


# ---------------------------------------------------------------- what to fetch

_out = None  # the web root this run renders into


def prepare(out=None):
    """Before an update: the state directories, the checker's chain and the pool list for this chain."""
    global _out
    _out = out
    for d in (HOME, BLOCKS, RAW, TXD):
        os.makedirs(d, exist_ok=True)
    c = chain()
    if c:
        # process.run_checker hands its environment to the engine, which reads the chain from it.
        os.environ["PLUMB_CHECK_CHAIN"] = c
    src = os.path.join(POOLS, f"{c}.json") if POOLS and c else ""
    cache = f"{HOME}/pools-v2.json"
    if src and os.path.exists(src):
        with open(src, "rb") as f:
            want = f.read()
        try:
            with open(cache, "rb") as f:
                same = f.read() == want
        except OSError:
            same = False
        if not same:
            with open(cache + ".tmp", "wb") as f:
                f.write(want)
            os.replace(cache + ".tmp", cache)
    if os.path.exists(cache):
        # reorg-watch downloads the list again when its copy is six hours old. A fresh date keeps the
        # copy that shipped with this release in use, and the install off the network.
        os.utime(cache)
    regrade_check()


def graded():
    """Heights with a record, sorted."""
    try:
        return sorted(int(n[:-5]) for n in os.listdir(BLOCKS) if n.endswith(".json"))
    except OSError:
        return []


def start(tip_h):
    """Where a first run begins: near the tip, and never below the fork or what the node still has."""
    return max(FORK, prune_height(), tip_h - RECENT + 1)


def disk_free():
    try:
        return shutil.disk_usage(HOME).free
    except OSError:
        return None


def missing(have, top, most):
    """Up to `most` heights from the fork to `top` that have no record, highest first."""
    out, want = [], top
    for h in reversed(have):
        if h > top:
            continue
        while want > h and len(out) < most:
            out.append(want)
            want -= 1
        if len(out) >= most:
            return out
        want = h - 1
    while want >= FORK and len(out) < most:
        out.append(want)
        want -= 1
    return out


def holes(have):
    """[[lo, hi], ...] for the heights between the first and last record that have none."""
    out = []
    for a, b in zip(have, have[1:]):
        if b > a + 1:
            out.append([a + 1, b - 1])
    return out


def heights(top, tip_h):
    """The blocks this update fetches. New blocks above `top` come first and alone, so a new block
    never waits behind history. With none of those, one batch of older blocks that are still missing,
    newest first: from the node while it has them, and below its prune height through backfill.py when
    that can rebuild them. What neither can supply stays a gap, and the pages say so."""
    from . import backfill
    free = disk_free()
    if free is not None and free < MIN_FREE:
        _run.update(blocked="disk", free=free)
        return []
    ph = prune_height()
    first = top + 1
    if tip_h - top > 2 * RECENT:
        # Far behind, as after a long stop: show the tip first and treat the rest as history.
        first = tip_h - RECENT + 1
    new = list(range(max(first, ph), tip_h + 1))
    if new:
        return new
    again = regrade_batch()
    if again:
        _run.update(source="regrade", fill=again, began=time.monotonic())
        return again
    if not room():
        _run.update(blocked="budget")
        return []
    todo, source = [], None
    for h in missing(graded(), top, FILL_MOST):
        src = "node" if h >= ph else "indexer" if backfill.usable() else None
        if src is None or (source and src != source):
            break  # one kind a batch, so the timing below means something
        source = src
        todo.append(h)
    if not todo:
        return []
    pace = (coverage().get("pace") or {}).get(source) or (20.0 if source == "indexer" else 4.0)
    todo = todo[:max(1, min(FILL_MOST, int(FILL_SECONDS / max(pace, 0.05))))]
    _run.update(source=source, fill=todo, began=time.monotonic())
    return todo


def fetch(h, blockhash, path):
    """Store block h with the coins it spends at `path`, gzipped, as REST's block/<hash>.json has it.
    A block the node has pruned is rebuilt through the Electrum server when that is set up; when it
    is not, nothing is written and the height stays a gap."""
    from . import backfill
    try:
        block = node().getblock(blockhash, 3)
        if not _has_prevouts(block):
            block = None  # the undo data is gone, though the block is not
    except rpc.RpcError as e:
        if not e.pruned:
            raise
        block = None
    if block is None:
        block = backfill.rebuild(h, blockhash, tip_moved) if backfill.usable() else None
        if block is None:
            return
    if block.get("height") != h or block.get("hash") != blockhash:
        raise RuntimeError(f"asked the node for block {h} ({blockhash}) and got {block.get('height')} ({block.get('hash')})")
    tmp = path + ".tmp"
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(json.dumps(block, separators=(",", ":")).encode())
    os.replace(tmp, path)


def _has_prevouts(block):
    for t in block["tx"][1:]:
        return all("prevout" in v for v in t["vin"])
    return True


def stored(new):
    """The heights of `new` that were fetched, in order."""
    return [h for h in new if os.path.exists(f"{RAW}/{h}.json.gz")]


def batches(new):
    """`new` in runs process_range can hold in memory: at most 100 blocks and BATCH_BYTES of stored JSON."""
    run, size = [], 0
    for h in new:
        try:
            n = os.path.getsize(f"{RAW}/{h}.json.gz")
        except OSError:
            n = 0
        if run and (len(run) >= 100 or size + n > BATCH_BYTES):
            yield run
            run, size = [], 0
        run.append(h)
        size += n
    if run:
        yield run


def build_due(out, new, top):
    """Whether this update renders the pages. A new block always does. Blocks that only fill in
    history wait until the pages are a few minutes old, or enough blocks have gathered, or nothing
    more is on its way: whichever build comes next renders every record that has no page yet. That
    last case includes an update that fetched nothing, when earlier ones left records without pages
    and the filling has since stopped; nothing else would render them before the next block."""
    if _run.get("expired"):
        return True  # blocks were dropped: the pages that listed them are out of date
    try:
        if os.path.getmtime(settings.path()) > os.path.getmtime(os.path.join(out, "tip.json")) and settings.enabled():
            return True  # the settings changed since the last build: /about/ says what they are
    except OSError:
        pass
    if any(h > top for h in new):
        return True
    have = graded()
    try:
        age = time.time() - os.path.getmtime(os.path.join(out, "tip.json"))
        pages = sum(1 for n in os.listdir(os.path.join(out, "block")) if n.isdigit())
    except OSError:
        return bool(have)  # never built
    if _run.get("source") == "regrade":
        waiting = sum(hi - lo + 1 for lo, hi in _regrade().get("left") or [])
        return age >= BUILD_EVERY or waiting <= len(new)
    unbuilt = len(have) - pages
    if not new and unbuilt <= 0:
        return False
    return age >= BUILD_EVERY or unbuilt >= BUILD_BLOCKS or not survey(have)["filling"]


# ---------------------------------------------------------------- what is covered

_run = {}  # what heights() planned for this update


_coverage = None


def coverage(fresh=False):
    """coverage.json: {"chain", "start", "top", "gaps": [[lo, hi], ...], "pruned", "pruneheight", ...}."""
    global _coverage
    if _coverage is None or fresh:
        try:
            with open(COVERAGE) as f:
                _coverage = json.load(f)
        except (OSError, ValueError):
            _coverage = {}
    return _coverage


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, path)


def unfilled(cov):
    """[[lo, hi], ...]: the blocks from the fork to the newest record that have none."""
    if not cov.get("blocks"):
        return []
    return ([[FORK, cov["start"] - 1]] if cov["start"] > FORK else []) + [list(g) for g in cov.get("gaps") or []]


def survey(have=None, grader=""):
    """Write coverage.json from the records on disk and what this update learned from the node."""
    global _coverage
    from . import backfill
    have = graded() if have is None else have
    cov = dict(coverage(fresh=True))
    cov.update(chain=chain(), floor=FORK, updated=int(time.time()))
    if have:
        cov.update(start=have[0], top=have[-1], blocks=len(have), gaps=holes(have))
    else:
        cov.update(start=None, top=None, blocks=0, gaps=[])
    if _info:
        cov.update(pruned=bool(_info.get("pruned")), pruneheight=prune_height(), tip=_info.get("blocks"))
    if grader:
        cov["grader"] = grader
    cov["backfill"] = backfill.report()
    # Why history is not filling in, when it is not: the disk, or blocks nobody can supply.
    left = unfilled(cov)
    ph = cov.get("pruneheight") or 0
    from_node = sum(hi - max(lo, ph) + 1 for lo, hi in left if hi >= ph)
    cov["remaining"] = sum(hi - lo + 1 for lo, hi in left)
    # How many of those are on their way, and from where: the node first, then the indexer for the rest.
    cov["source"] = "node" if from_node else "indexer" if left and backfill.usable() else None
    cov["coming"] = cov["remaining"] if backfill.usable() else from_node
    cov["blocked"] = _run.get("blocked") or (None if not left or cov["source"] else "pruned")
    cov["filling"] = bool(cov["coming"]) and not cov["blocked"]
    cov["regrade"] = sum(hi - lo + 1 for lo, hi in _regrade().get("left") or [])
    if "bytes" in _run:
        cov["bytes"] = _run["bytes"]
    cov["budget"] = MAX_BYTES
    _write(COVERAGE, cov)
    _coverage = cov
    return cov


def done(new, top):
    """After an update: how fast history is filling in, what is covered now, and the status file."""
    got = set(new)
    fill = [h for h in _run.get("fill", []) if h in got]
    if fill:
        took = (time.monotonic() - _run["began"]) / len(fill)
        pace = dict(coverage().get("pace") or {})
        src = _run["source"]
        pace[src] = round(took if src not in pace else 0.5 * pace[src] + 0.5 * took, 2)
        coverage()["pace"] = pace
        _write(COVERAGE, coverage())
    if MAX_BYTES and new:
        _run["bytes"] = sum(footprint(_out).values())  # with what this update added, for the pages and the next decision
    if _run.get("source") == "regrade":
        # The whole batch is struck, including a block whose stored copy turned out to be missing
        # and could not be fetched again: it keeps the grade it has rather than being tried forever.
        regrade_done(set(_run.get("fill", [])))
    status(survey())


def pending():
    """True while there is history this install can still fill in, or blocks to grade again, so the
    watch loop keeps going between blocks."""
    return bool(coverage().get("filling") or coverage().get("regrade"))


def status(cov=None, **extra):
    """status.json, for the platform's health check and the page shown before the first build. The
    supervisor (tools/serve.py) writes the same file while there is nothing to grade yet."""
    cov = coverage() if cov is None else cov
    doc = {"chain": cov.get("chain") or chain(), "tip": cov.get("tip"), "top": cov.get("top"), "start": cov.get("start"),
           "blocks": cov.get("blocks", 0), "gaps": len(cov.get("gaps") or []), "remaining": cov.get("remaining"), "coming": cov.get("coming"),
           "filling": bool(cov.get("filling")), "source": cov.get("source"), "blocked": cov.get("blocked"),
           "pruned": cov.get("pruned"), "pruneheight": cov.get("pruneheight"), "backfill": cov.get("backfill"), "regrade": cov.get("regrade", 0),
           "bytes": cov.get("bytes"), "settings": {"backfill": BACKFILL, "max_gb": MAX_BYTES // 2**30},
           "updated": int(time.time())}
    doc.update(extra)
    doc.setdefault("state", "grading" if doc["blocks"] else "starting")
    doc.setdefault("message", describe(doc))
    write_status(doc)
    return doc


def write_status(doc):
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        _write(STATUS, doc)
    except OSError:
        pass  # a read-only or missing data directory must not stop the grading


def describe(doc):
    """One sentence on where this install stands, for the health check."""
    name = CHAINS.get(doc.get("chain") or "", "")
    if not doc.get("blocks"):
        return f"Grading the first blocks of the {name} chain" if name else "Starting"
    text = f"Grading the {name} chain, blocks {doc['start']} to {doc['top']}"
    if doc.get("blocked") == "disk":
        return text + ". Paused: the disk is nearly full"
    if doc.get("blocked") == "budget":
        return text + ", as many as fit in the disk space it may use"
    if doc.get("regrade"):
        return text + f", and grading {doc['regrade']:,} of them again with this release's filters"
    if doc.get("filling") and doc.get("coming"):
        how = "through the indexer" if doc.get("source") == "indexer" else "from the node"
        return text + f", and filling in {doc['coming']:,} older blocks {how}"
    if doc.get("gaps"):
        return text + f", with {doc['gaps']} gap{'s' if doc['gaps'] != 1 else ''}"
    return text


# ---------------------------------------------------------------- grading again after an update

REGRADE = f"{HOME}/regrade.json"
_grader = None


def grader():
    """What gives a transaction its verdict and its name: the Plumb release the engine is built from,
    the engine's own source, and the code that reads its answers. A release that changes any of them
    grades differently, so the blocks on file are graded again from their stored copies, as
    cesspool.lol does by hand after such a change. (The engine's source rather than its binary: the
    same source built twice need not give the same bytes, and that would be no reason to regrade.)"""
    global _grader
    if _grader is None:
        import hashlib
        h = hashlib.sha256(os.environ.get("PLUMB_TAG", "").encode())
        here = os.path.dirname(os.path.abspath(__file__))
        for path in [os.path.join(here, n) for n in ("classify.py", "process.py", "txdetail.py")] + \
                    [os.path.join(os.path.dirname(here), "plumb-check", "plumb_check_tests.cpp")]:
            try:
                with open(path, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"absent")
            h.update(b"\0")
        _grader = h.hexdigest()[:16]
    return _grader


def _regrade():
    try:
        with open(REGRADE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def regrade_check():
    """At the start of a run: when the grader is not the one the records were written by, queue
    every block on file. An install's first run has nothing on file and only notes the grader."""
    state = _regrade()
    now = grader()
    if state.get("grader") == now:
        return
    have = graded() if state.get("grader") else []
    left = [[have[0], have[0]]] if have else []
    for h in have[1:]:
        if h == left[-1][1] + 1:
            left[-1][1] = h
        else:
            left.append([h, h])
    if left:
        print(f"this release grades differently from the one before: grading {len(have)} stored blocks again, newest first", flush=True)
    _write(REGRADE, {"grader": now, "left": left})


def regrade_batch():
    """The next blocks to grade again, newest first: a batch sized like a batch of history."""
    left = _regrade().get("left") or []
    if not left:
        return []
    pace = (coverage().get("pace") or {}).get("regrade") or 2.0
    most = max(1, min(FILL_MOST, int(FILL_SECONDS / max(pace, 0.05))))
    out, gone = [], set()
    for lo, hi in reversed(left):
        for h in range(hi, lo - 1, -1):
            if not os.path.exists(f"{BLOCKS}/{h}.json"):
                gone.add(h)  # dropped since, by a reorg or the ceiling: nothing to grade again
            elif len(out) < most:
                out.append(h)
        if len(out) >= most:
            break
    if gone:
        regrade_done(gone)
    return out


def regrade_done(heights):
    state = _regrade()
    left = []
    for lo, hi in state.get("left") or []:
        run = None
        for h in range(lo, hi + 1):
            if h in heights:
                run = None
            elif run is None:
                run = [h, h]
                left.append(run)
            else:
                run[1] = h
    state["left"] = left
    _write(REGRADE, state)


# ---------------------------------------------------------------- the ceiling

def footprint(out):
    """{height: bytes} for what each block takes on disk: the stored block, its two records, its
    transaction file on the site and its page. The shared indexes are small beside these."""
    sizes = {}

    def scan(directory, cut):
        try:
            with os.scandir(directory) as it:
                for e in it:
                    name = e.name
                    if name.endswith(cut) and name[:-len(cut)].isdigit():
                        h = int(name[:-len(cut)])
                        sizes[h] = sizes.get(h, 0) + e.stat().st_size
        except OSError:
            pass

    scan(RAW, ".json.gz")
    scan(BLOCKS, ".json")
    scan(TXD, ".json")
    if out:
        scan(os.path.join(out, "d", "b"), ".json.gz")
        for h in list(sizes):
            try:
                sizes[h] += os.path.getsize(os.path.join(out, "block", str(h), "index.html"))
            except OSError:
                pass
    return sizes


def room():
    """Whether another batch of history fits under the ceiling, with a margin, so that filling and
    dropping never chase each other."""
    if not MAX_BYTES:
        return True
    if "bytes" not in _run:
        _run["bytes"] = sum(footprint(_out).values())
    return _run["bytes"] < 0.95 * MAX_BYTES


def expire(out, top):
    """Drop the oldest blocks when this chain takes more disk than it may. A dropped block goes the
    way a reorged one does: out of the transaction index, then its files; the build that follows
    takes it out of every page and index."""
    if not MAX_BYTES:
        return []
    sizes = footprint(out)
    total = sum(sizes.values())
    drop = []
    for h in sorted(sizes):
        if total <= 0.98 * MAX_BYTES or not drop and total <= MAX_BYTES or h > top - KEEP_LEAST:
            break
        drop.append(h)
        total -= sizes[h]
    _run["bytes"] = total
    if not drop:
        return []
    from . import site
    print(f"over the {MAX_BYTES / 2**30:g} GB this install may use: dropping {len(drop)} oldest block{'s' if len(drop) != 1 else ''}, "
          f"{drop[0]} to {drop[-1]}", flush=True)
    site.drop_from_tx_index(out, set(drop))
    for h in drop:
        for p in (f"{BLOCKS}/{h}.json", f"{RAW}/{h}.json.gz", f"{TXD}/{h}.json",
                  os.path.join(out, "block", str(h), "index.html"), os.path.join(out, "og", "block", f"{h}.png")):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(os.path.join(out, "block", str(h)))
        except OSError:
            pass
    _run["expired"] = drop
    return drop


# ---------------------------------------------------------------- words on the pages

_built = {}  # the index's own reading of what is covered, set as each build starts


def begin_build(idx, grader=""):
    """Called by site.build with the index it is about to render: fixes what "since" means for this build."""
    have = sorted(idx)
    cov = survey(have, grader) if have else coverage()
    _built.clear()
    _built.update(start=have[0] if have else FORK, top=have[-1] if have else None, gaps=holes(have), blocks=len(have), cov=cov)


def first():
    """The lowest block graded here: what a page means by its earliest block."""
    if _built:
        return _built["start"]
    return coverage().get("start") or FORK


def since():
    """'since the fork' when this install has graded from the fork on, else 'since block N'."""
    s = first()
    return "since the fork" if s <= FORK else f"since block {s}"


def since_title():
    s = since()
    return s[0].upper() + s[1:]


def from_the_fork():
    """After "Every block": 'since the fork at height 961640', or 'from N on' for a later start."""
    s = first()
    return f"since the fork at height {FORK}" if s <= FORK else f"from {s} on"


def verdicts(names):
    """The policies a page compares. On the SHA256 chain Bitcoin Core follows the chain too, so its
    verdict, which the engine has always computed, is shown beside the others."""
    return ([("core", "Bitcoin Core 31")] if chain() == "sha256" else []) + list(names)


def pools_list():
    """Whose list names the pools on this chain."""
    return "mempool's pools-v2 list" if chain() == "sha256" else "Kilombino's pools-v2 list"


def pools_short():
    return "mempool's pools list" if chain() == "sha256" else "Kilombino's pools list"


def body_attrs():
    """What site.js needs to word its own messages: the first block graded here and the phrase for it."""
    return f' data-fork="{first()}" data-since="{since()}" data-brand="{BRAND}"'


def og_meta(esc, og, path):
    """The share-image tags, when share images are drawn; a private install has no use for them."""
    if not OG:
        return '<meta name="twitter:card" content="summary">'
    return (f'<meta property="og:image" content="{esc(SITE + og)}"><meta property="og:url" content="{esc(SITE + path)}">'
            f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{esc(SITE + og)}">')


def brand_html():
    name = chain_name()
    chip = f'<span class="chainchip" title="This install grades the {name} chain">{name}</span>' if name else ""
    return f'<span>cesspool<span class="tld"> pruned</span></span>{chip}'


def footer_html():
    name = chain_name()
    on = f"the {name} chain" if name else "your chain"
    return (f'<div>A self-hosted build of <a href="{UPSTREAM}">cesspool.lol</a>, grading {on} from your own node.<br>'
            f'<a href="/about/#coverage">What this install covers</a></div>')


def size_text(b):
    if b >= 2**30:
        return f"{b / 2**30:.1f} GB"
    return f"{b / 2**20:.1f} MB" if b >= 2**20 else f"{b / 2**10:.0f} kB"


def range_text(lo, hi):
    return f"block {lo}" if lo == hi else f"blocks {lo} to {hi}"  # heights are written plain everywhere on the site


def coverage_html(n):
    """The section of /about/ that says which blocks this install has graded, and why not the rest,
    with the form for the two settings that decide it where the platform has no form of its own."""
    return _coverage_text(n) + settings_html(n)


def _coverage_text(n):
    b = _built or {}
    if not b.get("blocks"):
        return ""
    cov = b.get("cov") or coverage()
    name = chain_name() or "this"
    start, top, gaps = b["start"], b["top"], b["gaps"]
    text = f'<p>This install has graded {n(b["blocks"])} blocks on the {name} chain, from {start} to {top}.'
    if gaps:
        shown = ", ".join(range_text(lo, hi) for lo, hi in gaps[:12])
        more = f", and {n(len(gaps) - 12)} more ranges" if len(gaps) > 12 else ""
        lost = sum(hi - lo + 1 for lo, hi in gaps)
        text += f' Between those it has no record of {n(lost)} block{"s" if lost != 1 else ""}: {shown}{more}.'
    text += "</p>"
    left = (start - FORK) + sum(hi - lo + 1 for lo, hi in gaps)
    coming = min(left, cov.get("coming") or 0) if cov.get("filling") else 0
    ph = cov.get("pruneheight") or 0
    bf = cov.get("backfill") or {}
    only = f"Counts on this site that say {since()} cover only the blocks graded here."
    if not left:
        return text + f"<p>That is every block since the fork at height {FORK}.</p>"
    if cov.get("blocked") == "disk":
        return text + f"<p>It has stopped storing blocks because the disk is nearly full. Free some space and it carries on. {only}</p>"
    if cov.get("blocked") == "budget" or (cov.get("budget") and (cov.get("bytes") or 0) >= 0.95 * cov["budget"]):
        gb = cov["budget"] / 2**30
        return text + (f"<p>That is as many blocks as fit in the {gb:g} GB this install may use. A block with its records and pages takes "
                       f"about {size_text((cov.get('bytes') or 0) / max(1, b['blocks']))} on this chain, so as each new block arrives the oldest is dropped. "
                       f"Allow it more room and it fills in older blocks again. {only}</p>")
    if coming:
        how = ("It is rebuilding them through your Electrum server, a block at a time, which is slow"
               if cov.get("source") == "indexer" else "It is reading them from your node")
        text += (f'<p>{n(coming)} older block{"s are" if coming != 1 else " is"} still to come. {how}, newest first.'
                 + (f" {only}" if coming == left else "") + "</p>")
    if left > coming:
        why = (f"Your node is pruned: it no longer has the blocks below {ph}, nor its record of the coins they spent, "
               "which the policy checks need. " if ph else "Your node could not supply them. ")
        if bf.get("state") == "failed":
            why += (f"Rebuilding them through your Electrum server stopped: {bf.get('reason') or 'it could not supply a transaction'}. "
                    "It will be tried again.")
        elif bf.get("state") == "off":
            why += ("Rebuilding them through your Electrum server is turned off" +
                    (": you can turn it on below." if settings.enabled() else "."))
        elif bf.get("state") == "none":
            why += ("With an Electrum server that works beside a pruned node, such as Electrs Pruned or Fulcrum Pruned, "
                    "this install can rebuild them. None is set up.")
        lost = left - coming
        text += (f'<p>{n(lost)} block{"s" if lost != 1 else ""} back to the fork at height {FORK} '
                 f'{"are" if lost != 1 else "is"} out of reach. {why} {only}</p>')
    return text


def settings_html(n):
    """The settings form on /about/#settings, where the platform has no form of its own (Umbrel). It is
    drawn with the values in force at this build; static/settings.js then shows the ones in force now,
    from /status.json, and sends a change to /settings (tools/settings_server.py)."""
    if not settings.enabled():
        return ""
    cov = (_built or {}).get("cov") or coverage()
    used = cov.get("bytes")
    per = (used or 0) / max(1, (_built or {}).get("blocks") or 1)
    pruned, indexer = cov.get("pruned"), configured_indexer()
    if not pruned:
        relevant = "Your node keeps every block, so there is nothing to rebuild and this setting changes nothing."
    elif not indexer:
        relevant = "No Electrum server is set up, so this setting changes nothing."
    else:
        relevant = (f"Your node is pruned and keeps blocks from {cov.get('pruneheight')} on, so this is how history below that "
                    "is filled in.")
    gb = MAX_BYTES // 2**30
    options = "".join(f'<option value="{v}"{" selected" if v == gb else ""}>{"No limit" if not v else f"{v} GB"}</option>'
                      for v in settings.MAX_GB_CHOICES)
    usage = f"Now in use: {size_text(used)}, about {size_text(per)} a block." if used else ""
    return f'''<h3 id="settings">Settings</h3>
<form class="settingsform panel" data-bytes="{used or 0}">
<fieldset><legend>Rebuild blocks your node has discarded</legend>
<label><input type="radio" name="backfill" value="1"{" checked" if BACKFILL else ""}> On</label>
<label><input type="radio" name="backfill" value="0"{"" if BACKFILL else " checked"}> Off</label>
<p class="small muted">{relevant} Each old block is put back together from its bytes, which your Electrum server's proxy fetches from
the Bitcoin network, and from every earlier transaction it spends, which the Electrum server looks up one at a time, often by
downloading yet another old block. On the BLAKE2b chain a block takes seconds to minutes. On the SHA256 chain, where a block spends
from several thousand earlier transactions, one block took from half an hour to an hour and a half when measured, which is slower
than new blocks arrive, so it fills in a short gap but not a long history. It runs only between new blocks, one block at a time,
and uses your connection while it runs.</p></fieldset>
<fieldset><legend>Disk space this chain may use</legend>
<select name="max_gb" aria-label="Disk space this chain may use">{options}</select>
<p class="small muted">Graded blocks, their records and their pages, for this chain. History fills in until the limit is reached;
after that the oldest block is deleted as each new one arrives. A block takes about 0.6 MB on the BLAKE2b chain and about 8 MB on
the SHA256 chain. Lowering the limit deletes the oldest blocks at once, and on a pruned node they cannot be graded again
unless they are rebuilt as above. {usage}</p></fieldset>
<p><button class="btn" type="submit">Save</button> <span class="settingsout small" aria-live="polite"></span></p>
<noscript><p class="small muted">Changing these needs JavaScript.</p></noscript>
</form>'''


def configured_indexer():
    return bool(ELECTRUM[0])


def not_found_html():
    """A line for the 404 page: the commonest reason a block has no page here."""
    b = _built or {}
    if not b.get("blocks"):
        return ""
    return f' This install has graded blocks {b["start"]} to {b["top"]}; <a href="/about/#coverage">what it covers</a>.'


# ---------------------------------------------------------------- pieces of pages

STATIC = ("local.css", "lightning.js", "settings.js")  # this fork's own static files, beside upstream's
STILL_PAGES = ("/lightning/node/", "/lightning/channel/")  # pages site.js fills in, which do not reload on a new block
MEASURED_ELSEWHERE = " That count is cesspool.lol&#39;s, taken on the BLAKE2b chain; this install did not make it."


def asset_version(src, upstream):
    """The ?v= on every static file: upstream's number, then a digest of the files as they are now."""
    import hashlib
    h = hashlib.sha256()
    for name in sorted(os.listdir(os.path.join(src, "static"))):
        path = os.path.join(src, "static", name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
    return f"{upstream}-{h.hexdigest()[:8]}"


def windows(ws):
    """The Hall of Shame's windows, the unbounded one named for where this install starts."""
    return [(k, since_title() if seconds is None else label, seconds, m) for k, label, seconds, m in ws]


def from_first():
    s = first()
    return "From the fork" if s <= FORK else f"From block {s}"


def nav(items):
    """The header's links, with Lightning before About when this install reads a Lightning node."""
    from . import lightning
    if not lightning.shown():
        return items
    at = next((i for i, x in enumerate(items) if x[0] == "about"), len(items))
    return items[:at] + [("lightning", "/lightning/", "Lightning")] + items[at:]


def scripts(path, v):
    """Script tags after site.js. The Lightning code goes on every page, whether or not this install
    has Lightning pages now: a block page is drawn once and kept, and Lightning can be set up or
    taken away afterwards. The script asks /d/features.json which it is."""
    tags = f'<script src="/static/lightning.js?v={v}" defer></script>'
    if path == "/about/" and settings.enabled():
        tags += f'<script src="/static/settings.js?v={v}" defer></script>'
    return tags


def end_build(out, idx, tip, page):
    """Pages of this fork's own, written at the end of every build."""
    from . import lightning
    lightning.publish(out, idx, tip, page)


def measured_note(lo, hi):
    """Above pages that quote counts baked into the source: whose they are."""
    return (f'<p class="small muted" style="margin-top:8px">The block ranges and counts quoted on this page, and in the files it offers, '
            f'were measured by <a href="{UPSTREAM}">cesspool.lol</a> on the BLAKE2b chain, blocks {lo} to {hi}. '
            f'They are not counts of the blocks this install has graded.</p>')


def core_sentence(fork, knots_size):
    """What /about/ says of Bitcoin Core's verdict, which depends on whether Core follows this chain."""
    if chain() != "sha256":
        return f"Bitcoin Core is not compared: it does not accept block {fork} or any block after it, so no Core node has these blocks."
    return ("Bitcoin Core 31 is compared too, as Knots&#39; <code>-corepolicy</code> with the 2,500 legacy-sigop limit and the multiple OP_RETURN "
            f"outputs Core has had since 30, and with Core&#39;s 100,000-byte limit on an OP_RETURN in place of the {knots_size} this code stops at. "
            "Knots 29.5 and Plumb are built for the BLAKE2b chain, and each verdict names the policy code that gave it, whichever chain mined the "
            "transaction. Both also hold a transaction&#39;s scripts to that chain&#39;s RDTS limits; one that breaks them is listed here by its "
            "data rules alone.")


def about_html(plumb, knots):
    """The rest of /about/'s section on this install: where the pool names come from."""
    whose = "mempool&#39;s" if chain() == "sha256" else "Kilombino&#39;s"
    return (f"<p>Pool names come from the copy of {whose} pools list that shipped with this release. This install does not download a newer one, "
            "so a pool that appeared since then shows by its coinbase text or as unknown until the next release.</p>")


def privacy_html():
    """/privacy/ for an install on the reader's own server. cesspool.lol's page is about its server log
    and Cloudflare, and neither is here."""
    return f'''<div class="hero prose"><div class="kicker">Privacy</div><h1>What this install keeps</h1>
<p class="lede">This is {BRAND}, running on your own server. This page says what it records and what it sends anywhere.</p></div>
<div class="prose">
<h2>What it sends</h2>
<p>Nothing. It asks your Bitcoin node for blocks, and, when you have set them up, your Electrum server for older transactions and your Lightning node for the public channel graph. All three are on your server. It makes no request to any other machine: the mining pool list and the fonts shipped with this release, and nothing is downloaded while it runs.</p>
<h2>What your browser loads</h2>
<ul><li>Every script, font and image comes from this install. Nothing loads from other sites.</li>
<li>It sets no cookies and runs no analytics.</li>
<li>Some pages check for a new block every 10 seconds while they are on screen.</li>
<li>What you paste into <a href="/check/">Check Node</a> never leaves your browser.</li>
<li>Your browser keeps two notes for this site: the tab you last picked, and the last block a page reloaded for.</li>
<li>Links to other sites, such as cesspool.lol, reorg.watch and GitHub, leave this one, and those sites have their own policies.</li></ul>
<h2>What it logs</h2>
<p>The web server here writes no access log. The platform it runs on, and anything you put in front of it to reach it from outside, may keep logs of their own.</p>
<p>On <a href="/found/">Your Blocks</a> and <a href="/address/">Address Lookup</a>, what you type stays after the # in the page&#39;s address, which your browser never sends to a server.</p>
<h2>If you publish it</h2>
<p>All of the above describes this software. If this install is reachable by other people, whoever runs it decides what else is recorded on the way.</p>
<h2>Questions</h2>
<p>Open an issue on <a href="{REPO}/issues">this build&#39;s GitHub repository</a>. The site it is built from is <a href="{UPSTREAM}">cesspool.lol</a>, whose source is at <a href="{UPSTREAM_REPO}">{UPSTREAM_REPO.split("//")[1]}</a>.</p>
</div>'''
