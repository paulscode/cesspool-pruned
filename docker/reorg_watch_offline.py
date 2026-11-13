"""reorg-watch with its network calls turned off.

cesspool/miner.py loads the file REORG_WATCH names and builds its Pools from it, and reorg-watch's
Pools downloads the pool list from GitHub whenever its copy is more than six hours old. This install
names pools from the list that shipped with it (cesspool/local.py puts it in place) and makes no
outbound request, so this file stands in for reorg-watch.py: it loads the real one, named by
REORG_WATCH_REAL, replaces the function every download goes through with one that fails, and passes
everything else on unchanged. Pools reads a failed download as "keep the copy you have".
"""
import importlib.util, os

_spec = importlib.util.spec_from_file_location("reorg_watch_real", os.environ["REORG_WATCH_REAL"])
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)


def _no_network(url, *args, **kwargs):
    raise OSError(f"this install makes no outbound requests (asked for {url})")


_real.http_get = _no_network
globals().update({k: v for k, v in vars(_real).items() if not k.startswith("__")})
