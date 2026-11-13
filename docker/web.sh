#!/bin/sh
# The web process: nginx on 8080, serving what the updater rendered into /data/www/current. Until
# the first build is there it serves a page that says what the updater is waiting for.
#
# With CESSPOOL_SETTINGS_PAGE=1 (Umbrel, which has no settings form of its own) it also runs the small
# server behind the form on /about/, on loopback, and tells nginx to pass it POST /settings. That
# needs /data writable here; it writes /data/settings.json and nothing else.
set -e
rm -f /tmp/cesspool-nginx-settings.conf
case "${CESSPOOL_SETTINGS_PAGE:-}" in
  1|true|yes|on)
    cat > /tmp/cesspool-nginx-settings.conf <<'NGX'
location = /settings {
    limit_except POST { deny all; }
    client_max_body_size 1k;
    proxy_pass http://127.0.0.1:8081;
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "default-src 'none'" always;
}
NGX
    # Restarted if it ever exits, so a crash costs a failed save, not the form for good.
    ( while :; do python3 /app/tools/settings_server.py --port 8081 || true; sleep 2; done ) &
    ;;
esac
exec nginx -c /app/docker/nginx.conf -e stderr -g 'daemon off;'
