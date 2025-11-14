import os
import threading
from datetime import datetime

from flask import Flask, request, redirect, abort, jsonify

import stripe

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from deep_translator import GoogleTranslator


# ==========================
# CONFIGURACIÓN BÁSICA
# ==========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN en variables de entorno")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
if not STRIPE_SECRET_KEY:
    print("[WARN] STRIPE_SECRET_KEY no está definido. Los pagos no funcionarán.")

stripe.api_key = STRIPE_SECRET_KEY

BASE_URL = os.environ.get("BASE_URL", "https://cosplaylive.onrender.com")

# Flask
app = Flask(__name__)

# Telegram Application
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Traductor (a alemán, desde cualquier idioma)
translator_de = GoogleTranslator(source="auto", target="de")

# ==========================
# PRECIOS DEL SHOW (FIJOS)
# ==========================

PRICES = [
    {"label": "Quick Tip", "amount": 5},
    {"label": "Heart Emoji", "amount": 7},
    {"label": "Nice Pose", "amount": 10},
    {"label": "Dance Move", "amount": 15},
    {"label": "Song Request", "amount": 20},
    {"label": "VIP Shoutout", "amount": 25},
    {"label": "Special Moment", "amount": 35},
]


def build_prices_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for item in PRICES:
        label = item["label"]
        amount = item["amount"]
        # solo el texto del botón, Stripe usará siempre un texto neutro
        text = f"{label} · {amount:.2f} EUR"

        # NO mandar emojis ni caracteres raros a Stripe (solo van al chat)
        from urllib.parse import quote_plus

        safe_label = quote_plus(label)

        url = f"{BASE_URL}/donar?amt={amount:.2f}&label={safe_label}"
        buttons.append([InlineKeyboardButton(text=text, url=url)])

    return InlineKeyboardMarkup(buttons)


def prices_menu_text() -> str:
    lines = ["🎬 Menú del Show"]
    for item in PRICES:
        lines.append(f"• {item['label']} – {item['amount']:.2f} EUR")
    lines.append("")
    lines.append("Pulsa un botón para apoyar el show 🔥")
    return "\n".join(lines)


# ==========================
# ANUNCIOS AUTOMÁTICOS (LIVEON / LIVEOFF)
# ==========================

AUTO_AD_TEXT = "🔥 Unterstütze die Show mit einem Klick! Das Model bedankt sich live."
AUTO_AD_JOB_KEY = "auto_ads_job"


async def announce_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que envía el anuncio automático al chat del show."""
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(chat_id=chat_id, text=AUTO_AD_TEXT)
    except Exception as e:
        print(f"[announce_job] Error enviando anuncio: {e}")


async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Activa anuncios automáticos en ESTE chat.
    Usa /liveon en la sala de chat de la modelo (no en privado).
    """
    chat = update.effective_chat
    if not chat:
        return

    # Solo tiene sentido en grupos / supergrupos
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Bitte benutze /liveon im Gruppenchat der Show, nicht im Privat-Chat mit dem Bot. 🙂"
        )
        return

    # Cancelar job anterior si existía
    existing_job = context.chat_data.get(AUTO_AD_JOB_KEY)
    if existing_job:
        existing_job.schedule_removal()

    # Crear job cada 5 minutos, empezando ahora
    job = context.job_queue.run_repeating(
        announce_job,
        interval=300,  # 5 minutos
        first=0,
        chat_id=chat.id,
        name=f"auto_ads_{chat.id}",
    )
    context.chat_data[AUTO_AD_JOB_KEY] = job

    # Mandar menú + confirmación
    await update.effective_message.reply_text(
        "✅ Automatische Show-Ankündigungen wurden in diesem Chat aktiviert.\n\n"
        + prices_menu_text(),
        reply_markup=build_prices_keyboard(),
    )


