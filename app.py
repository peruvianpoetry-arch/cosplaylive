# -*- coding: utf-8 -*-
"""
Cosplay Live Bot – versión estable
- Telegram + Stripe + anuncios automáticos + traducción básica
"""

import os
import threading
import logging
from typing import List, Dict

from flask import Flask, request, redirect, jsonify

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
import stripe
import asyncio

# -------------------------------------------------------------------
# CONFIGURACIÓN BÁSICA
# -------------------------------------------------------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_CURRENCY = "eur"

# URL pública de Render (ajústala si tienes otra)
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://cosplaylive.onrender.com"
)

# Intervalo de anuncios automáticos (segundos) – 300 = 5 min
AUTO_AD_INTERVAL = 300

# Mensaje de anuncio automático
AUTO_AD_TEXT = "🔥 Unterstütze die Show mit einem Klick!\nDas Model bedankt sich live."

# Precios fijos (puedes cambiar los nombres y montos)
PRICES: List[Dict] = [
    {"label": "Cute Emoji",      "amount": 5},
    {"label": "Heart Emoji",     "amount": 7},
    {"label": "Nice Pose",       "amount": 10},
    {"label": "Dance Move",      "amount": 15},
    {"label": "Song Request",    "amount": 20},
    {"label": "VIP Shoutout",    "amount": 25},
    {"label": "Special Moment",  "amount": 35},
]

# Flask
flask_app = Flask(__name__)

# Application de telegram (se asigna en start_bot)
tg_app: Application | None = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# BLOQUE DE ANUNCIOS AUTOMÁTICOS (LIVEON / LIVEOFF)
# -------------------------------------------------------------------

async def announce_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que envía el anuncio automático al chat del show."""
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(chat_id=chat_id, text=AUTO_AD_TEXT)
    except Exception as e:
        logger.error(f"[announce_job] Error enviando anuncio: {e}")


def _job_name_for_chat(chat_id: int) -> str:
    return f"auto_ads_{chat_id}"


async def cmd_liveon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Activa anuncios automáticos en ESTE chat.
    Solo se escribe /liveon en la sala de chat de la modelo.
    """
    if not update.effective_chat:
        return

    chat = update.effective_chat
    chat_id = chat.id

    # Eliminar jobs viejos, si hubiera
    for job in context.job_queue.get_jobs_by_name(_job_name_for_chat(chat_id)):
        job.schedule_removal()

    # Crear nuevo job
    context.job_queue.run_repeating(
        announce_job,
        interval=AUTO_AD_INTERVAL,
        first=0,
        chat_id=chat_id,
        name=_job_name_for_chat(chat_id),
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            "🔔 Automatische Show-Hinweise wurden in diesem Chat AKTIVIERT."
        )


async def cmd_liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Desactiva los anuncios automáticos en este chat."""
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    jobs = context.job_queue.get_jobs_by_name(_job_name_for_chat(chat_id))
    for job in jobs:
        job.schedule_removal()

    if update.effective_message:
        await update.effective_message.reply_text(
            "🔕 Automatische Show-Hinweise wurden in diesem Chat DEAKTIVIERT."
        )

# -------------------------------------------------------------------
# MENÚ DE PRECIOS Y BOTONES DE STRIPE
# -------------------------------------------------------------------

def build_menu_text() -> str:
    lines = ["🎬 *Menü der Show*"]
    for p in PRICES:
        lines.append(f"• {p['label']} – {p['amount']:.2f} EUR")
    lines.append("")
    lines.append("Drücke einen Button, um die Show zu unterstützen 🔥")
    return "\n".join(lines)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el menú del show con los botones de pago."""
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    buttons: List[List[InlineKeyboardButton]] = []

    from urllib.parse import quote_plus

    for p in PRICES:
        amount = p["amount"]
        label = p["label"]

        pay_url = (
            f"{PUBLIC_BASE_URL}/donar"
            f"?amount={amount}"
            f"&label={quote_plus(label)}"
            f"&chat_id={chat_id}"
            f"&user_name={quote_plus(user.first_name or 'Gast')}"
        )

        buttons.append(
            [InlineKeyboardButton(f"{label} · {amount:.2f} EUR", url=pay_url)]
        )

    text = build_menu_text()
    await update.effective_message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mensaje de inicio."""
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "Hola 👋\n"
        "Ich bin dein Cosplay-Live-Bot.\n\n"
        "Befehle:\n"
        "• /menu – Preisliste & Buttons\n"
        "• /liveon – Auto-Hinweise im aktuellen Chat aktivieren\n"
        "• /liveoff – Auto-Hinweise deaktivieren\n\n"
        "Schreibe einfach im Chat, ich helfe mit einer Übersetzung ins Deutsche (🌐)."
    )

# -------------------------------------------------------------------
# TRADUCCIÓN BÁSICA EN EL CHAT
# -------------------------------------------------------------------

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traduce mensajes de usuarios normales al alemán."""
    message = update.effective_message
    if not message:
        return

    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption
    if not text:
        return

    try:
        translated = GoogleTranslator(source="auto", target="de").translate(text)
    except Exception as e:
        logger.error(f"[translate_in_chat] Error traduciendo: {e}")
        return

    # Si la traducción es igual, no hace falta repetir
    if translated.strip().lower() == text.strip().lower():
        return

    await message.reply_text(f"🌐 {translated}")

