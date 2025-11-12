import os, json, threading, time
from functools import partial
from urllib.parse import urlencode, quote_plus

from flask import Flask, request, redirect, jsonify
from deep_translator import GoogleTranslator

import stripe

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# -------------------- Config --------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PUBLIC_BASE = os.getenv("PUBLIC_BASE") or os.getenv("BASE_URL") or "https://example.com"
ADMIN_USER_IDS = {s.strip() for s in (os.getenv("ADMIN_USER_IDS") or "").split(",") if s.strip()}
AUTO_INTERVAL_MIN = int(os.getenv("AUTO_INTERVAL_MIN") or 10)
TRANSLATE_TO = (os.getenv("TRANSLATE_TO") or "").strip()  # ej. "de", "en"
ALLOW_ADULT = (os.getenv("ALLOW_ADULT") or "1") not in ("0", "false", "False")

DATA_DIR = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Stripe (opcional)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
stripe.api_key = STRIPE_SECRET_KEY if STRIPE_SECRET_KEY else None
CURRENCY = (os.getenv("CURRENCY") or "EUR").lower()

# -------------------- Estado --------------------
state = {
    "live": False,
    "last_announce_ts": 0.0
}
default_data = {"prices": {}}  # {name: price_float}
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(default_data, f, ensure_ascii=False, indent=2)

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"prices": {}}

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

# -------------------- Util --------------------
def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_USER_IDS if ADMIN_USER_IDS else True  # si no se configuró, cualquiera es admin

def fmt_menu_text() -> str:
    d = load_data()
    if not d["prices"]:
        return "💤 Aún no hay opciones cargadas. Usa /addprice Nombre, 5 para añadir."
    lines = ["🎬 *Menú del show*"]
    for name, price in d["prices"].items():
        lines.append(f"• {name} — *{price:.2f} {CURRENCY.upper()}*")
    lines.append("\nPulsa un botón para apoyar al show 🔥")
    return "\n".join(lines)

def build_keyboard() -> InlineKeyboardMarkup:
    d = load_data()
    rows = []
    for name, price in d["prices"].items():
        # Para DEMO (sin Stripe) llamamos a /donar con parámetros
        if not stripe.api_key:
            url = f"{PUBLIC_BASE}/donar?{urlencode({'amt': price, 'name': name})}"
        else:
            # Para Stripe, pasamos un endpoint que creará la sesión de checkout
            url = f"{PUBLIC_BASE}/pay?{urlencode({'amt': price, 'name': name})}"
        rows.append([InlineKeyboardButton(text=f"{name} · {price:.2f} {CURRENCY.upper()}", url=url)])
    return InlineKeyboardMarkup(rows)

def translate_if_needed(text: str) -> str:
    if not TRANSLATE_TO:
        return text
    try:
        return GoogleTranslator(source="auto", target=TRANSLATE_TO).translate(text)
    except Exception:
        return text

# -------------------- Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Hola 👋\nUsa /addprice Nombre, 5 para añadir opciones.\n/liveon para mostrar el menú.",
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    isadm = is_admin(uid)
    await update.effective_chat.send_message(
        f"{'✅' if isadm else '❌'} Eres admin (ID: {uid})"
    )

def _admin_guard(update: Update) -> bool:
    u = update.effective_user
    return bool(u and is_admin(u.id))

