#!/usr/bin/env python3
"""The updater process of a Cesspool Pruned install: wait for the node, ask it which chain it is on,
and keep tools/update.py running against that chain's own state. docker/updater.sh starts it.

Each chain has its own state and its own rendered site, so pointing the install at a node on the
other chain never mixes two chains' blocks, and pointing it back picks up where it left off:

    <data>/<chain>/        raw/, blocks/, txd/, the indexes, coverage.json
    <data>/www/<chain>/    the rendered site
    <data>/www/current     a link to the chain being graded now; nginx serves it
    <data>/status.json     where things stand, for the health check and the starting page

serve.py [--data /data]
"""
import argparse, json, os, signal, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cesspool import local, rpc  # noqa: E402

UPDATE = os.path.join(ROOT, "tools", "update.py")
WATCH = "57"  # seconds one update.py keeps watching the tip before this starts a fresh one
child = None
last_said = None


def say(text):
    """A line in the log, once: a state that lasts an hour is not an hour of the same line."""
    global last_said
    if text != last_said:
        print(text, flush=True)
        last_said = text


def status(data, state, message, **more):
    """Merge into status.json, keeping what the last update wrote about coverage."""
    path = os.path.join(data, "status.json")
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc.update(state=state, message=message, updated=int(time.time()), **more)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, path)
    say(message)


def point(data, chain):
    """Make <data>/www/current name this chain's site, in one step."""
    www = os.path.join(data, "www")
    os.makedirs(www, exist_ok=True)
    link = os.path.join(www, "current")
    if os.path.islink(link) and os.readlink(link) == chain:
        return
    tmp = link + ".new"
    try:
        os.remove(tmp)
    except OSError:
        pass
    os.symlink(chain, tmp)
    os.replace(tmp, link)


def run(args, env):
    """Run update.py, passing its errors through to the log. Returns (exit code, the last error line)."""
    global child
    child = subprocess.Popen([sys.executable, UPDATE] + args, env=env, stderr=subprocess.PIPE, text=True)
    last = ""
    for line in child.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()
        if line.strip():
            last = line.strip()
    code = child.wait()
    child = None
    return code, last


def stop(*_):
    if child is not None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
    sys.exit(0)


def plain(error):
    """A traceback's last line without the exception's module path."""
    head, _, rest = error.partition(": ")
    return rest if rest and " " not in head else error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("CESSPOOL_DATA", "/data"))
    a = ap.parse_args()
    data = a.data
    os.makedirs(data, exist_ok=True)
    os.umask(0o022)  # the web server reads what this writes, as another user on some platforms
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    wait, was = 0, None
    while True:
        # The node first: reachable, done syncing, and past the block that tells the chains apart.
        try:
            client = local.node()
            info = client.getblockchaininfo()
            chain = None if info.get("initialblockdownload") else local.detect_chain(client)
        except (rpc.Unreachable, rpc.RpcError) as e:
            status(data, "waiting", f"Waiting for the Bitcoin node: {e}", alive=int(time.time()))
            time.sleep(10)
            continue
        if info.get("initialblockdownload"):
            done = 100 * float(info.get("verificationprogress") or 0)
            status(data, "waiting", f"Waiting for the Bitcoin node to finish syncing: block {info.get('blocks')} of {info.get('headers')} ({done:.1f}%)",
                   alive=int(time.time()))
            time.sleep(30)
            continue
        if chain is None:
            status(data, "waiting", f"Waiting for the Bitcoin node to reach block {local.FORK}, where grading starts: it is at {info.get('blocks')}",
                   alive=int(time.time()))
            time.sleep(60)
            continue
        if chain != was:
            say(f"The node is on the {local.CHAINS[chain]} chain")
            was = chain
        home, out = os.path.join(data, chain), os.path.join(data, "www", chain)
        os.makedirs(home, exist_ok=True)
        point(data, chain)
        env = dict(os.environ, CESSPOOL_HOME=home, CESSPOOL_CHAIN=chain, PLUMB_CHECK_CHAIN=chain, CESSPOOL_DATA=data)
        carry_on(data, chain)

        # "Rebuild Site" leaves this file: render every page again from the stored records.
        flag = os.path.join(data, "rebuild")
        if os.path.exists(flag):
            say("Rendering every page again")
            code, error = run(["--out", out, "--all"], env)
            if code == 0:
                os.remove(flag)
        else:
            code, error = run(["--out", out, "--watch", WATCH], env)
        if code == 0:
            wait = 0
            lightning(env, out)
            time.sleep(1)
            continue
        # A failure: say so where the health check reads it, and try again, less often each time.
        wait = min(120, max(5, wait * 2))
        status(data, "error", f"The last update failed: {plain(error) or 'exit code %d' % code}. Trying again in {wait} seconds.", alive=int(time.time()))
        time.sleep(wait)


def carry_on(data, chain):
    """Before an update: note that this process is alive. A status left by a wait for the node is
    over; one left by a failed update stands until an update succeeds and writes its own."""
    path = os.path.join(data, "status.json")
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if doc.get("state") in ("grading", "starting", "error") and doc.get("chain") == chain:
        doc.update(alive=int(time.time()))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
    else:
        status(data, "starting", f"Starting to grade the {local.CHAINS[chain]} chain", chain=chain, alive=int(time.time()),
               blocks=0 if doc.get("chain") != chain else doc.get("blocks", 0))


def lightning(env, out):
    """Between updates: read the Lightning graph again when one is set up and it is due."""
    if not os.environ.get("CESSPOOL_LN_BACKEND", "").strip():
        return
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "lightning_sync.py"), "--out", out], env=env)


if __name__ == "__main__":
    main()
