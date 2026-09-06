"""Generate /app/render-config.json for chatgpt-web2api from environment vars.

Keeps the vendored web2api source untouched: everything it reads comes from
this config file plus standard W2A_* env vars.
"""
import json
import os

config = {
    "chrome": {
        "chrome_path": os.environ.get("W2A_CHROME_PATH", "/usr/local/bin/render-chrome"),
        "user_data_dir": os.environ.get("W2A_USER_DATA_DIR", "/data/chrome-profile"),
        "cdp_port": int(os.environ.get("W2A_CDP_PORT", "9222")),
        "headless": False,
    },
    "server": {
        "port": int(os.environ.get("W2A_PORT", "8080")),
        "host": "127.0.0.1",
        "request_timeout": int(os.environ.get("W2A_REQUEST_TIMEOUT", "240")),
    },
    "chatgpt": {
        "default_model": os.environ.get("W2A_DEFAULT_MODEL", "auto"),
        "tab_mode": "owned",
    },
    "log": {
        "level": os.environ.get("W2A_LOG_LEVEL", "INFO"),
    },
}

api_secret = os.environ.get("API_SECRET", "").strip()
if api_secret:
    config["server"]["api_keys"] = [api_secret]

print(json.dumps(config, indent=2))