async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _admin_guard(update):
        return await update.effective_chat.send_message("Solo admin.")
    text = (update.message.text if update.message else "").strip()
    # formatos válidos: /addprice Nombre, 5  |  /addprice Nombre; 5
    body = text.split(" ", 1)[-1]
    if "," in body:
        parts = body.split(",", 1)
    elif ";" in body:
        parts = body.split(";", 1)
    else:
        return await update.effective_chat.send_message("Formato incorrecto. Usa: /addprice 🍑 Nombre 5€")
    name = parts[0].strip()
    try:
        price = float(parts[1].replace("€", "").strip().replace(",", "."))
    except ValueError:
        return await update.effective_chat.send_message("Precio inválido.")
    d = load_data()
    d["prices"][name] = price
    save_data(d)
    await update.effective_chat.send_message("💰 Precio agregado correctamente.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    if not d["prices"]:
        return await update.effective_chat.send_message("Sin precios cargados.")
    lines = ["🧾 *Lista de precios:*"]
    for n, p in d["prices"].items():
        lines.append(f"• {n}: {p:.2f} {CURRENCY.upper()}")
    await update.effective_chat.send_message("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN)

async def announce_once(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await context.bot.send_message(
        chat_id=chat_id,
        text=fmt_menu_text(),
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=build_keyboard()
    )

async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _admin_guard(update):
        return await update.effective_chat.send_message("Solo admin.")
    state["live"] = True
    chat_id = update.effective_chat.id
    await update.effective_chat.send_message("🔴 Live ON — el bot está anunciando el menú.")
    # anuncio inmediato
    await announce_once(context, chat_id)
    # job repetitivo
    context.job_queue.run_repeating(
        callback=lambda ctx: ctx.application.create_task(announce_once(ctx, chat_id)),
        interval=AUTO_INTERVAL_MIN * 60,
        name=f"auto_ads_{chat_id}",
        chat_id=chat_id,
        data={"chat_id": chat_id},
        first=AUTO_INTERVAL_MIN * 60
    )

async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _admin_guard(update):
        return await update.effective_chat.send_message("Solo admin.")
    state["live"] = False
    chat_id = update.effective_chat.id
    # cancelar jobs para este chat
    for job in context.job_queue.get_jobs_by_name(f"auto_ads_{chat_id}"):
        job.schedule_removal()
    await update.effective_chat.send_message("⚫ Live OFF — anuncios detenidos.")

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Solo texto de usuarios (evita NoneType en channel posts, stickers, etc.)
    msg = update.message
    if not msg or not msg.text:
        return
    src = msg.text.strip()
    if not src:
        return
    if TRANSLATE_TO:
        translated = translate_if_needed(src)
        if translated and translated != src:
            await msg.reply_text(translated)

# -------------------- Flask (pagos) --------------------
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.get("/donar")
def donar_demo():
    """Modo demo: simula donación y devuelve 200 con el monto y item."""
    amt = float(request.args.get("amt", "0") or 0)
    name = request.args.get("name", "")
    return f"OK, simulación de donación recibida. Monto: {amt:.2f} {CURRENCY.upper()} | Item: {name}", 200

@app.get("/pay")
def pay_checkout():
    """Crea sesión de Stripe Checkout (si hay clave)."""
    if not stripe.api_key:
        # si falta Stripe, redirige al demo
        q = request.query_string.decode("utf-8")
        return redirect(f"{PUBLIC_BASE}/donar?{q}", code=302)

    try:
        amt = float(request.args.get("amt", "0") or 0)
        name = request.args.get("name", "")
        # Stripe trabaja en centavos:
        unit_amount = int(round(amt * 100))

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": CURRENCY,
                    "product_data": {"name": "Apoyo al show"},
                    "unit_amount": unit_amount,
                },
                "quantity": 1,
            }],
            success_url=f"{PUBLIC_BASE}/thanks?{urlencode({'ok':1,'amt':amt})}",
            cancel_url=f"{PUBLIC_BASE}/cancel",
            metadata={"item_name": name}
        )
        return redirect(session.url, code=303)
    except Exception as e:
        return f"Stripe error: {e}", 500

@app.get("/thanks")
def thanks():
    amt = request.args.get("amt", "0")
    return f"✅ Pago recibido. ¡Gracias por tu apoyo! ({amt} {CURRENCY.upper()})", 200

@app.get("/cancel")
def cancel():
    return "Pago cancelado.", 200

# -------------------- Arranque --------------------
def start_flask():
    port = int(os.getenv("PORT") or 10000)
    app.run(host="0.0.0.0", port=port, threaded=True)

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Comandos
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("addprice", cmd_addprice))
    application.add_handler(CommandHandler("listprices", cmd_listprices))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Traducción (solo mensajes de texto que NO son comandos)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate_in_chat))

    # Levantar Flask en hilo aparte
    threading.Thread(target=start_flask, daemon=True).start()

    print("Bot iniciando…")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
