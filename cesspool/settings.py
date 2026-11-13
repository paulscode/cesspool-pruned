"""The settings a user can change from the site itself, on platforms with no settings form of their
own (Umbrel). StartOS has actions for the same two, and there the environment decides.

    CESSPOOL_SETTINGS_PAGE=1   offer the form on /about/ and let <data>/settings.json override the
                               environment; anything else leaves both out

<data>/settings.json: {"backfill": true|false, "max_gb": one of MAX_GB_CHOICES}. tools/settings_server.py
writes it, from the form; cesspool/local.py reads it at the start of every update, so a change takes
effect within a minute. Standard library only, and no import of the rest of the package.
"""
import json, os

MAX_GB_CHOICES = (10, 25, 50, 100, 250, 500, 0)  # 0: no limit
KEYS = ("backfill", "max_gb")


def enabled():
    return os.environ.get("CESSPOOL_SETTINGS_PAGE", "").strip().lower() in ("1", "true", "yes", "on")


def path():
    return os.path.join(os.environ.get("CESSPOOL_DATA", "").strip() or "/data", "settings.json")


def valid(doc):
    """The recognised, well-formed entries of a settings document; anything else is dropped."""
    out = {}
    if not isinstance(doc, dict):
        return out
    if isinstance(doc.get("backfill"), bool):
        out["backfill"] = doc["backfill"]
    if type(doc.get("max_gb")) is int and doc["max_gb"] in MAX_GB_CHOICES:
        out["max_gb"] = doc["max_gb"]
    return out


def read():
    """What the user chose, or {} when the page is off or nothing has been chosen."""
    if not enabled():
        return {}
    try:
        with open(path()) as f:
            return valid(json.load(f))
    except (OSError, ValueError):
        return {}


def write(doc):
    """Store the settings, merged over what is there, in one step. Returns what is stored now."""
    cur = read() if enabled() else {}
    cur.update(valid(doc))
    target = path()
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return cur
