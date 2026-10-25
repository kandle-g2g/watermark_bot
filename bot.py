import asyncio
import io
import logging
import os
import sys
import threading
import time

import requests
from flask import Flask
from PIL import Image, ImageOps
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ============================================================
#  НАСТРОЙКИ ВОТЕРМАРКИ — правьте здесь
#  Все значения — доли ШИРИНЫ фото (0.01 = 1% ширины), одинаково для любой ориентации.
# ============================================================
SCALE = 0.15      # размер: ширина вотермарки (0.15 = 15% ширины фото)
MARGIN_X = 0.04   # отступ от ЛЕВОГО края (больше — правее)
MARGIN_Y = -0.02   # отступ от НИЖНЕГО края (меньше — ниже, 0 — вплотную к краю)
OPACITY = 0.7     # прозрачность: 1.0 — непрозрачная, 0.5 — полупрозрачная
JPEG_QUALITY = 95
# ============================================================

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Необязательно: список Telegram ID через запятую. Если пусто — бот отвечает всем.
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}

WATERMARK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watermark.png")

# Логи сразу, без буферизации (иначе print в Render может появиться с задержкой или не появиться)
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # на INFO httpx пишет в лог URL вместе с токеном

# ---------- Flask для healthcheck (как в Covr-боте) ----------
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Bot is running"


@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)


# ---------- Вотермарка ----------
def load_watermark() -> Image.Image:
    return Image.open(WATERMARK_PATH).convert("RGBA")


WATERMARK = load_watermark()  # читаем один раз при старте; нет файла — ошибка сразу в логах


def apply_watermark(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    ImageOps.exif_transpose(img, in_place=True)  # без лишней копии
    if img.mode != "RGB":
        img = img.convert("RGB")

    w = max(1, int(img.width * SCALE))
    h = max(1, int(WATERMARK.height * w / WATERMARK.width))
    wm = WATERMARK.resize((w, h), Image.Resampling.LANCZOS)
    wm.putalpha(wm.getchannel("A").point(lambda p: int(p * OPACITY)))

    x = int(img.width * MARGIN_X)
    y = img.height - h - int(img.width * MARGIN_Y)
    # paste с маской вместо alpha_composite: не создаём полноразмерные слои (экономия памяти на 512 МБ)
    img.paste(wm, (x, y), wm)  # нижний левый угол

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def allowed(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED_IDS or (user is not None and user.id in ALLOWED_IDS)


# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отвечает всем, даже если ID нет в ALLOWED_IDS — это проверка, что бот жив
    user = update.effective_user
    uid = user.id if user else "unknown"
    access = "allowed" if allowed(update) else "NOT allowed (add this ID to ALLOWED_IDS)"
    print(f"📩 /start from {uid}, access: {access}")
    await update.message.reply_text(
        "✅ Bot is running\n"
        f"Your ID: {uid}\n"
        f"Access: {access}\n\n"
        "Send me photos — I'll add the watermark and send them back.\n"
        "Send as a file (without compression) to keep full quality."
    )


async def deny(update: Update):
    user = update.effective_user
    await update.message.reply_text(f"⛔ No access. Your ID: {user.id if user else 'unknown'}")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not allowed(update):
        await deny(update)
        return
    msg = update.message
    print("📩 Photo received")
    try:
        file = await msg.photo[-1].get_file()  # самый большой размер
        data = bytes(await file.download_as_bytearray())
        result = await asyncio.to_thread(apply_watermark, data)
        await msg.reply_photo(photo=io.BytesIO(result), read_timeout=60, write_timeout=60)
        print("✅ Photo processed")
    except Exception as e:
        print(f"🔥 Error in on_photo: {e}")
        await msg.reply_text(f"Error: {e}")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not allowed(update):
        await deny(update)
        return
    msg = update.message
    print("📩 Document received")
    try:
        file = await msg.document.get_file()
        data = bytes(await file.download_as_bytearray())
        result = await asyncio.to_thread(apply_watermark, data)
        out = io.BytesIO(result)
        out.name = "watermarked.jpg"
        await msg.reply_document(document=out, read_timeout=60, write_timeout=60)
        print("✅ Document processed")
    except Exception as e:
        print(f"🔥 Error in on_document: {e}")
        await msg.reply_text(f"Error: {e}")


# ---------- Запуск polling с автоматическим перезапуском ----------
def check_token():
    """Проверяет токен через getMe. В лог не пишем текст исключений requests — в них бывает токен."""
    try:
        data = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10).json()
        if data.get("ok"):
            print(f"✅ Token OK, bot: @{data['result']['username']}")
        else:
            print(f"❌ Telegram rejected the token: {data.get('description')}")
    except Exception as e:
        print(f"❌ getMe failed: {type(e).__name__}")


def run_polling_with_retry():
    check_token()
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        print("✅ Webhook deleted")
    except Exception as e:
        print(f"Failed to delete webhook: {type(e).__name__}")

    while True:
        try:
            # Новый event loop на каждую попытку: после падения старый может быть закрыт
            asyncio.set_event_loop(asyncio.new_event_loop())

            application = Application.builder().token(BOT_TOKEN).build()
            application.add_handler(CommandHandler("start", start))
            application.add_handler(MessageHandler(filters.PHOTO, on_photo))
            application.add_handler(MessageHandler(filters.Document.IMAGE, on_document))

            print("✅ Бот запущен в режиме polling, слушаем сообщения...")
            # drop_pending_updates=False: фото, отправленные пока сервис спал,
            # не теряются и обработаются после пробуждения (Telegram хранит их до 24 часов)
            application.run_polling(drop_pending_updates=False, allowed_updates=["message"])
            # run_polling возвращается штатно только по сигналу остановки (SIGTERM/SIGINT) — выходим
            break
        except Exception as e:
            print(f"❌ Polling упал с ошибкой: {e}. Перезапуск через 10 секунд...")
            time.sleep(10)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_polling_with_retry()
