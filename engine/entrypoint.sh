#!/bin/bash
# Engine entrypoint: Xvfb + Chrome (entrypoint-owned) + pixel-bridge MCP HTTP
# shim + chatgpt-web2api under a supervisor loop + public router.
#
# Chrome is started HERE (not by web2api) so that cookies can be injected via
# CDP before web2api connects, and so web2api can be restarted (e.g. after a
# cookie refresh) without killing the browser. web2api attaches to the
# existing CDP endpoint as a non-owner.
set -e

CDP_PORT="${W2A_CDP_PORT:-9222}"
W2A_PORT="${W2A_PORT:-8080}"
BRIDGE_PORT="${BRIDGE_PORT:-8090}"
ROUTER_PORT="${PORT:-8080}"

mkdir -p /data/cookies /data/chrome-profile /data/pixel-bridge/assets \
         /data/pixel-bridge/uploads /data/pixel-bridge/logs /tmp

python3 /app/gen_config.py > /app/render-config.json
echo "[entrypoint] render-config.json written"

# Cookies may arrive as raw JSON or base64 in COOKIES_JSON.
if [ -n "$COOKIES_JSON" ]; then
    if echo "$COOKIES_JSON" | base64 -d > /data/cookies/cookies.json 2>/dev/null \
       && python3 -c "import json;json.load(open('/data/cookies/cookies.json'))" 2>/dev/null; then
        echo "[entrypoint] cookies decoded from base64"
    else
        printf '%s' "$COOKIES_JSON" > /data/cookies/cookies.json
        echo "[entrypoint] cookies written as raw JSON"
    fi
fi

echo "[entrypoint] starting Xvfb on :99"
# Render may restart the container PROCESS (not the filesystem): a stale
# X99 lock would make Xvfb exit with "Server is already active for display 99".
rm -f /tmp/.X99-lock /tmp/.X99-lock.test /tmp/.X11-unix/X99 2>/dev/null || true
Xvfb :99 -screen 0 800x600x24 -nolisten tcp &
export DISPLAY=:99

echo "[entrypoint] starting Chrome (supervised) on CDP :$CDP_PORT"
(
    while true; do
        /usr/local/bin/render-chrome \
            --remote-debugging-port="$CDP_PORT" \
            --user-data-dir=/data/chrome-profile \
            --no-first-run --no-default-browser-check \
            --disable-sync --disable-popup-blocking \
            https://chatgpt.com >/dev/null 2>&1 || true
        echo "[chrome-supervisor] chrome exited; restarting in 3s" >&2
        sleep 3
    done
) &

echo "[entrypoint] waiting for Chrome CDP..."
for i in $(seq 1 90); do
    if curl -s "http://127.0.0.1:$CDP_PORT/json/version" > /dev/null 2>&1; then
        echo "[entrypoint] Chrome CDP is up"
        break
    fi
    sleep 1
done

# Inject cookies BEFORE web2api starts, so its first driver connect succeeds.
if [ -f /data/cookies/cookies.json ]; then
    echo "[entrypoint] injecting cookies..."
    chatgpt-web2api inject-cookies /data/cookies/cookies.json \
        --config /app/render-config.json --cdp-port "$CDP_PORT" \
        || echo "[entrypoint] WARNING: boot cookie injection failed"
    sleep 2
fi

echo "[entrypoint] starting pixel-bridge MCP http shim on :$BRIDGE_PORT"
BRIDGE_PORT="$BRIDGE_PORT" node /app/pixel-bridge/mcp-http-bridge.mjs &

echo "[entrypoint] starting chatgpt-web2api under supervisor (REST :$W2A_PORT)"
(
    while true; do
        chatgpt-web2api start --config /app/render-config.json \
            --port "$W2A_PORT" --cdp-port "$CDP_PORT" || true
        echo "[w2a-supervisor] web2api exited; restarting in 5s" >&2
        sleep 5
    done
) &

# The router stays in the foreground as the container's main process.
echo "[entrypoint] starting router on :$ROUTER_PORT"
exec python3 /app/router.py
