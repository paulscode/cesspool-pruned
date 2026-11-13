"""Shared by the tests: stored blocks, and a way to load cesspool against a fresh state directory.

The cesspool modules read their settings when they are imported, so a test that needs different
settings gets its own import of them (`cesspool(env)` below), not a patched global.

    python3 -m unittest discover -s tests -v

The tests that grade with the real engine run only when PLUMB_CHECK_BIN names it (inside the image it
does); the rest use the engine's stored answers for the fixture blocks.
"""
import contextlib, gzip, importlib, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

BLAKE2B_961640 = "0000000000000050c1e5f69672f459293be14f46e5a494e7a8c8541396f18eeb"
CHECKER = os.environ.get("PLUMB_CHECK_BIN", "")
HAVE_CHECKER = bool(CHECKER) and os.access(CHECKER, os.X_OK)
# reorg-watch is not in this repository (the image fetches it at a pinned commit). Tests that name
# pools need a copy: REORG_WATCH_REAL, as in the image.
HAVE_REORG_WATCH = os.path.exists(os.environ.get("REORG_WATCH_REAL", ""))


def block(name):
    with gzip.open(os.path.join(FIXTURES, name + ".json.gz")) as f:
        return json.load(f)


def rows(name):
    """The engine's answers for a fixture block, keyed by txid, as process.run_checker returns them."""
    with gzip.open(os.path.join(FIXTURES, name + ".rows.json.gz")) as f:
        return {r["txid"]: r for r in json.load(f)}


@contextlib.contextmanager
def cesspool(env=None, chain="blake2b"):
    """A temporary state directory and web root, with the cesspool package imported against them.
    Yields a namespace: home, out, data, and module(name) to import one of cesspool's modules."""
    data = tempfile.mkdtemp(prefix="cesspool-test-")
    home, out = os.path.join(data, chain), os.path.join(data, "www", chain)
    os.makedirs(home)
    keep = dict(os.environ)
    for k in [k for k in os.environ if k.startswith("CESSPOOL_")]:
        del os.environ[k]
    os.environ.update({"CESSPOOL_HOME": home, "CESSPOOL_CHAIN": chain, "CESSPOOL_DATA": data, "CESSPOOL_POOLS": os.path.join(ROOT, "docker", "pools"),
                       "CESSPOOL_FONTS": os.path.join(data, "fonts"), "REORG_WATCH": os.path.join(ROOT, "docker", "reorg_watch_offline.py"),
                       "POOL_SURVEY": os.path.join(data, "no-survey.json"), "SV1_PORT_IDENTITIES": os.path.join(data, "no-identities.json")})
    os.environ.update(env or {})
    os.makedirs(os.environ["CESSPOOL_FONTS"], exist_ok=True)
    for name in ("IBMPlexSans.ttf", "IBMPlexMono-Regular.ttf", "IBMPlexMono-Medium.ttf"):
        open(os.path.join(os.environ["CESSPOOL_FONTS"], name), "wb").close()  # copy_static only copies them
    _forget()

    class Loaded:
        pass

    ns = Loaded()
    ns.home, ns.out, ns.data = home, out, data
    ns.module = lambda name: importlib.import_module("cesspool." + name)
    try:
        yield ns
    finally:
        _forget()
        os.environ.clear()
        os.environ.update(keep)
        shutil.rmtree(data, ignore_errors=True)


def _forget():
    for m in [m for m in sys.modules if m == "cesspool" or m.startswith("cesspool.")]:
        del sys.modules[m]


def grade(ns, names):
    """Write the block and transaction records for fixture blocks, from the engine's stored answers."""
    process = ns.module("process")
    os.makedirs(process.BLOCKS, exist_ok=True)
    os.makedirs(process.TXD, exist_ok=True)
    os.makedirs(process.RAW, exist_ok=True)
    out = []
    for name in names:
        b = block(name)
        txd, rec = process.process_block(b, rows(name))
        process.write_json(f"{process.TXD}/{rec['h']}.json", txd)
        process.write_json(f"{process.BLOCKS}/{rec['h']}.json", rec)
        shutil.copy(os.path.join(FIXTURES, name + ".json.gz"), f"{process.RAW}/{rec['h']}.json.gz")
        out.append(rec)
    return out
