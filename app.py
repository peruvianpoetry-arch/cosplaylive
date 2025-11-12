import os
import logging
import threading
from urllib.parse import quote_plus
from time import monotonic

from flask import Flask, request, jsonify, make_response
from deep_translator import GoogleTranslator

import stripe

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters
)

# -------------------- CONFIG --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("cosplaylive")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE = os.getenv("PUBLIC_BASE", os.getenv("BASE_URL", "")).rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")
AUTO_MIN = int(os.getenv("AUTO_INTERVAL_MIN", "10"))
ALLOW_ADULT = os.getenv("ALLOW_ADULT", "1") == "1"

# Admins: "123,456"
_admin_raw = os.getenv("ADMIN_USER_IDS") or os.getenv("ADMIN_USER_ID", "")
ADMIN_IDS = {int(x) for x in [s.strip() for s in _admin_raw.split(",")] if x.strip().isdigit()}

# Traducción
ENABLE_TRANSLATION = (os.getenv("ENABLE_TRANSLATION", "True")).lower() == "true"
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "de")

# Stripe
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if STRIPE_KEY:
    stripe.api_key = STRIPE_KEY

# Canal/sala (opcional)
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHANNEL_ID")

# Estado en memoria (persistente suficiente para Render Starter)
DATA = {
    "prices": {},       # {"Nombre visible": 10}
    "live_chat_id": None,
    "auto_job": None,
    "last_announce": 0.0
}

# -------------------- FLASK --------------------
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.get("/donar")
def donar():
    """
    Recibe amt (euros) e item (nombre visible). Crea checkout de Stripe si hay clave,
    si no, devuelve simulación OK.
    """
    try:
        amt = request.args.get("amt", "").strip()
        item = request.args.get("item", "").strip()
        if not amt or not item:
            return make_response("Parámetros inválidos", 400)

        amount_cents = int(float(amt) * 100)

        # Con Stripe real
        if STRIPE_KEY:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": CURRENCY.lower(),
                        "product_data": {"name": item[:120]},
                        "unit_amount": amount_cents
                    },
                    "quantity": 1
                }],
                success_url=f"{PUBLIC_BASE}/ok",
                cancel_url=f"{PUBLIC_BASE}/cancel"
            )
            # Redirigir al Checkout
            return jsonify({"checkout_url": session.url})

        # Modo demo (sin Stripe)
        return f"OK, simulación de donación recibida. Monto: {amt} {CURRENCY} | Item: {item}"

    except Exception as e:
        log.exception("Error en /donar")
        return make_response(f"Error: {e}", 500)

@app.get("/ok")
def ok():
    return "Pago completado (demo).", 200

@app.get("/cancel")
def cancel():
    return "Pago cancelado.", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    # Flask en hilo secundario
    app.run(host="0.0.0.0", port=port, threaded=True)

# -------------------- TELEGRAM --------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def fmt_menu() -> str:
    if not DATA["prices"]:
        return "Aún no hay opciones. Usa /addprice Nombre · Precio"
    lines = [ "🎬 *Menú del show*"]
    for n, p in DATA["prices"].items():
        lines.append(f"• {n} — {p:.2f} {CURRENCY}")
    lines.append("\nPulsa un botón para apoyar al show 🔥")
    return "\n".join(lines)