async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Desactiva los anuncios automáticos en ESTE chat."""
    chat = update.effective_chat
    if not chat:
        return

    existing_job = context.chat_data.get(AUTO_AD_JOB_KEY)
    if existing_job:
        existing_job.schedule_removal()
        context.chat_data.pop(AUTO_AD_JOB_KEY, None)
        await update.effective_message.reply_text(
            "⛔ Automatische Show-Ankündigungen wurden in diesem Chat deaktiviert."
        )
    else:
        await update.effective_message.reply_text(
            "Hier sind gerade keine automatischen Ankündigungen aktiv."
        )


# ==========================
# COMANDOS BÁSICOS DEL BOT
# ==========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Hallo! Ich bin der Cosplay Live Bot.\n\n"
        "• Benutze /liveon im Gruppenchat der Show, um automatische Ankündigungen zu starten.\n"
        "• Benutze /liveoff, um sie zu stoppen.\n"
        "• Nachrichten werden automatisch ins Deutsche übersetzt, um dem Model zu helfen. 🇩🇪"
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(
        f"✅ Eres admin (ID: {user.id})"
    )


async def cmd_precios(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        prices_menu_text(), reply_markup=build_prices_keyboard()
    )


# ==========================
# TRADUCCIÓN EN EL CHAT
# ==========================

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if msg.from_user and msg.from_user.is_bot:
        return

    text = msg.text or msg.caption
    if not text:
        return

    try:
        translated = translator_de.translate(text)
    except Exception as e:
        print(f"[translate_in_chat] Error traduciendo: {e}")
        return

    # Si por alguna razón la traducción es igual, no respondemos
    if translated.strip().lower() == text.strip().lower():
        return

    await msg.reply_text(f"🌐 {translated}")


# ==========================
# FLASK: RUTAS WEB / STRIPE
# ==========================

@app.route("/")
def index():
    return (
        "<h1>Cosplay Live Bot</h1>"
        "<p>Bot y servidor Flask funcionando.</p>"
    )


@app.route("/donar")
def donar():
    if not STRIPE_SECRET_KEY:
        return "Stripe no está configurado.", 500

    try:
        amount = float(request.args.get("amt", "0").replace(",", "."))
    except ValueError:
        return "Cantidad inválida", 400

    if amount <= 0:
        return "Cantidad inválida", 400

    from urllib.parse import unquote_plus

    label = request.args.get("label", "Support")
    label = unquote_plus(label)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "eur",
                        "unit_amount": int(amount * 100),
                        "product_data": {
                            # Texto neutro en Stripe, sin palabras raras
                            "name": "Chat Support",
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=BASE_URL + "/ok",
            cancel_url=BASE_URL + "/cancel",
            metadata={
                "label": label,
                "amount": f"{amount:.2f}",
                "created_at": datetime.utcnow().isoformat(),
            },
        )
    except Exception as e:
        print(f"[donar] Error creando sesión Stripe: {e}")
        return "Stripe error", 500

    # Redirigir directamente a Stripe
    return redirect(session.url, code=303)


@app.route("/ok")
def ok():
    return "<h2>Danke für deine Unterstützung! 🎉</h2>"


@app.route("/cancel")
def cancel():
    return "<h2>Die Zahlung wurde abgebrochen.</h2>"


# Webhook Stripe (opcional – si no hay secreto, solo responde 200)
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    endpoint_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not endpoint_secret:
        # No configurado -> ignorar pero no romper
        return "", 200

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        print(f"[stripe_webhook] Error verificando firma: {e}")
        return str(e), 400

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {}) or {}
        label = meta.get("label", "Support")
        amount = meta.get("amount", "0.00")

        # Mensaje simple de agradecimiento (en el futuro se puede mejorar)
        text = f"💥 Danke für die Unterstützung! ({label} · {amount} EUR)"

        # Enviamos al chat donde esté configurado el LIVEON
        # Por simplicidad, lo mandamos al admin (puedes cambiar después)
        admin_id_str = os.environ.get("ADMIN_CHAT_ID")
        if admin_id_str:
            try:
                admin_id = int(admin_id_str)
                # usamos create_task para no bloquear Flask
                application.create_task(
                    application.bot.send_message(chat_id=admin_id, text=text)
                )
            except Exception as e:
                print(f"[stripe_webhook] Error enviando mensaje a Telegram: {e}")

    return "", 200


# ==========================
# REGISTRO DE HANDLERS
# ==========================

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("whoami", cmd_whoami))
application.add_handler(CommandHandler("precios", cmd_precios))
application.add_handler(CommandHandler("liveon", cmd_liveon))
application.add_handler(CommandHandler("liveoff", cmd_liveoff))

# Traducción para todos los mensajes de texto en grupos donde esté el bot
application.add_handler(
    MessageHandler(filters.TEXT & (~filters.COMMAND), translate_in_chat)
)


# ==========================
# ARRANQUE BOT + FLASK
# ==========================

def start_bot():
    # stop_signals=None porque corremos en un hilo secundario
    application.run_polling(drop_pending_updates=True, stop_signals=None)


bot_thread = threading.Thread(target=start_bot, name="tg-bot", daemon=True)
bot_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
