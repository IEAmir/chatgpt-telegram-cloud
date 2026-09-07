"""Public router for the engine container.

Routes (public $PORT):
  GET  /healthz               aggregate health — HTTP 200 whenever the router is
                              up (Render health check); the body carries the
                              real component states.
  POST /admin/cookies         write + inject ChatGPT cookies via CDP, restart web2api (admin)
  GET  /admin/page-info       inspect the live ChatGPT page (admin)
  POST /admin/restart-engine  restart the web2api process (admin)
  POST /admin/restart-chrome  kill Chrome; the entrypoint supervisor relaunches it (admin)
  /img/*                      pixel-bridge MCP http shim (x-api-secret)
  everything else             chatgpt-web2api REST API (it enforces Bearer api_keys)
"""
import asyncio
import base64
import json
import os
import uuid

import websockets
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


def _img_authorized(request: "web.Request") -> bool:
    if not API_SECRET:
        return True
    return request.headers.get("x-api-secret", "") == API_SECRET


# ── Proxying ─────────────────────────────────────────────────────────────

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


async def _proxy_img(request: "web.Request") -> "web.StreamResponse":
    if not _img_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await _proxy(request, BRIDGE, strip_prefix="/img")


# ── Health ───────────────────────────────────────────────────────────────

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


# ── Admin helpers ────────────────────────────────────────────────────────

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


class CdpPage:
    """Minimal async CDP client for a single page target."""

    def __init__(self):
        self.ws = None
        self._mid = 0
        self.target = None

    async def open(self):
        async with ClientSession() as s:
            async with s.get(f"{CDP}/json/list") as r:
                targets = await r.json()
        pages = [t for t in targets if t.get("type") == "page"]
        chatgpt_pages = [t for t in pages if "chatgpt.com" in t.get("url", "")]
        self.target = (chatgpt_pages or pages)[0]
        self.ws = await websockets.connect(self.target["webSocketDebuggerUrl"],
                                           max_size=64 * 1024 * 1024)
        return self

    async def send(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict:
        self._mid += 1
        mid = self._mid
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    async def eval(self, expression: str, timeout: float = 20.0):
        r = await self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        }, timeout=timeout + 5)
        return r.get("result", {}).get("value")

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None


# ── Admin routes ─────────────────────────────────────────────────────────

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

    # Cloudflare tokens (__cf_bm/_cfuvid) are bound to the exporting browser's
    # IP; dropping them lets the server-side Chrome earn fresh ones.
    skip = {"__cf_bm", "_cfuvid"}
    cookies = [c for c in cookies if c.get("name") not in skip]
    # Strip control characters: Telegram paste-wrapping can inject raw
    # newlines into long cookie values and Chrome rejects those outright.
    for c in cookies:
        c["value"] = "".join(ch for ch in c.get("value", "")
                             if ch not in "\r\n\t")

    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f)

    try:
        await _run(["pkill", "-f", "chatgpt-web2api start"], timeout=20)
    except Exception:
        pass
    await asyncio.sleep(2)

    steps: list[str] = []
    verified = False
    page = None
    try:
        page = await CdpPage().open()
        steps.append(f"tab: {page.target.get('url')}")
        await page.send("Page.navigate", {"url": "https://chatgpt.com/"})
        await asyncio.sleep(10)
        for c in cookies:
            cdp_cookie = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ".chatgpt.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
                "httpOnly": c.get("httpOnly", False),
            }
            sm = str(c.get("sameSite", "")).lower()
            cdp_cookie["sameSite"] = {"lax": "Lax", "strict": "Strict",
                                      "no_restriction": "None"}.get(sm, "Lax")
            if c.get("expirationDate"):
                cdp_cookie["expires"] = float(c["expirationDate"])
            await page.send("Network.setCookie", cdp_cookie)
        steps.append(f"set {len(cookies)} cookies")
        await page.send("Page.reload")
        for i in range(6):
            await asyncio.sleep(8)
            state = await page.eval("JSON.stringify({t:document.title,u:location.href,prompt:!!document.querySelector('#prompt-textarea'),cf:!!document.querySelector('iframe[src*=\"challenges.cloudflare\"]')})")
            steps.append(f"poll{i}: {state}")
            verdict = await page.eval("(async()=>{try{const r=await fetch('/api/auth/session',{credentials:'include'});const d=await r.json();return d.accessToken?('OK:'+(d.user?.name||'')):('FAIL:'+JSON.stringify(d).slice(0,100))}catch(e){return 'ERR:'+e.message}})()")
            steps.append(f"auth{i}: {str(verdict)[:100]}")
            if isinstance(verdict, str) and verdict.startswith("OK:"):
                verified = True
                break
    except Exception as exc:
        steps.append(f"error: {exc}")
    finally:
        if page:
            await page.close()

    health = await _wait_w2a_healthy(request.app, deadline_s=90)
    return web.json_response({"ok": True, "verified": verified,
                              "steps": steps[-14:], "web2api": health})


async def admin_page_info(request: "web.Request") -> "web.Response":
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        page = await CdpPage().open()
        info = await page.eval("JSON.stringify({title:document.title,location:location.href,bodySnippet:document.body?document.body.innerText.slice(0,400):'',hasPromptTextarea:!!document.querySelector('#prompt-textarea'),hasCloudflareFrame:!!document.querySelector('iframe[src*=\"challenges.cloudflare\"]'),visibility:document.visibilityState})")
        shot = await page.send("Page.captureScreenshot", {"format": "jpeg", "quality": 40})
        await page.close()
        return web.json_response({
            "target_url": page.target.get("url") if page.target else None,
            "info": json.loads(info) if isinstance(info, str) else info,
            "screenshot_b64": shot.get("data", ""),
        })
    except Exception as exc:
        return web.json_response({"error": f"CDP failed: {exc}"}, status=500)


async def admin_auth_probe(request: "web.Request") -> "web.Response":
    """Full /api/auth/session response + cookie presence, for diagnosing auth."""
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        page = await CdpPage().open()
        session = await page.eval("(async()=>{try{const r=await fetch('/api/auth/session',{credentials:'include'});const t=await r.text();return t.slice(0,1500)}catch(e){return 'ERR:'+e.message}})()")
        cookie_state = await page.eval("JSON.stringify({hasSessionToken: document.cookie.includes('session-token'), title: document.title, url: location.href})")
        await page.close()
        return web.json_response({"session_response": session, "page": json.loads(cookie_state) if isinstance(cookie_state, str) else cookie_state})
    except Exception as exc:
        return web.json_response({"error": f"CDP failed: {exc}"}, status=500)


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
    app.router.add_get("/admin/page-info", admin_page_info)
    app.router.add_get("/admin/auth-probe", admin_auth_probe)
    app.router.add_post("/admin/restart-engine", admin_restart_engine)
    app.router.add_post("/admin/restart-chrome", admin_restart_chrome)
    app.router.add_route("*", "/img/{tail:.*}", _proxy_img)
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
