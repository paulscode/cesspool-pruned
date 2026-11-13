#!/usr/bin/env python3
"""Read the Lightning graph when it is due, look for channel closes in newly graded blocks, and
write the Lightning pages. tools/serve.py runs it between updates, when a Lightning node is set up.

lightning_sync.py --out /data/www/<chain>

It takes the same lock as update.py, so neither ever reads what the other is halfway through
writing. When an update holds the lock this does nothing, and the next turn tries again.
"""
import argparse, fcntl, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cesspool import lightning, local  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not lightning.backend():
        return
    os.makedirs(local.HOME, exist_ok=True)
    with open(f"{local.HOME}/lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        lightning.main(a.out)


if __name__ == "__main__":
    main()
