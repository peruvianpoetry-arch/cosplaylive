import os, json, threading, time, asyncio
from pathlib import Path
from urllib.parse import urlencode, quote_plus

from flask import Flask, request, redirect, make_response
import stripe

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from deep_translator import GoogleTranslator

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
STRIPE_SK = os.getenv("STRIPE_SECRET_KEY", "").strip()
BASE_URL = os.getenv("BASE_URL", "https://cosplaylive.onrender.com").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")
ANNOUNCE_EVERY_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN", "5"))

# Mensajes en alemán
WELCOME_TEXT_DE = "Hallo 👋 Willkommen zur Show!"
ANNOUNCE_TEXT_DE = "🔥 Unterstütze die Show mit einem Klick! Das Model bedankt sich live."
MENU_TITLE_DE = "🎬 *Show-Menü*"
MENU_HELP_DE = "\nDrücke einen Button, um die Show zu unterstützen 🔥"

# Producto genérico para Stripe (no se envían palabras explícitas)
STRIPE_PRODUCT_NAME = "Chat-Unterstützung"

# Ruta de persistencia
DATA_PATH = Path("data.json")
# ====================

app = Flask(__name__)
stripe.api_key = STRIPE_SK

# ------- Estado & persistencia -------
_state = {
    "chat_id": None,           # chat para anuncios/pagos
    "model_user_id": None,     # opcional, DM a la modelo
    "prices": {                # 7 precios iniciales (editar libremente)
        "Cute Smile": 5.00,
        "Heart Emoji": 7.00,
        "Nice Pose": 10.00,
        "Dance Move": 15.00,
        "Song Request": 20.00,
        "VIP Shoutout": 25.00,
        "Special Moment": 35.00
    },
    "live_chats": []           # lista de chats con live activo
}

