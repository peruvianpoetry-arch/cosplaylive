import os
import json
import threading
import time
import asyncio
from urllib.parse import urlencode, quote_plus

from flask import Flask, request, redirect, make_response
import stripe

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========= CONFIG =========
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
STRIPE_SK = os.getenv("STRIPE_SECRET_KEY", "").strip()
BASE_URL = os.getenv("BASE_URL", "https://cosplaylive.onrender.com").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")
ANNOUNCE_EVERY_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN", "5"))
ANNOUNCE_TEXT = os.getenv(
    "ANNOUNCE_TEXT",
    "🔥 ¡Apoya el show con un botón! La modelo te dará las gracias en vivo."
)

# Chat/grupo donde anunciaremos (el chat donde usas /liveon)
/* PON tu chat_id aquí si quieres forzarlo; si no, se usa dinámico por chat */
DEFAULT_CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Usuario (id) de la modelo para DM al pagar (opcional)
MODEL_USER_ID = os.getenv("MODEL_USER_ID", "").strip()

# Precios base (puedes cambiarlos aquí y olvidarte de /addprice)
DEFAULT_PRICES = {
    "Titten": 10.00,
    "Muschi": 15.00,
    "Dildo": 20.00,
    "Masturbation": 35.00
}
# =========================

app = Flask(__name__)
stripe.api_key = STRIPE_SK

# Estado en memoria
live_chats = set()                 # chats con live activo
prices = DEFAULT_PRICES.copy()     # dict nombre -> float

# --- Utilidades ---

def price_rows():
    # Devuelve lista de filas de botones (1 por fila)
    rows = []
    for name, amount in prices.items():
        # Armamos URL de pay segura (sin emojis/espacios raros sin codificar)
        pay_qs = urlencode({
            "name": name,
            "amount": f"{amount:.2f}"
        }, quote_via=quote_plus)
        pay_url = f"{BASE_URL}/pay?{pay_qs}"
        rows.append([InlineKeyboardButton(f"{name} · {amount:.2f} {CURRENCY}", url=pay_url)])
    return rows

def prices_menu_text():
    lines = ["🎬 *Menú del show*"]
    for name, amount in prices.items():
        lines.append(f"• {name} — {amount:.2f} {CURRENCY}")
    lines.append("\nPulsa un botón para apoyar al show 🔥")
    return "\n".join(lines)

async def send_announce(app_bot: Application, chat_id: int):
    try:
        await app_bot.bot.send_message(
            chat_id=chat_id,
            text=ANNOUNCE_TEXT
        )
    except Exception:
        pass

# --- Bot Handlers ---

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await update.message.reply_text(f"✅ Eres admin (ID: {update.effective_user.id})")

async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Formato: /addprice Nombre, 12.34
    if not update.message:
        return
    txt = update.message.text or ""
    try:
        body = txt.split(" ", 1)[1]
        name, amount_s = [x.strip() for x in body.split(",", 1)]
        amount = float(amount_s.replace(",", "."))
        prices[name] = amount
        await update.message.reply_text("💰 Precio agregado correctamente.")
    except Exception:
        await update.message.reply_text("Formato incorrecto. Usa: /addprice 🍑 Nombre, 5.00")

async def listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text("💸 Lista de precios:\n" + prices_menu_text(), parse_mode="Markdown")

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # marca chat en vivo + publica menú + agenda anuncios
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return
    live_chats.add(chat_id)

    kb = InlineKeyboardMarkup(price_rows())
    await context.bot.send_message(
        chat_id=chat_id,
        text="Hola 👋 ¡Bienvenidos al show!",
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=prices_menu_text(),
        parse_mode="Markdown",
        reply_markup=kb
    )

    # programa anuncios cada N minutos usando job_queue del Application
    # (usamos application.job_queue para evitar None en context.job_queue)
    application: Application = context.application
    # cancelamos anteriores para este chat y creamos uno nuevo
    for job in application.job_queue.get_jobs_by_name(f"auto_ads_{chat_id}"):
        job.schedule_removal()
    application.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(send_announce(application, chat_id)),
        interval=ANNOUNCE_EVERY_MIN*60,
        name=f"auto_ads_{chat_id}",
        first=ANNOUNCE_EVERY_MIN*60
    )

    await update.message.reply_text("🟢 Live activado.")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return
    if chat_id in live_chats:
        live_chats.remove(chat_id)
    # cancela anuncios
    application: Application = context.application
    for job in application.job_queue.get_jobs_by_name(f"auto_ads_{chat_id}"):
        job.schedule_removal()
    await update.message.reply_text("🔴 Live desactivado.")

