# ChatGPT روی تلگرام — روی پلن رایگان Render

ربات تلگرامی که متن و تصویر را با **اکانت وب رایگان ChatGPT** تولید می‌کند، بدون هیچ API Key.

## معماری

```
Telegram ⇄ [سرویس ۱: telegram-bot] ⇄ [سرویس ۲: chatgpt-engine] ⇄ Chromium(chatgpt.com)
             webhook + keep-alive        ├─ chatgpt-web2api  → متن (OpenAI-compatible /v1)
             (aiogram 3, Docker)         ├─ pixel-bridge-mcp → تصویر (MCP via HTTP shim)
                                         ├─ Xvfb + Chromium (CDP 9222)
                                         └─ router.py (پورت عمومی $PORT)
```

- **chatgpt-web2api** (from [Octo-Lex/ChatGPT-Web2API](https://github.com/Octo-Lex/ChatGPT-Web2API), MIT):
  یک Chrome واقعی را با CDP می‌راند؛ چالش‌های anti-bot خودکار هندل می‌شوند و
  REST سازگار با OpenAI روی `/v1/chat/completions` می‌دهد.
- **pixel-bridge-mcp** (from [Mrshahidali420/pixel-bridge-mcp](https://github.com/Mrshahidali420/pixel-bridge-mcp), MIT):
  تولید/ویرایش تصویر با اکانت وب ChatGPT. اینجا در حالت attach به همان Chromium
  وصل می‌شود و از طریق `mcp-http-bridge.mjs` به REST تبدیل شده.
- هر دو پروژه بدون تغییر کد (vendor شده‌اند)؛ تنها لایه‌های اتصال اضافه شده‌اند.

## متغیرهای محیطی

### chatgpt-engine
| کلید | توضیح |
|---|---|
| `API_SECRET` | کلید مشترک بین ربات و موتور |
| `COOKIES_JSON` | (اختیاری) کوکی‌های chatgpt.com — آرایه JSON یا base64 |
| `W2A_LOG_LEVEL` | INFO/DEBUG |

### chatgpt-telegram-bot
| کلید | توضیح |
|---|---|
| `BOT_TOKEN` | توکن ربات از BotFather |
| `OWNER_ID` / `ALLOWED_IDS` | آیدی عددی کاربران مجاز |
| `ENGINE_URL` | آدرس سرویس موتور |
| `API_SECRET` | همان کلید موتور |
| `WEBHOOK_SECRET` | بخش مخفی URL وب‌هوک |
| `KEEP_ENGINE_WARM` | `1` = موتور همیشه روشن بماند (ساعت بیشتری مصرف می‌شود) |
| `IMG_PROVIDER` | `chatgpt` (پیش‌فرض) یا `gemini` |

## استقرار روی Render (پلن رایگان)

1. **Blueprint**: در Render → New → Blueprint → این ریپو را انتخاب کن؛ `render.yaml`
   هر دو سرویس را می‌سازد. یا دستی دو Web Service با Dockerfile های
   `engine/Dockerfile` و `telegram-bot/Dockerfile` بساز.
2. `BOT_TOKEN` را در سرویس ربات بگذار.
3. بعد از بالا آمدن، در تلگرام به ربات `/setcookies` بزن و فایل کوکی
   chatgpt.com (خروجی افزونه Cookie-Editor) را بفرست.

## کوکی ChatGPT

- روی chatgpt.com لاگین کن → افزونه Cookie-Editor → Export → فایل JSON را به ربات بفرست.
- کوکی‌ها حدود ۲ هفته اعتبار دارند. ربات آخرین کوکی را نگه می‌دارد و بعد از هر
  ری‌استارت موتور خودش دوباره تزریق می‌کند (auto-heal).

## محدودیت‌های پلن رایگان

- ۵۱۲MB رم برای هر سرویس؛ Chromium + Xvfb در مرز کار می‌کند.
- سرویس‌ها بعد از ۱۵ دقیقه بی‌کاری خاموش می‌شوند (first message بعد از idle ~۱-۲ دقیقه).
- `KEEP_ENGINE_WARM=1` موتور را بیدار نگه می‌دارد ولی ساعت رایگان (۷۵۰h/ماه) سریع‌تر مصرف می‌شود.
- Cloudflare ممکن است IP دیتاسنتر را چالش کند؛ در آن صورت پاسخ‌ها خطای challenge می‌دهند.
