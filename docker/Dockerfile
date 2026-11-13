# syntax=docker/dockerfile:1.7
#
# Cesspool Pruned: one image, two processes.
#
#   docker/updater.sh   grades blocks and renders the site into /data
#   docker/web.sh       nginx on 8080, serving what the updater rendered
#
# Build from the repository root, for one platform or both:
#
#   docker buildx build -f docker/Dockerfile --platform linux/amd64,linux/arm64 -t cesspool-pruned .
#
# The checker is compiled on the build machine for either target (stage "checker" runs on
# $BUILDPLATFORM and cross-compiles), so an arm64 image needs QEMU only for the short apt and
# smoke-test steps of the last stage, not for compiling Knots.

# The Plumb release the verdicts come from. PLUMB_TAG must equal PLUMB_TAG in cesspool/site.py,
# and the build stops when it does not: the pages name the release, so the binary has to be it.
ARG PLUMB_TAG=v29.5.knots20260508.plumb10
ARG PLUMB_COMMIT=95017d730d3d43903f60c9ec0f53d128c8694b81
# reorg-watch, whose pool-naming rules cesspool/miner.py loads (MIT).
ARG REORG_WATCH_COMMIT=6a47d78a0268240085006b068147a30bd53cb5ec
ARG REORG_WATCH_SHA256=9c7355a56812427944968509a8594146a93c8b60cf9d162c4b93deff70215f6d
ARG REORG_WATCH_LICENSE_SHA256=58aae2d7ec0ce5bc54b7ca8bc44a1a51e5907e4f1824b99eb8592244e2ac3221
# IBM Plex (SIL Open Font License 1.1), from google/fonts. IBMPlexSans.ttf is the variable font
# that static/site.css asks for by that name.
ARG FONTS_COMMIT=c30b221beb0c1da59d563feb91ec8656a1fa972a


# ---------------------------------------------------------------- the tag
# A stage of its own, so that it fails within seconds and so that editing site.py does not
# throw away the compiled checker below.
FROM debian:bookworm-slim AS tagcheck
ARG PLUMB_TAG
COPY cesspool/site.py /tmp/site.py
RUN <<EOF
set -eu
named=$(sed -n 's/^PLUMB_TAG = "\(.*\)"$/\1/p' /tmp/site.py)
if [ "$named" != "$PLUMB_TAG" ]; then
    echo "cesspool/site.py names Plumb $named and this Dockerfile builds $PLUMB_TAG: set PLUMB_TAG and PLUMB_COMMIT to the release the site names" >&2
    exit 1
fi
echo "$named" > /plumb-tag
EOF


# ---------------------------------------------------------------- the checker
# test_bitcoin from the Plumb tree with plumb-check/plumb_check_tests.cpp as its only test
# suite. GCC 12 on Debian 12, the compiler line Plumb's own release build uses; libstdc++, Boost
# and libevent are linked in, so the binary needs only glibc and libgcc_s, which the Debian 13
# runtime below has in newer versions.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS checker
ARG TARGETARCH
ARG BUILDARCH
ARG PLUMB_TAG
ARG PLUMB_COMMIT
ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C TZ=UTC
RUN <<EOF
set -eu
apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake pkgconf python3 git ca-certificates curl bison xz-utils bzip2 patch
if [ "$TARGETARCH" != "$BUILDARCH" ]; then
    case "$TARGETARCH" in
        arm64) apt-get install -y --no-install-recommends g++-aarch64-linux-gnu binutils-aarch64-linux-gnu ;;
        amd64) apt-get install -y --no-install-recommends g++-x86-64-linux-gnu binutils-x86-64-linux-gnu ;;
        *) echo "no cross compiler set up for $TARGETARCH" >&2; exit 1 ;;
    esac
