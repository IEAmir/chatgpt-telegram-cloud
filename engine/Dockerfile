FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Node.js 20 (for pixel-bridge), Chromium + Xvfb (headed Chrome on a virtual
# display, since headless triggers ChatGPT's bot detection), fonts for
# multilingual rendering (the bot is Persian-first), and process tools.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg xvfb chromium \
        fonts-liberation fonts-noto-core fonts-noto-color-emoji \
        procps tini \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# chatgpt-web2api (vendored from github.com/Octo-Lex/ChatGPT-Web2API, MIT)
COPY chatgpt-web2api/ /app/chatgpt-web2api/
RUN pip install --no-cache-dir ./chatgpt-web2api

# pixel-bridge-mcp (vendored from github.com/Mrshahidali420/pixel-bridge-mcp, MIT).
# Runs in attach mode against the container's own Chromium, so no Playwright
# browser download is required.
COPY pixel-bridge/ /app/pixel-bridge/
RUN cd /app/pixel-bridge && npm ci && npm run build \
 && chmod +x mcp-http-bridge.mjs

COPY gen_config.py entrypoint.sh router.py /app/
RUN chmod +x /app/entrypoint.sh

COPY render-chrome /usr/local/bin/render-chrome
RUN chmod +x /usr/local/bin/render-chrome

ENV W2A_CHROME_PATH=/usr/local/bin/render-chrome \
    W2A_USER_DATA_DIR=/data/chrome-profile \
    W2A_HOST=127.0.0.1 \
    W2A_PORT=8080 \
    W2A_CDP_PORT=9222 \
    W2A_LOG_LEVEL=INFO \
    PORT=10000 \
    DISPLAY=:99 \
    PIXEL_BRIDGE_HOME=/data/pixel-bridge \
    PIXEL_BRIDGE_CDP_URL=http://127.0.0.1:9222 \
    NODE_OPTIONS=--max-old-space-size=384

VOLUME /data
EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/entrypoint.sh"]
