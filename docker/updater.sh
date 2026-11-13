#!/bin/sh
# The updater process: grades blocks and renders the site into /data. tools/serve.py does the work;
# this only makes sure the platform's stop signal reaches it.
exec python3 /app/tools/serve.py "$@"
