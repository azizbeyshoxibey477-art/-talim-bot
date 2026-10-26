"""Ta'lim assistenti: Claude API asosidagi Telegram bot."""

import base64
import logging
import os

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

MODEL = "claude-opus-5-5"
# Har bir suhbatda saqlanadigan xabarlar soni (foydalanuvchi + assistent).
MAX_HISTORY = 30
TELEGRAM_LIMIT = 4000

SYSTEM_PROMPT = """Sen "Ustoz" ismli ta'lim assistentisan. Telegram orqali o'quvchilar va talabalarga yordam berasan.

Vazifalaring:
- Har qanday fan bo'yicha mavzularni sodda, tushunarli tilda, misollar bilan tushuntirish (matematika, fizika, kimyo, biologiya, tarix, ona tili, ingliz tili, informatika va boshqalar).
- Uy vazifalarida yordam berish. Avval o'quvchining o'zi o'ylashiga yordam beradigan yo'naltiruvchi savol yoki maslahat ber; o'quvchi to'liq yechimni so'rasa yoki qiynalib qolsa, yechimni bosqichma-bosqich ko'rsat.
- Test va mashqlar tuzish, o'quvchining javoblarini tekshirib, xatolarini tushuntirish.
- Rasmda yuborilgan masala yoki daftar sahifasini o'qib, tahlil qilish.

Uslub:
- O'quvchi qaysi tilda yozsa, o'sha tilda javob ber (odatda o'zbek tilida, lotin yozuvida).
- Javoblar ixcham va aniq bo'lsin; keraksiz uzun kirish so'zlarisiz.
- Javob Telegram'da oddiy matn sifatida ko'rsatiladi: ** , # , jadval kabi Markdown belgilaridan foydalanma. Ro'yxatlar uchun "1.", "2." yoki "-" ishlat. Formulalarni oddiy yoz (masalan: x^2 + 3x = 0, sqrt(16) = 4).
- Rag'batlantiruvchi va sabrli bo'l, xato qilganni kamsitma.
- Bilmasang yoki ishonching komil bo'lmasa, buni ochiq ayt."""

HELP_TEXT = """Men Ustoz — ta'lim assistentiman. Nimalar qila olaman:

- Istalgan mavzuni tushuntiraman — shunchaki savol yozing.
- Masala yoki daftar sahifasining rasmini yuboring — tahlil qilib beraman.
- /test <mavzu> — shu mavzuda test tuzaman, javoblaringizni tekshiraman.
  Masalan: /test kvadrat tenglamalar
- /yangi — suhbatni yangidan boshlash.
- /yordam — shu yordam matni."""

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
for noisy in ("httpx", "httpx2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("talim-bot")

client = anthropic.AsyncAnthropic()


def _history(context: ContextTypes.DEFAULT_TYPE) -> list:
    return context.chat_data.setdefault("history", [])


def _trim(history: list) -> None:
    """Tarixni MAX_HISTORY ichida saqlaydi; birinchi xabar doim 'user' bo'ladi."""
    if len(history) <= MAX_HISTORY:
        return
    del history[: len(history) - MAX_HISTORY]
    while history and history[0]["role"] != "user":
        history.pop(0)


async def ask_claude(history: list) -> str:
    response = await client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=history,
        output_config={"effort": "medium"},
        cache_control={"type": "ephemeral"},
        # Xavfsizlik filtri so'rovni rad etsa, Anthropic tavsiya qilgan zaxira modelda qayta urinadi.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return "Kechirasiz, bu so'rovga javob bera olmayman. Savolni boshqacha ifodalab ko'ring."
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        text += "\n\n(Javob juda uzun bo'lib ketdi — davomini so'rang.)"
    return text or "Javob bo'sh qaytdi, iltimos, qaytadan urinib ko'ring."


async def _send_long(update: Update, text: str) -> None:
    while text:
        if len(text) <= TELEGRAM_LIMIT:
            chunk, text = text, ""
        else:
            cut = text.rfind("\n", 0, TELEGRAM_LIMIT)
            if cut <= 0:
                cut = TELEGRAM_LIMIT
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        await update.message.reply_text(chunk)


async def _respond(update: Update, context: ContextTypes.DEFAULT_TYPE, content) -> None:
    history = _history(context)
    history.append({"role": "user", "content": content})
    _trim(history)

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        answer = await ask_claude(history)
    except anthropic.RateLimitError:
        history.pop()
        await update.message.reply_text("Hozir so'rovlar ko'p. Bir daqiqadan so'ng qayta urinib ko'ring.")
        return
    except anthropic.BadRequestError as e:
        log.error("Bad request: %s", e.message)
        history.pop()
        await update.message.reply_text("So'rovni qayta ishlab bo'lmadi. /yangi bilan suhbatni yangilab ko'ring.")
        return
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.error("Claude API xatosi: %s", e)
        history.pop()
        await update.message.reply_text("Texnik nosozlik yuz berdi. Birozdan so'ng qayta urinib ko'ring.")
        return

    history.append({"role": "assistant", "content": answer})
    await _send_long(update, answer)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or ""
    await update.message.reply_text(f"Assalomu alaykum, {name}! 👋\n\n{HELP_TEXT}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["history"] = []
    await update.message.reply_text("Suhbat yangilandi. Qaysi mavzudan boshlaymiz?")


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text("Mavzuni yozing. Masalan: /test Amir Temur davlati")
        return
    prompt = (
        f"\"{topic}\" mavzusida 5 ta savoldan iborat test tuz. Har bir savolda A, B, C, D variantlari bo'lsin. "
        "To'g'ri javoblarni hozir ko'rsatma. Oxirida javoblarimni \"1-A, 2-C, ...\" ko'rinishida yuborishimni so'ra, "
        "keyin tekshirib, har bir xatoni qisqa tushuntirib berasan."
    )
    await _respond(update, context, prompt)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _respond(update, context, update.message.text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = await update.message.photo[-1].get_file()
    data = base64.standard_b64encode(bytes(await photo.download_as_bytearray())).decode()
    caption = update.message.caption or "Rasmdagi masala yoki topshiriqni tahlil qilib, yechishda yordam ber."
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": caption},
    ]
    await _respond(update, context, content)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["yordam", "help"], help_cmd))
    app.add_handler(CommandHandler(["yangi", "new"], new_chat))
    app.add_handler(CommandHandler("test", quiz))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot ishga tushdi.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