def load_state():
    if DATA_PATH.exists():
        try:
            _state.update(json.loads(DATA_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass

def save_state():
    try:
        DATA_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

load_state()

# acceso rápido
def get_prices():
    return _state["prices"]

def set_price(name: str, amount: float):
    _state["prices"][name] = amount
    save_state()

def get_live_chats():
    return set(_state.get("live_chats") or [])

def set_live(chat_id: int, on: bool):
    s = get_live_chats()
    if on: s.add(chat_id)
    else: s.discard(chat_id)
    _state["live_chats"] = list(s)
    save_state()

# --- Utilidades bot ---
def price_rows():
    rows = []
    for name, amount in get_prices().items():
        pay_qs = urlencode({"name": name, "amount": f"{amount:.2f}"}, quote_via=quote_plus)
        pay_url = f"{BASE_URL}/pay?{pay_qs}"
        rows.append([InlineKeyboardButton(f"{name} · {amount:.2f} {CURRENCY}", url=pay_url)])
    return rows

def prices_menu_text_de():
    lines = [MENU_TITLE_DE]
    for name, amount in get_prices().items():
        lines.append(f"• {name} — {amount:.2f} {CURRENCY}")
    lines.append(MENU_HELP_DE)
    return "\n".join(lines)

def translate_to_de(text: str) -> str:
    t = (text or "").strip()
    if not t: return ""
    # Heurística mínima: si parece alemán, no traducir
    lower = t.lower()
    german_markers = ["der", "die", "das", "ich", "du", "danke", "bitte", "wie", "geht", "und", "oder"]
    if any(m in lower for m in german_markers):
        return ""
    try:
        return GoogleTranslator(source="auto", target="de").translate(t)
    except Exception:
        return ""

async def send_announce(app_bot: Application, chat_id: int):
    try:
        await app_bot.bot.send_message(chat_id=chat_id, text=ANNOUNCE_TEXT_DE)
    except Exception:
        pass

# --- Handlers ---
async def bindhere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fija el chat actual como chat oficial de la sala (para anuncios y pagos)."""
    if not update.effective_chat:
        return
    _state["chat_id"] = update.effective_chat.id
    save_state()
    if update.message:
        await update.message.reply_text(f"✅ Chat fijado: {update.effective_chat.id}")

async def setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmodel 123456789  → para DM a la modelo cuando paguen"""
    if not update.message:
        return
    try:
        uid = int((update.message.text or "").split()[1])
        _state["model_user_id"] = uid
        save_state()
        await update.message.reply_text("✅ MODEL_USER_ID actualizado.")
    except Exception:
        await update.message.reply_text("Formato: /setmodel 123456789")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.message:
        await update.message.reply_text(f"✅ Tu ID: {update.effective_user.id}")

async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /addprice Nombre, 12.34
    if not update.message:
        return
    txt = update.message.text or ""
    try:
        body = txt.split(" ", 1)[1]
        name, amount_s = [x.strip() for x in body.split(",", 1)]
        amount = float(amount_s.replace(",", "."))
        set_price(name, amount)
        await update.message.reply_text("💰 Precio agregado.")
    except Exception:
        await update.message.reply_text("Formato incorrecto. Usa: /addprice Nombre, 12.34")

async def listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text("💸 Precios:\n" + prices_menu_text_de(), parse_mode="Markdown")

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    # Si hay chat fijado, usamos ese (anuncios van allí)
    target = _state["chat_id"] or chat_id
    set_live(target, True)

    kb = InlineKeyboardMarkup(price_rows())
    await context.bot.send_message(chat_id=target, text=WELCOME_TEXT_DE)
    await context.bot.send_message(
        chat_id=target, text=prices_menu_text_de(), parse_mode="Markdown", reply_markup=kb
    )

    application: Application = context.application
    # limpia cualquier job previo
    for job in application.job_queue.get_jobs_by_name(f"auto_ads_{target}"):
        job.schedule_removal()

    application.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(send_announce(application, target)),
        interval=ANNOUNCE_EVERY_MIN * 60,
        first=ANNOUNCE_EVERY_MIN * 60,
        name=f"auto_ads_{target}",
    )

    if update.message and (chat_id != target):
        await update.message.reply_text("🟢 Live activado en la sala fijada.")

async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = _state["chat_id"] or (update.effective_chat.id if update.effective_chat else None)
    if not chat_id:
        return
    set_live(chat_id, False)
    application: Application = context.application
    for job in application.job_queue.get_jobs_by_name(f"auto_ads_{chat_id}"):
        job.schedule_removal()
    if update.message:
        await update.message.reply_text("🔴 Live desactiviert.")

async def on_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Traduce a alemán durante el live para ayudar a la modelo."""
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    # Solo traducir si el chat está live (sala oficial o el mismo chat)
    target = _state["chat_id"] or chat_id
    if target not in get_live_chats():
        return
    txt = update.message.text or ""
    translated = translate_to_de(txt)
    if translated:
        await update.message.reply_text(f"🌐 {translated}")

# --- Flask (pagos) ---
@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/pay")
def pay():
    # GET /pay?name=XYZ&amount=10.00  → redirige a Stripe Checkout
    if not STRIPE_SK:
        return "Stripe nicht konfiguriert", 500
    name = request.args.get("name", "").strip() or "Support"
    amount_s = request.args.get("amount", "0").strip()
    try:
        amount = float(amount_s.replace(",", "."))
    except Exception:
        return "Ungültiger Betrag", 400
    if amount <= 0:
        return "Ungültige Parameter", 400

    success_url = f"{BASE_URL}/thanks?label={quote_plus(name)}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url  = f"{BASE_URL}/cancel"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": CURRENCY.lower(),
                    "product_data": {"name": STRIPE_PRODUCT_NAME},  # <— nombre neutro
                    "unit_amount": int(round(amount * 100))
                },
                "quantity": 1
            }],
            success_url=success_url,
            cancel_url=cancel_url
        )
    except Exception as e:
        return f"Stripe error: {e}", 500

    return redirect(session.url, code=303)

@app.get("/thanks")
def thanks():
    # Verifica sesión y anuncia en Telegram
    session_id = request.args.get("session_id", "")
    label = request.args.get("label", "Support")
    if not session_id:
        return "session_id fehlt", 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        return f"Stripe error: {e}", 500

    amount_total = (session.get("amount_total") or 0) / 100.0

    # Anuncia en sala y DM a la modelo
    try:
        if tg_app and _state.get("chat_id"):
            text = f"💥 *Danke für die Unterstützung!* {label} — {amount_total:.2f} {CURRENCY}"
            asyncio.run(tg_app.bot.send_message(
                chat_id=int(_state["chat_id"]), text=text, parse_mode="Markdown"
            ))
        if tg_app and _state.get("model_user_id"):
            asyncio.run(tg_app.bot.send_message(
                chat_id=int(_state["model_user_id"]),
                text=f"🔔 Zahlung eingegangen: {label} — {amount_total:.2f} {CURRENCY}"
            ))
    except Exception:
        pass

    html = f"""
    <html><body>
      <h3>OK. Zahlung bestätigt.</h3>
      <p>Artikel: {label} — {amount_total:.2f} {CURRENCY}</p>
      <p>Du kannst jetzt zum Telegram-Chat zurückkehren.</p>
    </body></html>"""
    return make_response(html, 200)

@app.get("/cancel")
def cancel():
    return "Zahlung abgebrochen. Zurück zu Telegram.", 200

# --- Arranque ---
def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)

tg_app: Application | None = None

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN")

    # Flask en thread
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(0.5)

    # Bot en hilo principal
    tg_app = Application.builder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("bindhere", bindhere))
    tg_app.add_handler(CommandHandler("setmodel", setmodel))
    tg_app.add_handler(CommandHandler("whoami", whoami))
    tg_app.add_handler(CommandHandler("addprice", addprice))
    tg_app.add_handler(CommandHandler("listprices", listprices))
    tg_app.add_handler(CommandHandler("liveon", liveon))
    tg_app.add_handler(CommandHandler("liveoff", liveoff))
    tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_chat))

    tg_app.run_polling(close_loop=False)