def build_keyboard():
    if not PUBLIC_BASE:
        return None
    rows = []
    for name, price in DATA["prices"].items():
        qs = f"amt={price:.2f}&item={quote_plus(name)}"
        url = f"{PUBLIC_BASE}/donar?{qs}"
        rows.append([InlineKeyboardButton(f"{name} · {price:.2f} {CURRENCY}", url=url)])
    return InlineKeyboardMarkup(rows) if rows else None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Hola 👋\nUsa /addprice Nombre · Precio para añadir opciones.\n/livezona o /liveon para mostrar el menú.",
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if is_admin(uid):
        await update.effective_chat.send_message(f"✅ Eres admin (ID: {uid})")
    else:
        await update.effective_chat.send_message("Solo admin.")

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.effective_chat.send_message("Solo admin.")

    txt = (update.message.text or "").strip()
    # formato: /addprice Nombre, 10   ó   /addprice Nombre · 10
    raw = txt.split(" ", 1)
    if len(raw) < 2:
        return await update.effective_chat.send_message("Formato incorrecto. Usa: /addprice 🍑 Nombre 5€")

    payload = raw[1]
    if "·" in payload:
        name, price_s = [s.strip() for s in payload.split("·", 1)]
    elif "," in payload:
        name, price_s = [s.strip() for s in payload.split(",", 1)]
    else:
        parts = payload.rsplit(" ", 1)
        if len(parts) != 2:
            return await update.effective_chat.send_message("Formato incorrecto. Usa: /addprice Nombre 5")
        name, price_s = parts[0].strip(), parts[1].strip()

    price_s = price_s.replace("€", "").replace(",", ".")
    try:
        price = float(price_s)
    except:
        return await update.effective_chat.send_message("Precio inválido.")

    DATA["prices"][name] = price
    await update.effective_chat.send_message("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(fmt_menu(), reply_markup=build_keyboard(), parse_mode="Markdown")

async def auto_banner(context: ContextTypes.DEFAULT_TYPE):
    chat_id = DATA["live_chat_id"]
    if not chat_id:
        return
    # Throttle anti-spam
    now = monotonic()
    if now - DATA["last_announce"] < (AUTO_MIN * 60 * 0.8):
        return
    DATA["last_announce"] = now

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=fmt_menu(),
            reply_markup=build_keyboard(),
            parse_mode="Markdown"
        )
    except Exception:
        log.exception("Error enviando banner automático")

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.effective_chat.send_message("Solo admin.")
    DATA["live_chat_id"] = update.effective_chat.id

    # Enviar el menú una vez
    await update.effective_chat.send_message(fmt_menu(), reply_markup=build_keyboard(), parse_mode="Markdown")

    # Programar anuncios automáticos
    if context.job_queue:
        # Cancelar previo
        if DATA["auto_job"]:
            DATA["auto_job"].schedule_removal()
        DATA["auto_job"] = context.job_queue.run_repeating(
            auto_banner, interval=AUTO_MIN * 60, first=AUTO_MIN * 60, name=f"auto_ads_{update.effective_chat.id}"
        )

    await update.effective_chat.send_message("🔔 Auto-anuncios activados.")

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.effective_chat.send_message("Solo admin.")
    if DATA["auto_job"]:
        DATA["auto_job"].schedule_removal()
        DATA["auto_job"] = None
    await update.effective_chat.send_message("🔕 Auto-anuncios desactivados.")

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignorar eventos sin texto (p.ej., posts de canal)
    if not update.message or not update.message.text:
        return
    if not ENABLE_TRANSLATION:
        return
    # Evitar traducir comandos
    if update.message.text.startswith("/"):
        return
    try:
        txt = update.message.text.strip()
        translated = GoogleTranslator(source="auto", target=TRANSLATE_TO).translate(txt)
        if translated and translated != txt:
            await update.message.reply_text(f"🌐 {translated}")
    except Exception:
        # No reventar el bot si falla el traductor
        log.warning("Fallo traducción", exc_info=True)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error: %s", context.error)

def build_app() -> Application:
    app_tg = Application.builder().token(TOKEN).build()

    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("whoami", cmd_whoami))
    app_tg.add_handler(CommandHandler("addprice", cmd_addprice))
    app_tg.add_handler(CommandHandler("listprices", cmd_listprices))
    app_tg.add_handler(CommandHandler("liveon", cmd_liveon))
    app_tg.add_handler(CommandHandler("liveoff", cmd_liveoff))

    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_in_chat))

    app_tg.add_error_handler(on_error)
    return app_tg

# -------------------- MAIN --------------------
if __name__ == "__main__":
    log.info("Bot iniciando en Render…  ✅ Iniciando servidor Flask y bot…")

    # 1) Lanzar Flask en hilo secundario
    threading.Thread(target=run_flask, daemon=True).start()

    # 2) Bot en HILO PRINCIPAL (requisito PTB 20.x para JobQueue)
    application = build_app()
    application.run_polling(allowed_updates=Update.ALL_TYPES)