# -------------------------------------------------------------------
# STRIPE: RUTA /donar Y WEBHOOK
# -------------------------------------------------------------------

stripe.api_key = STRIPE_SECRET_KEY


@flask_app.route("/")
def index():
    return "Cosplay Live Bot läuft ✅", 200


@flask_app.route("/donar")
def donar():
    """Crea una sesión de Stripe Checkout y redirige al usuario."""
    if not STRIPE_SECRET_KEY:
        return "Stripe Secret Key fehlt", 500

    amount = float(request.args.get("amount", "0"))
    label = request.args.get("label", "Support")
    chat_id = request.args.get("chat_id", "")
    user_name = request.args.get("user_name", "Gast")

    if amount <= 0 or not chat_id:
        return "Ungültige Parameter", 400

    # Stripe trabaja en centavos
    unit_amount = int(amount * 100)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": STRIPE_CURRENCY,
                        "product_data": {"name": label},
                        "unit_amount": unit_amount,
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"{PUBLIC_BASE_URL}/ok",
            cancel_url=f"{PUBLIC_BASE_URL}/cancel",
            metadata={
                "chat_id": str(chat_id),
                "user_name": user_name,
                "label": label,
                "amount": str(amount),
            },
        )
    except Exception as e:
        logger.error(f"[donar] Error creando sesión de Stripe: {e}")
        return "Stripe Fehler", 500

    # Redirigimos directamente a la página de pago
    return redirect(session.url, code=303)


@flask_app.route("/ok")
def ok():
    return "✅ Zahlung erfolgreich. Danke für deinen Support!", 200


@flask_app.route("/cancel")
def cancel():
    return "Zahlung abgebrochen.", 200


@flask_app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Recibe eventos de Stripe y manda el superchat al chat de la modelo."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    event = None
    try:
        if endpoint_secret:
            event = stripe.Webhook.construct_event(
                payload, sig_header, endpoint_secret
            )
        else:
            # Sin verificación (solo pruebas)
            event = stripe.Event.construct_from(
                jsonify(payload).json, stripe.api_key
            )
    except Exception as e:
        logger.error(f"[stripe_webhook] Error verificando webhook: {e}")
        return "Webhook error", 400

    if event and event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {}) or {}
        chat_id = meta.get("chat_id")
        user_name = meta.get("user_name", "Ein Fan")
        label = meta.get("label", "Support")
        amount = meta.get("amount", "")

        if chat_id and tg_app:
            text = (
                f"💥 Neue Spende!\n"
                f"👤 *{user_name}* hat *{label}* für *{amount} €* gekauft.\n"
                f"Das Model bedankt sich live! 🙏"
            )

            try:
                tg_app.create_task(
                    tg_app.bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                        parse_mode="Markdown",
                    )
                )
            except Exception as e:
                logger.error(f"[stripe_webhook] Error enviando superchat: {e}")

    return "OK", 200

# -------------------------------------------------------------------
# ARRANQUE DEL BOT (TELEGRAM) + FLASK
# -------------------------------------------------------------------

def start_bot():
    """Arranca el bot de Telegram en un hilo separado."""
    global tg_app

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no está configurado.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Guardamos la instancia global para usarla en el webhook
    tg_app = application

    # Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("liveon", cmd_liveon))
    application.add_handler(CommandHandler("liveoff", cmd_liveoff))

    # Traducción: mensajes de texto en chats
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, translate_in_chat)
    )

    logger.info("Bot de Telegram iniciando con run_polling()...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Hilo para el bot
    bot_thread = threading.Thread(target=start_bot, name="tg-bot", daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))
    logger.info(f"Servidor Flask escuchando en puerto {port}")
    flask_app.run(host="0.0.0.0", port=port)
