import asyncio
import io
import os

from PIL import Image, ImageOps
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]
# Необязательно: ограничение по user id, через запятую
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}

WATERMARK_PATH = "watermark.png"
SCALE = 0.15    # ширина вотермарки = 15% ширины фото
MARGIN = 0.02   # отступ от края = 2% ширины фото
OPACITY = 0.7   # прозрачность (1.0 = непрозрачная)


def apply_watermark(data: bytes) -> bytes:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
    wm = Image.open(WATERMARK_PATH).convert("RGBA")

    w = int(img.width * SCALE)
    h = int(wm.height * w / wm.width)
    wm = wm.resize((w, h), Image.LANCZOS)

    alpha = wm.getchannel("A").point(lambda p: int(p * OPACITY))
    wm.putalpha(alpha)

    m = int(img.width * MARGIN)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(wm, (img.width - w - m, img.height - h - m))

    out = Image.alpha_composite(img, layer).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_user.id in ALLOWED


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = update.message
    file = await msg.photo[-1].get_file()  # самый большой размер
    data = bytes(await file.download_as_bytearray())
    result = await asyncio.to_thread(apply_watermark, data)
    await msg.reply_photo(photo=io.BytesIO(result))


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = update.message
    file = await msg.document.get_file()
    data = bytes(await file.download_as_bytearray())
    result = await asyncio.to_thread(apply_watermark, data)
    await msg.reply_document(document=io.BytesIO(result), filename="watermarked.jpg")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    app.run_polling()


if __name__ == "__main__":
    main()