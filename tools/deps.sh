#!/bin/sh
# Fetch what the pipeline needs that is not in this repository, for running it or its tests outside
# the image: reorg-watch, at the commit docker/Dockerfile pins, checked against the digest it pins.
#
#   eval "$(tools/deps.sh)"        fetches into .deps/ and prints the environment to use it
#
# The engine (test_bitcoin from Plumb) is not fetched here: build it with
#   docker buildx build -f docker/Dockerfile --target checker-bin --output type=local,dest=.deps .
# and set PLUMB_CHECK_BIN=.deps/test_bitcoin-plumb PLUMB_FILTERS=.deps/filters.json.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
pin() { sed -n "s/^ARG $1=//p" "$root/docker/Dockerfile"; }
commit=$(pin REORG_WATCH_COMMIT)
want=$(pin REORG_WATCH_SHA256)
dir="$root/.deps"
file="$dir/reorg-watch-$commit.py"
mkdir -p "$dir"
if [ ! -f "$file" ]; then
    curl -sSfL -o "$file.tmp" "https://raw.githubusercontent.com/jasonsopko/reorg-watch/$commit/reorg-watch.py"
    got=$(sha256sum "$file.tmp" | cut -d' ' -f1)
    if [ "$got" != "$want" ]; then
        echo "reorg-watch.py at $commit has digest $got, not the pinned $want" >&2
        rm -f "$file.tmp"
        exit 1
    fi
    mv "$file.tmp" "$file"
fi
echo "export REORG_WATCH=$root/docker/reorg_watch_offline.py"
echo "export REORG_WATCH_REAL=$file"
echo "export CESSPOOL_POOLS=$root/docker/pools"
