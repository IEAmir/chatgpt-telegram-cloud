"""Public router for the engine container.

Routes (public $PORT):
  GET  /healthz               aggregate health — HTTP 200 whenever the router is
                              up (Render health check); the body carries the
                              real component states.
  POST /admin/cookies         write + inject ChatGPT cookies, restart web2api (admin)
  POST /admin/restart-engine  restart the web2api process (admin)
  POST /admin/restart-chrome  kill Chrome; the entrypoint supervisor relaunches it (admin)
  /img/*                      pixel-bridge MCP http shim (x-api-secret)
  everything else             chatgpt-web2api REST API (it enforces Bearer api_keys)
"""
import asyncio
import json
import os
import uuid

from aiohttp import ClientSession, ClientTimeout, web

W2A = f"http://127.0.0.1:{os.environ.get('W2A_PORT', '8080')}"
BRIDGE = f"http://127.0.0.1:{os.environ.get('BRIDGE_PORT', '8090')}"
CDP = f"http://127.0.0.1:{os.environ.get('W2A_CDP_PORT', '9222')}"
API_SECRET = os.environ.get("API_SECRET", "").strip()
COOKIE_FILE = "/data/cookies/cookies.json"
BOOT_ID = uuid.uuid4().hex[:12]

HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "keep-alive",
               "host", "server", "date"}


def _authorized(request: "web.Request") -> bool:
    if not API_SECRET:
        return True
    return request.headers.get("x-admin-secret", "") == API_SECRET


async def _proxy(request: "web.Request", target_base: str,
                 strip_prefix: str = "") -> "web.StreamResponse":
    path_qs = request.rel_url.path_qs
    if strip_prefix and path_qs.startswith(strip_prefix):
        path_qs = path_qs[len(strip_prefix):] or "/"
    url = target_base + path_qs
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    body = await request.read() if request.body_exists else None
    try:
        async with request.app["client"].request(
            request.method, url, headers=headers, data=body, allow_redirects=False
        ) as upstream:
            out_headers = {k: v for k, v in upstream.headers.items()
                           if k.lower() not in HOP_HEADERS}
            response = web.StreamResponse(status=upstream.status, headers=out_headers)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
    except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
        return web.json_response({"error": f"upstream unreachable: {exc}"}, status=502)


async def _probe(app: "web.Application", url: str, timeout: float = 5.0):
    try:
        async with app["client"].get(url, timeout=ClientTimeout(total=timeout)) as r:
            body = None
            if r.content_type == "application/json":
                body = await r.json(content_type=None)
            return r.status == 200, body if isinstance(body, dict) else {}
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        return False, {}


async def healthz(request: "web.Request") -> "web.Response":
    app = request.app
    w2a_ok, w2a_body = await _probe(app, f"{W2A}/health", timeout=8)
    bridge_ok, _ = await _probe(app, f"{BRIDGE}/health")
    cdp_ok, _ = await _probe(app, f"{CDP}/json/version")

    payload = {
        "ok": w2a_ok and bridge_ok and cdp_ok,
        "boot_id": BOOT_ID,
        "web2api": {"ok": w2a_ok, "detail": w2a_body},
        "pixel_bridge": {"ok": bridge_ok},
        "chrome_cdp": {"ok": cdp_ok},
        "cookies_file": os.path.exists(COOKIE_FILE),
    }
    # Always 200 while the router itself is alive: Render must not restart the
    # container just because web2api is waiting for ChatGPT cookies.
    return web.json_response(payload, status=200)


async def _run(cmd: list[str], timeout: float = 150.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return out.decode("utf-8", "replace")


async def _wait_w2a_healthy(app: "web.Application", deadline_s: float = 90.0) -> dict:
    import time
    deadline = time.monotonic() + deadline_s
    detail = {}
    while time.monotonic() < deadline:
        ok, detail = await _probe(app, f"{W2A}/health", timeout=8)
        if ok:
            return {"healthy": True, "detail": detail}
        await asyncio.sleep(4)
    return {"healthy": False, "detail": detail}


async def admin_cookies(request: "web.Request") -> "web.Response":
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "body must be JSON"}, status=400)

    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if isinstance(payload, list):
        cookies = payload
    if not isinstance(cookies, list) or not cookies:
        return web.json_response({"error": "cookies array missing"}, status=400)

    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f)

    # Cookies must land in Chrome while web2api is not driving a tab.
    try:
        await _run(["pkill", "-f", "chatgpt-web2api start"] + [])
    except Exception:
        pass
    await asyncio.sleep(2)

    try:
        output = await _run(
            ["chatgpt-web2api", "inject-cookies", COOKIE_FILE,
             "--config", "/app/render-config.json"],
            timeout=150,
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "inject timed out"}, status=500)

    verified = "Auth verified" in output
    # web2api's supervisor loop restarts it within ~7s; wait for it to come
    # back healthy with the fresh session.
    health = await _wait_w2a_healthy(request.app, deadline_s=90)
    return web.json_response({
        "ok": True,
        "verified": verified,
        "web2api": health,
        "output": output[-600:],
    })


async def admin_restart_engine(request: "web.Request") -> "web.Response":
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        await _run(["pkill", "-f", "chatgpt-web2api start"], timeout=20)
    except Exception:
        pass
    health = await _wait_w2a_healthy(request.app, deadline_s=90)
    return web.json_response({"ok": health.get("healthy", False), "web2api": health})


async def admin_restart_chrome(request: "web.Request") -> "web.Response":
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        await _run(["pkill", "-f", "user-data-dir=/data/chrome-profile"], timeout=20)
    except Exception:
        pass
    import time
    deadline = time.monotonic() + 60
    cdp_ok = False
    while time.monotonic() < deadline:
        cdp_ok, _ = await _probe(request.app, f"{CDP}/json/version")
        if cdp_ok:
            break
        await asyncio.sleep(3)
    health = await _wait_w2a_healthy(request.app, deadline_s=60)
    return web.json_response({"ok": cdp_ok, "chrome_cdp": cdp_ok, "web2api": health})


def main() -> None:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/admin/cookies", admin_cookies)
    app.router.add_post("/admin/restart-engine", admin_restart_engine)
    app.router.add_post("/admin/restart-chrome", admin_restart_chrome)
    app.router.add_route("*", "/img/{tail:.*}",
                         lambda r: _proxy(r, BRIDGE, strip_prefix="/img"))
    app.router.add_route("*", "/{tail:.*}", lambda r: _proxy(r, W2A))

    async def on_start(app_: "web.Application") -> None:
        app_["client"] = ClientSession(timeout=ClientTimeout(total=600, sock_read=300))

    async def on_cleanup(app_: "web.Application") -> None:
        await app_["client"].close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    port = int(os.environ.get("PORT", "8080"))
    print(f"[router] listening on 0.0.0.0:{port} (boot_id={BOOT_ID})", flush=True)
    web.run_app(app, host="0.0.0.0", port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
