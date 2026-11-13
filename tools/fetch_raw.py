#!/usr/bin/env python3
"""Cache blocks (with prevouts) as gzipped JSON, as update.py stores them: fetch_raw.py FROM TO.

Reads the node over JSON-RPC with the settings update.py uses (CESSPOOL_RPC_URL and the rest; see
cesspool/local.py). A block the node has pruned is rebuilt through the Electrum server when one is
set, and skipped otherwise."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cesspool import local  # noqa: E402


def main():
    lo, hi = int(sys.argv[1]), int(sys.argv[2])
    local.prepare()
    local.tip()
    for h in range(lo, hi + 1):
        path = f"{local.RAW}/{h}.json.gz"
        if os.path.exists(path):
            continue
        local.fetch(h, local.node().getblockhash(h), path)
        if h % 500 == 0:
            print(h, flush=True)


if __name__ == "__main__":
    main()
