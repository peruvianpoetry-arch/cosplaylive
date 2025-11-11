import os, json, threading, asyncio, logging
from flask import Flask, request, redirect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- ENV ----------
TOKEN    = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID  = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "https://cosplaylive.onrender.com").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")
PORT     = int(os.getenv("PORT", "10000"))
AUTO_MIN = int(os.getenv("AUTO_INTERVAL_MIN", "10"))
LANG_TO  = (os.getenv("TRANSLATE_TO", "de") or "de").strip().lower()
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()

DATA_FILE = "/var/data/data.json"

# ---------- LOG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cosplaylive")

# ---------- PERSISTENCIA ----------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"prices": {}, "live": False}

def save_data(d):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

data = load_data()
if not isinstance(data.get("prices", {}), dict):
    data["prices"] = {}
if "live" not in data:
    data["live"] = False

# ---------- TRADUCCIÓN ----------
def tr(txt: str, to: str = LANG_TO) -> str:
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target=to).translate(txt)
    except Exception as e:
        log.warning(f"translate fail: {e}")
        return txt

# ---------- FLASK ----------
app = Flask(__name__)

@app.get("/")
def health():
    return "✅ CosplayLive online"

@app.get("/donar")
def donar():
    amt = (request.args.get("amt", "0") or "0").replace(",", ".")
    item = request.args.get("item", "") or "Apoyo"
    try:
        val = float(amt)
    except Exception:
        return "Monto inválido", 400

    # Stripe si hay clave, si no simulación
    if STRIPE_KEY:
        import stripe
        stripe.api_key = STRIPE_KEY
        cents = int(round(val * 100))
        success = f"{BASE_URL}/ok?amt={cents/100:.2f}&item={item}"
        cancel  = f"{BASE_URL}/cancel"
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": (CURRENCY or "eur").lower(),
                    "product_data": {"name": item},
                    "unit_amount": cents,
                },
                "quantity": 1
            }],
            success_url=success,
            cancel_url=cancel
        )
        return redirect(session.url, code=303)
    else:
        return f"OK simulación: {val:.2f} {CURRENCY} | Item: {item}"

@app.get("/ok")
def ok():
    amt  = request.args.get("amt", "0")
    item = request.args.get("item", "")
    return f"✅ Pago recibido: {amt} {CURRENCY} | {item}"

@app.get("/cancel")
def cancel():
    return "❎ Pago cancelado"

# ---------- UTIL ----------
def only_emojis(s: str) -> str:
    # usa los caracteres no ASCII como “emoticon pista”
    e = "".join(ch for ch in s if ord(ch) > 1000)
    return e if e else "🔥"

def prices_keyboard():
    if not data["prices"]:
        return None
    rows = []
    for name, price in data["prices"].items():
        emo = only_emojis(name)
        url = f"{BASE_URL}/donar?amt={price}&item={emo}"
        rows.append([InlineKeyboardButton(f"{name} — {price}€", url=url)])
    return InlineKeyboardMarkup(rows)

async def announcer(appctx: ContextTypes.DEFAULT_TYPE):
    """Anuncia precios periódicamente mientras live=True."""
    if CHAT_ID == 0:
        return
    while True:
        try:
            if data.get("live") and data["prices"]:
                text = (
                    "🎥 *EN VIVO*\n"
                    "Apoya y elige una acción:\n" +
                    "\n".join([f"• {k} — *{v}€*" for k, v in data["prices"].items()])
                )
                await appctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=prices_keyboard()
                )
        except Exception as e:
            log.error(f"announce error: {e}")
        # nunca menos de 60 s
        await asyncio.sleep(max(60, AUTO_MIN * 60))