fi
rm -rf /var/lib/apt/lists/*
EOF

RUN <<EOF
set -eu
git clone --quiet --depth 1 --branch "$PLUMB_TAG" https://github.com/plumb-node/plumb.git /src
got=$(git -C /src rev-parse HEAD)
if [ "$got" != "$PLUMB_COMMIT" ]; then
    echo "tag $PLUMB_TAG is at $got, not the pinned $PLUMB_COMMIT" >&2
    exit 1
fi
EOF

# What depends calls the target: this machine for a native build, the Debian cross triplet otherwise.
RUN <<EOF
set -eu
if [ "$TARGETARCH" = "$BUILDARCH" ]; then
    sh /src/depends/config.guess > /host
else
    case "$TARGETARCH" in
        arm64) echo aarch64-linux-gnu > /host ;;
        amd64) echo x86_64-linux-gnu > /host ;;
    esac
fi
echo "building for $(cat /host)"
EOF

# Boost and libevent only: no wallet, GUI, ZMQ, UPnP or tracepoints.
RUN make -C /src/depends HOST="$(cat /host)" NO_QT=1 NO_QR=1 NO_WALLET=1 NO_ZMQ=1 NO_UPNP=1 NO_USDT=1 \
        LDFLAGS="-static-libstdc++ -Wl,-O2 -Wl,--as-needed" -j"$(nproc)" >/tmp/depends.log 2>&1 \
    || { tail -n 60 /tmp/depends.log; exit 1; }

# The engine goes in as plumb-check/build.sh puts it in. Unlike build.sh this also takes the
# tree's own test suites out of the binary: process.py only ever runs plumb_check_tests, and
# leaving out some 150 files that nothing runs shortens the build and the binary. The node
# libraries it links, which hold the policy code, are built exactly as before.
#
# The libraries are built first, against an empty stand-in for the engine, so that a change to
# plumb_check_tests.cpp compiles two files and links rather than building Knots again.
RUN python3 - <<'EOF'
import re
path = "/src/src/test/CMakeLists.txt"
text = open(path).read()
new, n = re.subn(r"add_executable\(test_bitcoin\n.*?\n\)\n", "add_executable(test_bitcoin\n  main.cpp\n  plumb_check_tests.cpp\n)\n", text, count=1, flags=re.S)
assert n == 1, "src/test/CMakeLists.txt no longer lists test_bitcoin's sources the way this expects"
open(path, "w").write(new)
open("/src/src/test/plumb_check_tests.cpp", "w").close()
EOF

RUN <<EOF
set -eu
cmake -S /src -B /src/build --toolchain "/src/depends/$(cat /host)/toolchain.cmake" \
      -DBUILD_GUI=OFF -DBUILD_BENCH=OFF -DBUILD_FUZZ_BINARY=OFF -DBUILD_TESTS=ON \
      -DENABLE_WALLET=OFF -DWITH_ZMQ=OFF -DWITH_MINIUPNPC=OFF -DWITH_USDT=OFF \
      -DWITH_CCACHE=OFF -DINSTALL_MAN=OFF >/tmp/configure.log 2>&1 || { tail -n 60 /tmp/configure.log; exit 1; }
cmake --build /src/build --target test_util bitcoin_node bitcoin_cli bitcoin_consensus secp256k1 -j"$(nproc)" >/tmp/build.log 2>&1 \
    || { tail -n 80 /tmp/build.log; exit 1; }
EOF

COPY plumb-check/plumb_check_tests.cpp /src/src/test/plumb_check_tests.cpp
RUN <<EOF
set -eu
# COPY keeps the file's own date, which can be older than the stand-in's object file.
touch /src/src/test/plumb_check_tests.cpp
cmake --build /src/build --target test_bitcoin -j"$(nproc)" >/tmp/build.log 2>&1 || { tail -n 80 /tmp/build.log; exit 1; }
strip="strip"
[ "$TARGETARCH" = "$BUILDARCH" ] || strip="$(cat /host)-strip"
install -D -m 755 /src/build/bin/test_bitcoin /out/test_bitcoin-plumb
"$strip" /out/test_bitcoin-plumb
ls -l /out/test_bitcoin-plumb
EOF


# The binary alone, for running the pipeline outside a container:
#   docker buildx build -f docker/Dockerfile --target checker-bin --output type=local,dest=out .
FROM scratch AS checker-bin
COPY --from=checker /out/test_bitcoin-plumb /test_bitcoin-plumb
COPY --from=checker /src/plumb/filters.json /filters.json


# ---------------------------------------------------------------- the image
FROM debian:trixie-slim
ARG PLUMB_TAG
ARG REORG_WATCH_COMMIT
ARG REORG_WATCH_SHA256
ARG REORG_WATCH_LICENSE_SHA256
ARG FONTS_COMMIT
ENV DEBIAN_FRONTEND=noninteractive
# tzdata: reorg-watch builds ZoneInfo("UTC") when it is imported. librsvg2-bin and fontconfig
# draw the share images, which are off unless CESSPOOL_OG=1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 nginx librsvg2-bin fontconfig tini tzdata \
 && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default \
 && nginx -V 2>&1 | grep -q -- --with-http_gunzip_module \
 && nginx -V 2>&1 | grep -q -- --with-http_gzip_static_module

COPY --from=tagcheck /plumb-tag /opt/plumb/TAG
COPY --from=checker /out/test_bitcoin-plumb /usr/local/bin/test_bitcoin-plumb
COPY --from=checker /src/plumb/filters.json /opt/plumb/filters.json
COPY --from=checker /src/COPYING /opt/plumb/COPYING

ADD --checksum=sha256:${REORG_WATCH_SHA256} \
    https://raw.githubusercontent.com/jasonsopko/reorg-watch/${REORG_WATCH_COMMIT}/reorg-watch.py /opt/reorg-watch/reorg-watch.py
ADD --checksum=sha256:${REORG_WATCH_LICENSE_SHA256} \
    https://raw.githubusercontent.com/jasonsopko/reorg-watch/${REORG_WATCH_COMMIT}/LICENSE /opt/reorg-watch/LICENSE

ADD --checksum=sha256:3b031aa4216174205bd8471f88a49b91f093169e9e87bd5262242bc5967fe2e3 \
    https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/ibmplexsans/IBMPlexSans%5Bwdth%2Cwght%5D.ttf /usr/local/share/fonts/plex/IBMPlexSans.ttf
ADD --checksum=sha256:6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46 \
    https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/ibmplexmono/IBMPlexMono-Regular.ttf /usr/local/share/fonts/plex/IBMPlexMono-Regular.ttf
ADD --checksum=sha256:a9b4c49bb299e05b5f6c481e7fb5e78943d2793249a0c8874ab574a2d1ea6755 \
    https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/ibmplexmono/IBMPlexMono-Medium.ttf /usr/local/share/fonts/plex/IBMPlexMono-Medium.ttf
ADD --checksum=sha256:7e6b2818edbd8f6a01ae80641cc8f16a51080d08fb4e532be3a0b6f74adb07da \
    https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/ibmplexsans/OFL.txt /usr/local/share/fonts/plex/OFL.txt

COPY cesspool /app/cesspool
COPY tools /app/tools
# Not run from here: cesspool/local.py reads it to tell when a release grades differently.
COPY plumb-check/plumb_check_tests.cpp /app/plumb-check/plumb_check_tests.cpp
COPY static /app/static
COPY docker/updater.sh docker/web.sh docker/reorg_watch_offline.py /app/docker/
COPY docker/nginx.conf /app/docker/nginx.conf
COPY docker/starting /app/docker/starting
COPY docker/pools /opt/pools
COPY README.md LICENSE /app/

# A user for platforms that run the image unprivileged (Umbrel runs it as 1000:1000). Files the
# updater writes are world-readable, so nginx can serve them whichever user either process has.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /data cesspool \
 && chmod 644 /opt/reorg-watch/* /usr/local/share/fonts/plex/* \
 && chmod 755 /app/docker/updater.sh /app/docker/web.sh \
 && fc-cache -f \
 && mkdir -p /data && chown 1000:1000 /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLUMB_TAG=${PLUMB_TAG} \
    PLUMB_CHECK_BIN=/usr/local/bin/test_bitcoin-plumb \
    PLUMB_FILTERS=/opt/plumb/filters.json \
    REORG_WATCH=/app/docker/reorg_watch_offline.py \
    REORG_WATCH_REAL=/opt/reorg-watch/reorg-watch.py \
    POOL_SURVEY=/data/pool-survey.json \
    SV1_PORT_IDENTITIES=/data/sv1-port-identities.json \
    CESSPOOL_POOLS=/opt/pools \
    CESSPOOL_FONTS=/usr/local/share/fonts/plex \
    CESSPOOL_DATA=/data

# The image has to be able to grade a transaction before it is worth shipping: the engine runs
# on a real one for both chains, the pool rules load with the network turned off, and nginx
# accepts its configuration. All of it as the unprivileged user, the way Umbrel runs the image.
COPY --chown=1000:1000 tests/fixtures/smoke.txt /tmp/smoke.txt
USER 1000:1000
RUN <<EOF
set -eu
for chain in blake2b sha256; do
    PLUMB_CHECK_CHAIN=$chain PLUMB_CHECK_IN=/tmp/smoke.txt PLUMB_CHECK_OUT=/tmp/smoke.$chain \
        test_bitcoin-plumb --run_test=plumb_check_tests --log_level=error
    grep -q '"v":' /tmp/smoke.$chain
done
cd /app && CESSPOOL_HOME=/tmp/smoke-home python3 -c "
from cesspool import miner, site
rw = miner.rw()
assert rw.coinbase_tag('') == '', 'reorg-watch did not load'
assert site.PLUMB_TAG == '${PLUMB_TAG}'
print('pool rules and site load;', len(site.PLUMB_FILTERS), 'Plumb filters')
"
nginx -t -c /app/docker/nginx.conf -e stderr
rm -rf /tmp/smoke* /tmp/cesspool-nginx* "/tmp/test_common bitcoin"
test -z "$(ls -A /tmp)"
EOF

WORKDIR /app
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["tini", "--"]
CMD ["/app/docker/web.sh"]
