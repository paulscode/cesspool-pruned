# Cesspool Pruned

A build of [cesspool.lol](https://cesspool.lol) to run on your own server, next to your own
Bitcoin node. It grades every block the node gives it for spam, names the pool that mined it and
the filter that would have kept the spam out, and serves the result as a static site.

It is a fork of [jasonsopko/cesspool](https://github.com/jasonsopko/cesspool), kept close to it.
What this fork changes:

- **It works with a pruned node.** It reads the node over JSON-RPC, not REST, so it can sit behind
  the btc-rpc-proxy that fronts a pruned node. It starts at the newest blocks and fills in history
  backwards, as far as the node still has it. Below that, it can rebuild blocks through an
  Electrum server that works beside a pruned node (Electrs Pruned, Fulcrum Pruned).
- **It works on either chain**, the BLAKE2b chain cesspool.lol grades or the SHA256 chain. It asks
  the node which one it is on (the hash of block 961640 tells them apart) and keeps each chain's
  blocks and pages apart. On the SHA256 chain it also shows what Bitcoin Core's defaults would do
  with each transaction, which the engine has always computed.
- **It reads a Lightning node**, when you have one: the public channel graph, joined to the graded
  blocks. See [Lightning](#lightning).
- **It makes no outbound requests.** The pool list and the fonts ship with the image.
- The pages lose the home page's opening section, the Get Plumb card and the Discord link, and say
  "since block N" where an install does not reach back to the fork.

Packaged for StartOS as `cesspool-pruned-startos` and for Umbrel in the `paulscode` community
store. Upstream's own README follows [below](#cesspoollol).

## Run it

One image, two processes sharing `/data`:

    docker buildx build -f docker/Dockerfile -t cesspool-pruned --load .

    docker run -d --name cesspool-web -p 8080:8080 -v cesspool:/data cesspool-pruned
    docker run -d --name cesspool-updater -v cesspool:/data \
        -e CESSPOOL_RPC_URL=http://node:8332 -e CESSPOOL_RPC_USER=user -e CESSPOOL_RPC_PASS=pass \
        cesspool-pruned /app/docker/updater.sh

The web process serves a page that says what the updater is waiting for until the first blocks
are graded, then the site. `http://<host>:8080/status.json` says where things stand.

The build compiles the verdict engine from [Plumb](https://github.com/plumb-node/plumb) at the tag
`cesspool/site.py` names (about three minutes on a fast machine) and refuses to build when the two
disagree. It cross-compiles, so `--platform linux/amd64,linux/arm64` needs no ARM machine.

## Settings

Environment variables of the updater. Only the first is required.

| Variable | Meaning | Default |
|---|---|---|
| `CESSPOOL_RPC_URL` | `http://host:port` of the node's RPC, or of the btc-rpc-proxy in front of a pruned node | |
| `CESSPOOL_RPC_USER`, `CESSPOOL_RPC_PASS` | RPC credentials | empty: use the cookie |
| `CESSPOOL_RPC_COOKIE` | the node's `.cookie` file, or its data directory | |
| `CESSPOOL_ELECTRUM_HOST`, `CESSPOOL_ELECTRUM_PORT` | an Electrum server, plaintext TCP, for rebuilding blocks the node has pruned | none, 50001 |
| `CESSPOOL_BACKFILL` | `1` to rebuild pruned blocks when an Electrum server is set | `1` |
| `CESSPOOL_MAX_GB` | disk this chain's blocks and pages may take; `0` for no limit | `50` |
| `CESSPOOL_MIN_FREE_MB` | stop storing blocks when the disk has less than this free | `2048` |
| `CESSPOOL_SITE` | the site's own address, for share links and the feed | empty: relative links |
| `CESSPOOL_OG` | `1` to draw share images | `0` |
| `CESSPOOL_SETTINGS_PAGE` | `1` to put backfill and the disk limit in a form on `/about/`, for platforms with no settings form (Umbrel). The web process then needs `/data` writable, for `settings.json` alone, and the choice saved there wins over the two variables above | off |
| `CESSPOOL_LN_BACKEND` | `lnd`, `cln`, `auto`, or empty | empty: no Lightning pages |
| `CESSPOOL_LND_REST_URL`, `CESSPOOL_LND_TLS_CERT`, `CESSPOOL_LND_MACAROON` | LND, or Lightning Fork, which is LND | |
| `CESSPOOL_CLN_SOCKET` | Core Lightning's `lightning-rpc` | |
| `CESSPOOL_LN_REFRESH` | seconds between reads of the Lightning graph | `3600` |

`cesspool/local.py` and `cesspool/lightning.py` document each one, and the `auto` mode.

## What it stores, and how much

Everything is under `/data`, one set of directories for each chain:

    /data/<chain>/raw/      each block as the node gave it, with the coins it spends (gzipped JSON)
    /data/<chain>/blocks/   one record a block; txd/ one record a transaction
    /data/<chain>/ln/       the Lightning graph, when one is read
    /data/www/<chain>/      the rendered site; /data/www/current links to the chain being graded
    /data/status.json       where things stand

Measured on real blocks in November 2026: a block with its records and pages takes about
**0.6 MB on the BLAKE2b chain** and about **8 MB on the SHA256 chain**, where blocks are full: over
a gigabyte a day. So there is a ceiling, `CESSPOOL_MAX_GB`. History fills in until it is reached,
and from then on the oldest blocks are dropped as new ones arrive, the way a reorged block is
dropped. At the default 50 GB the BLAKE2b chain keeps everything for well over a year, and the
SHA256 chain keeps about six weeks.

Stored blocks are kept whole on purpose: a pruned node cannot supply a block again, and a later
release with newer filters grades from them.

## Pruned nodes

`getblock <hash> 3` is what cesspool grades from, and a pruned node cannot answer it below its
prune height: both the block and its record of the coins the block spent are gone. So:

- a first run starts 12 blocks below the tip, so the site is up in minutes, then fills in older
  blocks newest first, down to the prune height;
- below that, with an Electrum server set, `cesspool/backfill.py` rebuilds each block from its
  bytes (the proxy fetches them from peers), the node's own decoding, and the transactions its
  inputs spend from, which the Electrum server has. Every transaction is checked against its id.
  This is slow, minutes a block on the SHA256 chain, because each of those transactions sits in
  some older block that the Electrum server in turn fetches from peers;
- what neither can supply is a gap, and `/about/` says which blocks and why.

## Lightning

With a Lightning node set, the site gains `/lightning/`: nodes, channels and capacity as that node
sees the public graph, rankings, a page for each node and channel, and, from cesspool's own
records, how each channel closed (cooperative, forced, forced with a penalty) and what each node
policy does with Lightning's own transactions. Transaction and block pages say when they open or
close a channel. Only public information is read: the gossip graph and the node's own identity.
No balances, payments or private channels, and nothing is written to the node.

A close is known only when its block was graded here. Maps and lists by country or network
provider, which mempool.space has, are not built: they need a location database this build does
not carry.

## How the fork is laid out

Fork behaviour lives in new files, and upstream's files reach it through one-line hooks, so that
merging upstream stays cheap:

    cesspool/local.py       settings, chain detection, what to fetch, coverage, words on the pages
    cesspool/rpc.py         the JSON-RPC client
    cesspool/backfill.py    rebuilding pruned blocks; cesspool/electrum.py is its Electrum client
    cesspool/lightning.py   the Lightning graph and closes; cesspool/pages_ln.py draws its pages
    static/local.css, static/lightning.js
    tools/serve.py          the updater process: waits for the node, finds the chain, runs update.py
    tools/since.py          rewrites "since the fork" into calls; run it again after merging upstream
    docker/                 the image, nginx, the pool lists
    tests/                  python3 -m unittest discover -s tests

To merge upstream: `git merge upstream/master`, take upstream's side of any conflict in
`cesspool/site.py` and `static/site.js` that is only wording, run `tools/since.py`, then the tests.
When upstream moves `PLUMB_TAG`, set `PLUMB_TAG` and `PLUMB_COMMIT` in `docker/Dockerfile` to match.

Outside the image, `eval "$(tools/deps.sh)"` fetches the one runtime file that is not in this
repository (reorg-watch, at a pinned commit) and sets the environment for it.

## Credits

cesspool.lol and its grading are Jason Sopko's work, under the MIT licence in `LICENSE`; this fork
only makes it run elsewhere, under the same licence. The image also contains [reorg-watch](https://github.com/jasonsopko/reorg-watch) (MIT), the verdict
engine built from [Plumb](https://github.com/plumb-node/plumb) (MIT), IBM Plex (SIL Open Font
License 1.1), and two pool lists: [mempool/mining-pools](https://github.com/mempool/mining-pools)
(MIT) for the SHA256 chain and [Kilombino/mempool-bip110](https://github.com/Kilombino/mempool-bip110)
for the BLAKE2b chain.

---

# cesspool.lol

Every Bitcoin block since the fork at 961640, every transaction in it, graded
for spam, with the pool that mined it and the filter that would have kept it
out.

## How it works

- `plumb-check/plumb_check_tests.cpp` is built into `test_bitcoin` from a Plumb
  tree (`plumb-check/build.sh`). It runs the shipped policy code on each
  transaction three ways, each with that software's defaults: Bitcoin Core 31 (`-corepolicy` plus Core 30's legacy-sigop limit and multiple OP_RETURN outputs), stock Knots 29.5
  (Plumb's two own filters off, `-permitlnrevokeleaf=0` and `-datacarriersize` at Knots' 83; since 29.4.3 Knots carries the dead-branch, bare-envelope, unproven-key and BPUB rules itself) and Plumb (42 since plumb7), and reports every reason each one
  trips and the data bytes it counts. Plumb's count includes the recount knots#470 adds after the policy
  script check, which credits a key only when a signature verified against it; the engine runs the script
  check the way the node does to get those keys, and stock Knots 29.5 has no such recount. For Plumb it
  also reports where the bytes sit
  and how many each of five data-counting rules adds (the count with
  that rule alone off) and how many the recount adds (the count with it
  left out), and, when Plumb's 42-byte limit changes its reasons, the
  reasons it gives at 83 (`p83`). Nothing reimplements a filter.
  The site shows the Knots and Plumb verdicts only: Bitcoin Core does not
  accept block 961640 or any block after it, so no Core node has these blocks.
- `cesspool/classify.py` names the protocol and finds the kinds Plumb does
  not count yet (fake multisig keys), so a miss shows as a miss.
- `cesspool/miner.py` names the pool with reorg-watch's own rules.
- `cesspool/site.py` renders static pages and share images.
- `cesspool/found.py` writes the indexes behind Your Blocks (`/found/`): pool
  names, coinbase tags and payout addresses to block heights, as static JSON
  under `/d/` that `site.js` searches in the browser.
- `cesspool/address.py` writes the index behind Address Lookup (`/address/`):
  each address to the sewage transactions that spent from it, paid it, or
  pointed runes or a Counterparty enhanced send at it, and in what role. It
  reads runestones and Counterparty enhanced sends to find which addresses
  they point tokens to.
- `tools/update.py` runs from cron each minute with `--watch 57`: it checks the
  node's tip every 2 seconds, and when the best block changes fetches new
  blocks over REST, reprocesses anything a reorg replaced, renders what
  changed. A share image is drawn again only when its SVG changed.

The payload is never displayed. Address Lookup shows which addresses a
runestone or a Counterparty enhanced send points tokens to, and nothing else
from them.

A `--watch` run keeps the code it started with until it ends, 57 seconds after
it started or later if an update is still running then. After a code change,
wait for it to end before counting on the new code: until then a build can
write pages from the old code next to the new static files.

## State

`~/.cesspool/`: `raw/` gzipped REST blocks (about 340 kB each), `blocks/`
one record per block, `index.json` summaries, `found.json` each block's
coinbase tag and paying outputs, `address.json` which blocks Address Lookup
has indexed and where their rows went (delete it to rebuild every address
file), `og.json` a hash of the SVG behind each share
image (delete it while holding `lock` to draw them all again, as after a font
or rsvg-convert change), `bin/test_bitcoin-plumb`.

Rebuild everything after a classifier change:

    rm ~/.cesspool/blocks/*.json ~/.cesspool/index.json
    tools/backfill.py 961640 <tip>
    tools/update.py --out /var/www/cesspool.lol --all

Setup on node1: `deploy/setup.sh`.

## License

MIT, see `LICENSE`. The pool naming loads `reorg-watch.py` from
[reorg-watch](https://github.com/jasonsopko/reorg-watch), also MIT.
`static/beh-discord.png` is the Bitcoin Education Hub's Discord server icon;
it is not mine and the license does not cover it.