# Traducción “ligera”: solo si el chat está en live
from deep_translator import GoogleTranslator

def smart_translate(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    lower = t.lower()
    # detección súper básica
    if any(ch in lower for ch in ["¿", "¡", "qué", "cómo", "estás", "gracias"]):
        src, dest = "es", "de"
    elif any(ch in lower for ch in ["wie", "geht", "danke", "bitte", "und"]):
        src, dest = "de", "es"
    else:
        src, dest = "en", "es"
    try:
        return GoogleTranslator(source=src, target=dest).translate(t)
    except Exception:
        return ""

async def on_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    if chat_id not in live_chats:
        return
    txt = update.message.text or ""
    translated = smart_translate(txt)
    if translated:
        await update.message.reply_text(f"🌐 {translated}")

# --- Flask (pagos) ---

@app.get("/")
def root_ok():
    return "OK", 200

@app.get("/pay")
def pay():
    """
    Genera una sesión de Stripe Checkout y redirige.
    GET /pay?name=Titten&amount=10.00
    """
    if not STRIPE_SK:
        return "Stripe no configurado", 500
    name = request.args.get("name", "").strip()
    amount_s = request.args.get("amount", "0").strip()
    try:
        amount = float(amount_s.replace(",", "."))
    except Exception:
        return "Monto inválido", 400
    if not name or amount <= 0:
        return "Parámetros inválidos", 400

    # Success URL vuelve a /thanks con session_id y nombre
    success_url = f"{BASE_URL}/thanks?item={quote_plus(name)}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{BASE_URL}/cancel"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": CURRENCY.lower(),
                    "product_data": {"name": name},
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
    """
    Verifica la sesión y anuncia en el chat + DM a la modelo.
    """
    session_id = request.args.get("session_id", "")
    item = request.args.get("item", "Apoyo")
    if not session_id:
        return "Falta session_id", 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        return f"Stripe error: {e}", 500

    paid = (session.get("payment_status") == "paid")
    amount_total = (session.get("amount_total") or 0) / 100.0

    # Avisar por Telegram
    try:
        text = f"💥 *Gracias por el apoyo!* {item} — {amount_total:.2f} {CURRENCY}"
        # Usamos DEFAULT_CHAT_ID si está; si no, no sabemos a qué chat — opcionalmente ignora
        if DEFAULT_CHAT_ID:
            asyncio.run(application.bot.send_message(
                chat_id=int(DEFAULT_CHAT_ID),
                text=text,
                parse_mode="Markdown"
            ))
        # DM a la modelo
        if MODEL_USER_ID:
            asyncio.run(application.bot.send_message(
                chat_id=int(MODEL_USER_ID),
                text=f"🔔 Alguien pagó: {item} — {amount_total:.2f} {CURRENCY}"
            ))
    except Exception:
        pass

    html = f"""
    <html><body>
    <h3>OK, {"pago recibido" if paid else "pedido registrado"}.</h3>
    <p>Ítem: {item} — {amount_total:.2f} {CURRENCY}</p>
    <p>Puedes volver al chat de Telegram.</p>
    </body></html>
    """
    resp = make_response(html, 200)
    return resp

@app.get("/cancel")
def cancel():
    return "Pago cancelado. Vuelve a Telegram.", 200

# --- Arranque: bot hilo principal, Flask en thread ---

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN")

    # Iniciar Flask en segundo plano
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    time.sleep(0.5)

    # Bot en hilo principal (evita errores de event loop)
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("addprice", addprice))
    application.add_handler(CommandHandler("listprices", listprices))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_chat))

    # JobQueue ya está listo dentro de application; no usamos context.job_queue
    application.run_polling(close_loop=False)
