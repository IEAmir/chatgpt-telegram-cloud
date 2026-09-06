"""Telegram bot = ChatGPT (free web) on Telegram.

Text goes to chatgpt-web2api (OpenAI-compatible REST), images to pixel-bridge
(via the engine's HTTP shim). Runs as a Render free web service: webhook for
Telegram + a self-ping keep-alive so the instance stays warm.
"""
import asyncio
import base64
import json
import logging
import os
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message, Update
from aiohttp import web

# ── Config ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", str(OWNER_ID)).split(",") if x.strip()}
ENGINE_URL = os.environ.get("ENGINE_URL", "http://127.0.0.1:8080").rstrip("/")
API_SECRET = os.environ.get("API_SECRET", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hook")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
KEEP_ENGINE_WARM = os.environ.get("KEEP_ENGINE_WARM", "0") == "1"
IMG_PROVIDER = os.environ.get("IMG_PROVIDER", "chatgpt")
SELF_URL = RENDER_EXTERNAL_URL or f"http://127.0.0.1:{PORT}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()

histories: dict[int, list[dict[str, str]]] = {}
models: dict[int, str] = {}
cookie_cache: list | None = None
cookie_cache_name: str | None = None
last_boot_id: str | None = None
awaiting_cookies: set[int] = set()
awaiting_edit_caption: dict[int, tuple[str, str]] = {}  # user_id -> (file_id, file_path)

session: aiohttp.ClientSession | None = None


def engine_headers(extra: dict | None = None) -> dict:
    h = {"x-api-secret": API_SECRET, "x-admin-secret": API_SECRET}
    if API_SECRET:
        h["Authorization"] = f"Bearer {API_SECRET}"
    if extra:
        h.update(extra)
    return h


async def engine_request(method: str, path: str, *, json_body: Any = None,
                         timeout: int = 300, raw: bool = False):
    url = f"{ENGINE_URL}{path}"
    async with session.request(method, url, json=json_body, headers=engine_headers(),
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if raw:
            data = await resp.read()
            return resp.status, data
        try:
            data = await resp.json(content_type=None)
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            data = {"raw": (await resp.text())[:500]}
        return resp.status, data


async def typing_loop(chat_id: int, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass


def allowed(uid: int) -> bool:
    return uid in ALLOWED_IDS or (OWNER_ID and uid == OWNER_ID)


def chunk_text(text: str, size: int = 3900) -> list[str]:
    out = []
    while len(text) > size:
        cut = text.rfind("\n", size - 600, size)
        if cut == -1:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


# ── Commands ──────────────────────────────────────────────────────────────
WELCOME = (
    "سلام! 👋 من چت‌بات تلگرامی‌ات هستم که پشت صحنه از ChatGPT وب (رایگان) استفاده می‌کنه.\n\n"
    "• هر متنی بفرستی جواب می‌گیرم (مکالمه با حافظه)\n"
    "• /img <توضیح> → تولید تصویر\n"
    "• عکس بفرست + توضیح تغییرات در کپشن → ویرایش تصویر\n"
    "• /new → شروع مکالمه جدید\n"
    "• /model → لیست مدل‌ها و انتخاب مدل\n"
    "• /status → وضعیت موتور\n"
    "• /setcookies → به‌روزرسانی کوکی ChatGPT (فقط مالک)\n"
    "• /help → راهنما"
)


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id):
        return
    await msg.answer(WELCOME)


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not allowed(msg.from_user.id):
        return
    await msg.answer(WELCOME)


@dp.message(Command("new"))
async def cmd_new(msg: Message):
    if not allowed(msg.from_user.id):
        return
    histories.pop(msg.from_user.id, None)
    await msg.answer("مکالمه جدید شروع شد ✅ (حافظه پاک شد)")


@dp.message(Command("model"))
async def cmd_model(msg: Message):
    if not allowed(msg.from_user.id):
        return
    arg = (msg.text or "").split(maxsplit=1)
    if len(arg) == 2:
        models[msg.from_user.id] = arg[1].strip()
        await msg.answer(f"مدل روی «{arg[1].strip()}» تنظیم شد ✅")
        return
    status, data = await engine_request("GET", "/v1/models", timeout=30)
    if status != 200:
        await msg.answer(f"خطا در گرفتن لیست مدل‌ها (HTTP {status}). موتور روشنه؟ /status")
        return
    ids = [m.get("id") for m in data.get("data", [])]
    current = models.get(msg.from_user.id, "auto")
    lines = ["مدل‌های فعال روی اکانت ChatGPT:", ""]
    lines += [f"• {m}" + ("  ← فعلی" if m == current else "") for m in ids] or ["(لیست خالی)"]
    lines += ["", "برای تغییر: /model <نام‌مدل>"]
    await msg.answer("\n".join(lines))


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not allowed(msg.from_user.id):
        return
    status, h = await engine_request("GET", "/healthz", timeout=30)
    if status != 200:
        await msg.answer(f"⚠️ موتور در دسترس نیست (HTTP {status}).\n"
                         "اگر مدتی غیرفعال بوده، ۱ تا ۲ دقیقه صبر کن و دوباره /status بزن.")
        return
    w2a = h.get("web2api", {}).get("detail", {})
    lines = [
        f"status: {h.get('ok') and '🟢' or '🟡'}",
        f"web2api: {w2a.get('status', '?')} | cdp_connected: {w2a.get('cdp_connected', '?')}",
        f"chrome_cdp: {'🟢' if h.get('chrome_cdp', {}).get('ok') else '🔴'}",
        f"pixel_bridge: {'🟢' if h.get('pixel_bridge', {}).get('ok') else '🔴'}",
        f"cookies_file: {'بله' if h.get('cookies_file') else 'نمی‌دونم'}",
    ]
    st, img = await engine_request("GET", f"/img/session/{IMG_PROVIDER}", timeout=60)
    if st == 200:
        lines.append(f"نشست تصویر ({IMG_PROVIDER}): {'🟢 لاگین' if img.get('authenticated') else '🔴 لاگین نیست'}"
                     + (f" — {img.get('detail', '')}" if img.get("detail") else ""))
    await msg.answer("\n".join(lines))


@dp.message(Command("setcookies"))
async def cmd_setcookies(msg: Message):
    global cookie_cache, cookie_cache_name
    if not allowed(msg.from_user.id) and msg.from_user.id != OWNER_ID:
        return
    if OWNER_ID and msg.from_user.id != OWNER_ID:
        await msg.answer("فقط مالک ربات اجازه این کار رو داره.")
        return
    awaiting_cookies.add(msg.from_user.id)
    await msg.answer(
        "فایل کوکی رو بفرست (JSON آرایه‌ای، خروجی افزونه Cookie-Editor یا EditThisCookie روی chatgpt.com و در حالت لاگین).\n"
        "می‌تونی متن JSON رو هم مستقیم پیست کنی."
    )


async def handle_cookies_payload(msg: Message, raw_bytes: bytes | None, text: str | None):
    global cookie_cache, cookie_cache_name, last_boot_id
    try:
        data = json.loads(raw_bytes.decode("utf-8") if raw_bytes else text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        await msg.answer(f"JSON نامعتبره: {exc}")
        return
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if not isinstance(data, list) or not data:
        await msg.answer("فرمت درست نیست؛ باید آرایه‌ای از کوکی‌ها باشه.")
        return
    names = {c.get("name", "") for c in data}
    if not any("session-token" in n or "session" in n for n in names):
        await msg.answer("⚠️ به نظر نمی‌رسه کوکی نشست ChatGPT توش باشه (__Secure-next-auth.session-token). "
                         "ادامه می‌دم ولی ممکنه لاگین نشه.")
    status, resp = await engine_request("POST", "/admin/cookies", json_body={"cookies": data}, timeout=180)
    if status != 200:
        await msg.answer(f"❌ موتور کوکی رو نپذیرفت (HTTP {status}): {resp}")
        return
    if resp.get("verified"):
        cookie_cache = data
        cookie_cache_name = f"cookies-{int(time.time())}.json"
        last_boot_id = None  # force re-sync on next health tick
        await msg.answer("✅ کوکی تزریق شد و لاگین ChatGPT تأیید شد!\nحالا می‌تونی چت کنی و /img بزنی.")
    else:
        cookie_cache = data
        cookie_cache_name = f"cookies-{int(time.time())}.json"
        await msg.answer("⚠️ کوکی ذخیره شد ولی تأیید لاگین ناموفق بود.\n"
                         "احتمالاً کوکی منقضی شده یا ChatGPT چالش امنیتی نشون داده. "
                         "چند دقیقه دیگه /status بزن؛ اگر حل نشد کوکی تازه بفرست.\n\n"
                         f"خروجی موتور:\n{resp.get('output', '')[-500:]}")


# ── Text chat ─────────────────────────────────────────────────────────────
async def do_chat(msg: Message, text: str):
    uid = msg.from_user.id
    stop = asyncio.Event()
    t = asyncio.create_task(typing_loop(msg.chat.id, stop))
    try:
        hist = histories.setdefault(uid, [])
        hist.append({"role": "user", "content": text})
        payload = {
            "model": models.get(uid, "auto"),
            "messages": hist[-24:],
        }
        status, data = await engine_request("POST", "/v1/chat/completions", json_body=payload, timeout=300)
        if status != 200:
            err = ""
            if isinstance(data, dict):
                err = str(data.get("error", data))[:400]
            hist.pop()
            await msg.answer(f"⚠️ خطای موتور (HTTP {status}):\n{err or 'بدون جزئیات'}\n\n"
                             "چند لحظه صبر کن و دوباره بفرست. /status هم وضعیت رو نشون می‌ده.")
            return
        content = data["choices"][0]["message"]["content"]
        hist.append({"role": "assistant", "content": content})
        for part in chunk_text(content):
            await msg.answer(part)
    except asyncio.TimeoutError:
        await msg.answer("⏱️ پاسخ بیش از حد طول کشید. ChatGPT وب گاهی کُنده؛ دوباره امتحان کن.")
    except aiohttp.ClientError as exc:
        await msg.answer(f"⚠️ اتصال به موتور برقرار نشد: {exc}\n"
                         "اگر موتور تازه بیدار شده ۱-۲ دقیقه صبر کن.")
    finally:
        stop.set()
        t.cancel()


# ── Images ────────────────────────────────────────────────────────────────
async def generate_and_send(msg: Message, prompt: str):
    status_note = await msg.answer("🎨 دارم تصویر رو می‌سازم… (ممکنه تا چند دقیقه طول بکشه)")
    try:
        status, job = await engine_request(
            "POST", "/img/generate",
            json_body={"prompt": prompt, "provider": IMG_PROVIDER, "wait_seconds": 150},
            timeout=200,
        )
        if status != 200:
            await msg.answer(f"⚠️ خطای موتور تصویر (HTTP {status}): {str(job)[:300]}")
            return
        deadline = time.monotonic() + 540
        while job.get("status") == "running" and time.monotonic() < deadline:
            await asyncio.sleep(8)
            st, job = await engine_request("GET", f"/img/status/{job.get('job_id')}?wait=10", timeout=60)
        if job.get("status") == "completed" and job.get("files"):
            f0 = job["files"][0]
            st, img_bytes = await engine_request("GET", f"/img/file?path={quote(f0['absolute_path'], safe='')}",
                                                 timeout=120, raw=True)
            if st == 200 and img_bytes:
                photo = BufferedInputFile(img_bytes, filename="image.png")
                caption = (prompt[:900] + "…") if len(prompt) > 900 else prompt
                await msg.answer_photo(photo, caption=caption)
            else:
                await msg.answer("تصویر ساخته شد ولی دانلودش ناموفق بود.")
            return
        if job.get("status") == "failed":
            err = job.get("error", "بدون جزئیات")
            await msg.answer(f"❌ تولید تصویر ناموفق بود:\n{err[:800]}\n\n"
                             "اگر پیام رد شدن (refusal) بوده، پرامپت رو عوض کن؛ "
                             "اگر لاگین مشکل داره /status بزن.")
            return
        await msg.answer("⏱️ تولید تصویر خیلی طول کشید. بعداً /status بزن یا دوباره امتحان کن.")
    finally:
        try:
            await status_note.delete()
        except Exception:
            pass


async def edit_and_send(msg: Message, file_id: str, instructions: str):
    note = await msg.answer("🖌️ دارم تصویر رو ویرایش می‌کنم…")
    try:
        f = await bot.get_file(file_id)
        buf = await bot.download_file(f.file_path)
        image_b64 = base64.b64encode(buf.read()).decode()
        status, job = await engine_request(
            "POST", "/img/edit",
            json_body={"image_b64": image_b64, "instructions": instructions,
                       "provider": IMG_PROVIDER, "wait_seconds": 150},
            timeout=200,
        )
        if status != 200:
            await msg.answer(f"⚠️ خطای موتور (HTTP {status}): {str(job)[:300]}")
            return
        deadline = time.monotonic() + 540
        while job.get("status") == "running" and time.monotonic() < deadline:
            await asyncio.sleep(8)
            st, job = await engine_request("GET", f"/img/status/{job.get('job_id')}?wait=10", timeout=60)
        if job.get("status") == "completed" and job.get("files"):
            f0 = job["files"][0]
            st, img_bytes = await engine_request("GET", f"/img/file?path={quote(f0['absolute_path'], safe='')}",
                                                 timeout=120, raw=True)
            if st == 200 and img_bytes:
                await msg.answer_photo(BufferedInputFile(img_bytes, filename="edited.png"),
                                       caption=instructions[:900])
                return
        await msg.answer(f"❌ ویرایش ناموفق بود: {str(job.get('error', job))[:600]}")
    finally:
        try:
            await note.delete()
        except Exception:
            pass


# ── Message router ────────────────────────────────────────────────────────
@dp.message(Command("img"))
async def cmd_img(msg: Message):
    if not allowed(msg.from_user.id):
        return
    prompt = (msg.text or "").split(maxsplit=1)
    if len(prompt) < 2:
        await msg.answer("استفاده: /img <توضیح تصویر>\nمثال: /img یک گربه فضانورد با پس‌زمینه تهران، کیفیت بالا")
        return
    await generate_and_send(msg, prompt[1].strip())


@dp.message(F.photo)
async def on_photo(msg: Message):
    if not allowed(msg.from_user.id):
        return
    uid = msg.from_user.id
    photo = msg.photo[-1]
    caption = (msg.caption or "").strip()
    if caption:
        await edit_and_send(msg, photo.file_id, caption)
        return
    f = await bot.get_file(photo.file_id)
    awaiting_edit_caption[uid] = (photo.file_id, f.file_path)
    await msg.answer("عکس گرفتم. حالا بنویس چه تغییری می‌خوای (همین پیام بعدی به‌عنوان دستور ویرایش استفاده می‌شه).")


@dp.message(Command("cookies_help"))
async def cookies_help(msg: Message):
    if not allowed(msg.from_user.id):
        return
    await msg.answer(
        "راهنمای کوکی:\n"
        "1) روی گوشی/کامپیوترت وارد chatgpt.com شو (لاگین باشی)\n"
        "2) افزونه Cookie-Editor رو نصب کن و روی chatgpt.com بزن Export (فرمت JSON)\n"
        "3) فایل JSON رو همین‌جا بفرست (یا بعد از /setcookies)\n"
        "کوکی‌ها حدوداً هر ۲ هفته منقضی می‌شن و باید دوباره بفرستی."
    )


@dp.message(F.document)
async def on_document(msg: Message):
    if not allowed(msg.from_user.id):
        return
    if msg.from_user.id in awaiting_cookies:
        f = await bot.get_file(msg.document.file_id)
        buf = await bot.download_file(f.file_path)
        awaiting_cookies.discard(msg.from_user.id)
        await handle_cookies_payload(msg, buf.read(), None)
        return
    await msg.answer("فایل گرفتم ولی منتظرش نبودم. اگر کوکی ChatGPT هست، اول /setcookies بزن.")


@dp.message(F.text)
async def on_text(msg: Message):
    if not allowed(msg.from_user.id):
        return
    uid = msg.from_user.id
    text = (msg.text or "").strip()
    if uid in awaiting_cookies:
        awaiting_cookies.discard(uid)
        if text.startswith("/"):
            await msg.answer("بی‌خیال کوکی. ⏭️")
            return
        await handle_cookies_payload(msg, None, text)
        return
    if uid in awaiting_edit_caption:
        file_id, _ = awaiting_edit_caption.pop(uid)
        await edit_and_send(msg, file_id, text)
        return
    if not text:
        return
    await do_chat(msg, text)


# ── Web server (webhook + keep-alive) ─────────────────────────────────────
async def h_index(_):
    return web.json_response({"ok": True, "service": "telegram-bot", "time": int(time.time())})


async def h_warm(_):
    # Self-ping target; also warm the engine when enabled.
    if KEEP_ENGINE_WARM:
        try:
            await engine_request("GET", "/healthz", timeout=25)
        except Exception:
            pass
    return web.json_response({"warmed": True})


async def h_webhook(request: web.Request):
    if request.match_info["secret"] != WEBHOOK_SECRET:
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        # Process asynchronously: long completions must not delay the HTTP 200
        # (Telegram retries webhooks that respond slowly).
        asyncio.create_task(dp.feed_update(bot, update))
    except Exception as exc:
        log.exception("webhook error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True})


async def keep_alive_loop():
    global last_boot_id
    await asyncio.sleep(20)
    while True:
        try:
            async with session.get(f"{SELF_URL}/", timeout=aiohttp.ClientTimeout(total=20)) as r:
                r.status
        except Exception:
            pass
        # Auto-heal: if the engine restarted and we hold cookies, re-inject them.
        if cookie_cache:
            try:
                status, h = await engine_request("GET", "/healthz", timeout=30)
                boot = h.get("boot_id") if isinstance(h, dict) else None
                if status == 200 and boot and boot != last_boot_id:
                    log.info("engine boot_id changed (%s) — re-injecting cookies", boot)
                    st, resp = await engine_request("POST", "/admin/cookies",
                                                    json_body={"cookies": cookie_cache}, timeout=180)
                    log.info("auto cookie inject: %s verified=%s", st,
                             isinstance(resp, dict) and resp.get("verified"))
                    if st == 200:
                        last_boot_id = boot
            except Exception as exc:
                log.warning("auto-heal check failed: %s", exc)
        await asyncio.sleep(600)


async def on_startup():
    global session
    session = aiohttp.ClientSession()
    asyncio.create_task(keep_alive_loop())
    url = f"{RENDER_EXTERNAL_URL}/webhook/{WEBHOOK_SECRET}" if RENDER_EXTERNAL_URL else None
    if url:
        try:
            await bot.set_webhook(url, drop_pending_updates=True, allowed_updates=["message"])
            log.info("webhook set to %s", url)
        except Exception as exc:
            log.error("set_webhook failed: %s", exc)
    else:
        log.warning("RENDER_EXTERNAL_URL missing — webhook not set")
    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, "🤖 ربات بالا اومد و آماده‌ست. /help را بزن.")
        except Exception:
            pass


def main():
    app = web.Application()
    app.router.add_get("/", h_index)
    app.router.add_get("/warm", h_warm)
    app.router.add_post("/webhook/{secret}", h_webhook)
    app.on_startup.append(lambda _: on_startup())
    log.info("starting bot on port %s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