# ---------- HANDLERS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola\n"
        "• /addprice 🍑 Nombre 5  → agrega opción\n"
        "• /listprices            → lista\n"
        "• /liveon                → activa en vivo + anuncios\n"
        "• /liveoff               → apaga en vivo\n"
        "• /whoami                → ver si eres admin\n"
        "• /setlang de|en|es      → idioma de traducción\n"
        "• /setinterval 10        → minutos entre anuncios"
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        await update.message.reply_text(f"✅ Eres admin (ID: {uid})")
    else:
        await update.message.reply_text(f"❌ No admin (ID: {uid})")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin.")
        return
    # admite: "/addprice 🍑 Titten 5" o "/addprice Muschi, 10€"
    try:
        raw = update.message.text.split(" ", 1)[1]
    except Exception:
        await update.message.reply_text("Formato: /addprice 🍑 Nombre 5")
        return
    raw = raw.replace(",", " ").replace("€", " ").strip()
    parts = raw.rsplit(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Formato: /addprice 🍑 Nombre 5")
        return
    name = parts[0].strip()
    try:
        price = float(parts[1])
    except Exception:
        await update.message.reply_text("Precio inválido")
        return

    data["prices"][name] = price
    save_data(data)
    await update.message.reply_text(f"💰 Agregado: {name} → {price}€")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not data["prices"]:
        await update.message.reply_text("No hay precios.")
        return
    msg = "💸 *Lista de precios*\n" + "\n".join([f"• {k} — *{v}€*" for k, v in data["prices"].items()])
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data["live"] = True
    save_data(data)
    if not data["prices"]:
        await update.message.reply_text("Agrega precios con /addprice.")
        return
    await update.message.reply_text(
        "🎬 *Show en vivo* — elige tu opción:",
        parse_mode="Markdown",
        reply_markup=prices_keyboard()
    )

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data["live"] = False
    save_data(data)
    await update.message.reply_text("🛑 Live desactivado.")

async def cmd_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin.")
        return
    global LANG_TO
    try:
        LANG_TO = update.message.text.split(" ", 1)[1].strip().lower()
    except Exception:
        pass
    await update.message.reply_text(f"🌐 Idioma de traducción: {LANG_TO}")

async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Solo admin.")
        return
    global AUTO_MIN
    try:
        AUTO_MIN = int(update.message.text.split(" ", 1)[1].strip())
    except Exception:
        await update.message.reply_text("Ejemplo: /setinterval 10")
        return
    await update.message.reply_text(f"⏱️ Intervalo anuncios: {AUTO_MIN} min")

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # solo traduce en el chat público configurado
    if update.effective_chat.id != CHAT_ID:
        return
    txt = update.message.text or ""
    if not txt.strip():
        return
    translated = tr(txt, LANG_TO)
    flag = "🇩🇪" if LANG_TO == "de" else "🌐"
    try:
        await update.message.reply_text(f"{flag} {translated}")
    except Exception as e:
        log.warning(f"reply fail: {e}")

# ---------- TELEGRAM APP ----------
if not TOKEN:
    raise SystemExit("Falta TELEGRAM_TOKEN")

application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("whoami", cmd_whoami))
application.add_handler(CommandHandler("addprice", cmd_addprice))
application.add_handler(CommandHandler("listprices", cmd_listprices))
application.add_handler(CommandHandler("liveon", cmd_liveon))
application.add_handler(CommandHandler("liveoff", cmd_liveoff))
application.add_handler(CommandHandler("setlang", cmd_setlang))
application.add_handler(CommandHandler("setinterval", cmd_setinterval))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_in_chat))
application.add_handler(CallbackQueryHandler(lambda *_: None, pattern=r"^noop$"))

# ---------- ARRANQUE SEGURO (sin before_first_request) ----------
_bot_started = False
def start_bot_once():
    global _bot_started
    if _bot_started:
        return
    _bot_started = True

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # anunciador periódico
        loop.create_task(announcer(application))
        loop.run_until_complete(application.run_polling(stop_signals=None))

    threading.Thread(target=_runner, name="tg-bot", daemon=True).start()

# Iniciar bot y luego levantar Flask
if __name__ == "__main__":
    log.info("Bot+Flask iniciando…")
    start_bot_once()
    app.run(host="0.0.0.0", port=PORT, debug=False)
