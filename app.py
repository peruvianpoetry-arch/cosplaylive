# app.py — CosplayLive (estable + overlay + Stripe + autoshow)
import os, sys, threading, logging, queue, time, json
from decimal import Decimal
from datetime import datetime, timezone

from flask import Flask, Response, request, jsonify

import stripe

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ChannelPostHandler, filters
)

# ========= Logging a Render =========
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ========= Config básica =========
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT  = int(os.getenv("PORT", "10000"))

# Stripe
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBKEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
SUCCESS_URL   = os.getenv("STRIPE_SUCCESS_URL", "https://cosplaylive.onrender.com/?ok=1")
CANCEL_URL    = os.getenv("STRIPE_CANCEL_URL",  "https://cosplaylive.onrender.com/?cancel=1")

if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# ========= Estado por canal/modelo =========
# Puedes pasar MODELS_JSON en env con este formato:
# {"-1001234567890":{"name":"Emma","next_show":"20:00 CET","model_user_id":123456789}}
try:
    MODELS = json.loads(os.getenv("MODELS_JSON", "{}"))
except Exception:
    MODELS = {}

# runtime state (no persistente): live flag y totales por show
RUNTIME = {}  # {channel_id: {"live": False, "show_started_at": ts, "total": Decimal("0"), "currency":"EUR"}}

def ensure_channel(channel_id: int):
    if str(channel_id) not in MODELS:
        # si no está en config, crea un placeholder
        MODELS[str(channel_id)] = {"name": f"Canal {channel_id}", "next_show": "Pronto", "model_user_id": None}
    if channel_id not in RUNTIME:
        RUNTIME[channel_id] = {"live": False, "show_started_at": None, "total": Decimal("0"), "currency":"EUR"}

# ========= Cola para overlay (SSE) =========
events: "queue.Queue[str]" = queue.Queue(maxsize=200)

def push_event(text: str) -> None:
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return
    try:
        events.put_nowait(text)
    except queue.Full:
        try:
            events.get_nowait()
        except queue.Empty:
            pass
        events.put_nowait(text)

# ========= Flask (keep-alive + overlay + Stripe webhook) =========
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

@web.get("/overlay")
def overlay():
    # Overlay simple y vistoso sobre fondo transparente
    html = """<!doctype html><meta charset="utf-8">
    <style>
      html,body{background:transparent;margin:0;height:100vh}
      #chat{font:18px/1.35 system-ui,Segoe UI,Roboto,Arial,sans-serif;color:#fff;
            text-shadow:0 1px 2px rgba(0,0,0,.6); padding:12px; box-sizing:border-box;
            display:flex; flex-direction:column; gap:6px; width:100vw; height:100vh}
      .msg{background:rgba(0,0,0,.35); border-radius:12px; padding:8px 12px; max-width:90%}
    </style>
    <div id="chat"></div>
    <script>
      const chat=document.getElementById('chat');
      const es=new EventSource('/events');
      es.onmessage=(e)=>{
        const div=document.createElement('div');
        div.className='msg';
        div.textContent=e.data;
        chat.appendChild(div);
        while(chat.children.length>40) chat.removeChild(chat.firstChild);
        window.scrollTo(0,document.body.scrollHeight);
      };
    </script>"""
    return html

@web.get("/events")
def sse():
    def stream():
        while True:
            msg = events.get()
            yield f"data: {msg}\n\n"
    headers = {"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

@web.post("/stripe/webhook")
def stripe_webhook():
    # No uses verificación de firma en test para simplificar
    event = request.get_json(silent=True) or {}
    etype = event.get("type") or event.get("type", "")
    log.info("✅ Evento Stripe recibido: %s", etype)

    if etype == "checkout.session.completed":
        data = event.get("data", {}).get("object", {})
        amount_total = data.get("amount_total") or 0
        currency = (data.get("currency") or "eur").upper()
        metadata = data.get("metadata") or {}

        channel_id = int(metadata.get("channel_id", "0") or "0")
        user_display = metadata.get("user_display", "usuario")

        # Euros (o la moneda configurada)
        amount = Decimal(amount_total) / Decimal(100)
        if channel_id:
            ensure_channel(channel_id)
            # sumamos al total del show en curso
            RUNTIME[channel_id]["total"] += amount
            RUNTIME[channel_id]["currency"] = currency
            # mensaje al overlay + canal
            push_event(f"💸 {user_display} → {amount} {currency}")
            try:
                # Nota: mandamos al canal
                from_bot.application.create_task(
                    from_bot.bot.send_message(
                        chat_id=channel_id,
                        text=(f"🎉 <b>¡Gracias, {user_display}!</b>\n"
                              f"Donación: <b>{amount} {currency}</b>"),
                        parse_mode=ParseMode.HTML
                    )
                )
            except Exception as e:
                log.exception("No se pudo enviar agradecimiento al canal: %s", e)

        log.info("💬 Nueva donación: %s - %.2f %s", user_display, amount, currency)
    return jsonify({"ok": True})

# ========= Telegram Bot =========
if not TOKEN:
    raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

# Handlers de utilidad
def fmt_live_banner(model_name: str) -> str:
    # Mensaje vistoso de inicio
    return ("🚨 <b>¡EN VIVO!</b> 🚨\n"
            f"✨ <b>{model_name}</b> acaba de comenzar su show.\n"
            "💬 Usa /donar para apoyar o enviar un pedido.\n"
            "🧨 ¡Que empiece el show!")

def fmt_offline(model_cfg: dict) -> str:
    nxt = model_cfg.get("next_show","Pronto")
    return (f"📺 <b>Ahora mismo no hay stream.</b>\n"
            f"🕒 Próximo show: <b>{nxt}</b>\n"
            "💖 Puedes dejar tu apoyo con /donar")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ¡Bot activo y funcionando correctamente!")

async def donate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea un Checkout rápido. /donar [importe]  (por defecto 5.00)"""
    if not STRIPE_SECRET:
        await update.message.reply_text("⚠️ Stripe no está configurado todavía.")
        return

    amount = Decimal("5.00")
    if context.args:
        try:
            amount = Decimal(context.args[0].replace(",", "."))
        except Exception:
            pass
    cents = int(amount * 100)

    # Identificar canal y modelo
    chat = update.effective_chat
    channel_id = chat.id
    user = update.effective_user
    user_display = user.full_name if user else "usuario"

    metadata = {
        "channel_id": str(channel_id),
        "user_display": user_display,
    }

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data":{
                    "currency":"eur",
                    "product_data":{"name":"Donación Cosplay"},
                    "unit_amount": cents
                },
                "quantity":1
            }],
            metadata=metadata,
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
        )
        url = session.get("url")
    except Exception as e:
        log.exception("Error creando Checkout: %s", e)
        await update.message.reply_text("❌ No se pudo crear el pago. Intenta más tarde.")
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Donar {amount:.2f} €", url=url)
    ],
    [
        InlineKeyboardButton("2 €", callback_data="pay:2"),
        InlineKeyboardButton("5 €", callback_data="pay:5"),
        InlineKeyboardButton("10 €", callback_data="pay:10"),
    ]])

    await update.message.reply_text(
        ("💖 <b>Gracias por apoyar el show</b>\n"
         f"Elige un importe o usa /donar 3.5 para cantidad libre.\n"
         "Tras pagar, el bot anunciará tu donación ✨"),
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

async def donate_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botones rápidos 2/5/10 €"""
    if not update.callback_query:
        return
    data = update.callback_query.data or ""
    if not data.startswith("pay:"):
        return
    await update.callback_query.answer()
    amt = Decimal(data.split(":",1)[1])
    # Reusar /donar con argumento
    update.callback_query.data = None
    msg = update.effective_message
    fake = Update(update.update_id, message=msg)  # reutilizar
    context.args = [str(amt)]
    await donate_cmd(fake, context)

async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Eco en DM (sigue como prueba)"""
    user = update.effective_user.full_name if update.effective_user else "Usuario"
    txt  = update.message.text or ""
    log.info("[DM] %s: %s", user, txt)
    push_event(f"✉️ {user}: {txt}")
    await update.message.reply_text(f"✉️ {txt}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mensajes en el canal: si no está en vivo, avisar próximo show."""
    if not update.channel_post:
        return
    ch = update.effective_chat
    channel_id = ch.id
    ensure_channel(channel_id)

    txt = update.channel_post.text or ""
    # no respondas a service messages desde aquí
    if not txt:
        return

    if not RUNTIME[channel_id]["live"]:
        await context.bot.send_message(
            chat_id=channel_id,
            text=fmt_offline(MODELS[str(channel_id)]),
            parse_mode=ParseMode.HTML
        )

async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta inicio/fin de stream por service messages (video chat)."""
    if not update.channel_post:
        return
    msg = update.channel_post
    ch = update.effective_chat
    channel_id = ch.id
    ensure_channel(channel_id)

    # PTB 20 envía flags en msg.video_chat_started / msg.video_chat_ended
    started = getattr(msg, "video_chat_started", None)
    ended   = getattr(msg, "video_chat_ended", None)

    if started is not None:
        # START
        RUNTIME[channel_id]["live"] = True
        RUNTIME[channel_id]["show_started_at"] = time.time()
        RUNTIME[channel_id]["total"] = Decimal("0")
        model_name = MODELS[str(channel_id)]["name"]
        await context.bot.send_message(
            chat_id=channel_id,
            text=fmt_live_banner(model_name),
            parse_mode=ParseMode.HTML
        )
        push_event(f"🔴 LIVE: {model_name}")

    if ended is not None:
        # END
        await close_show_and_report(context, channel_id)

async def start_show_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando manual /startshow (por si el service message no llega)"""
    ch = update.effective_chat
    channel_id = ch.id
    ensure_channel(channel_id)
    RUNTIME[channel_id]["live"] = True
    RUNTIME[channel_id]["show_started_at"] = time.time()
    RUNTIME[channel_id]["total"] = Decimal("0")
    model_name = MODELS[str(channel_id)]["name"]
    await context.bot.send_message(
        chat_id=channel_id,
        text=fmt_live_banner(model_name),
        parse_mode=ParseMode.HTML
    )
    push_event(f"🔴 LIVE: {model_name}")

async def end_show_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando manual /endshow (por si el service message no llega)"""
    ch = update.effective_chat
    channel_id = ch.id
    await close_show_and_report(context, channel_id)

async def close_show_and_report(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    """Cierra show, calcula 60/40, avisa al canal y DM a la modelo si está configurada."""
    ensure_channel(channel_id)
    if not RUNTIME[channel_id]["live"]:
        return
    RUNTIME[channel_id]["live"] = False
    total = RUNTIME[channel_id]["total"]
    currency = RUNTIME[channel_id]["currency"]
    started = RUNTIME[channel_id]["show_started_at"] or time.time()
    dur_min = int((time.time() - started) / 60)

    m60 = (total * Decimal("0.60")).quantize(Decimal("0.01"))
    m40 = (total * Decimal("0.40")).quantize(Decimal("0.01"))

    model_cfg = MODELS[str(channel_id)]
    model_name = model_cfg.get("name","Modelo")
    model_user_id = model_cfg.get("model_user_id")

    summary = (f"🏁 <b>Show finalizado</b>\n"
               f"👤 {model_name}\n"
               f"⏱️ Duración: {dur_min} min\n"
               f"💰 Total: <b>{total:.2f} {currency}</b>\n"
               f"🧾 Reparto → Modelo 60%: <b>{m60:.2f} {currency}</b> | Casa 40%: <b>{m40:.2f} {currency}</b>")
    # Canal
    await context.bot.send_message(chat_id=channel_id, text=summary, parse_mode=ParseMode.HTML)
    push_event(f"⏹️ END • {model_name} • {total:.2f} {currency}")

    # DM a modelo si está configurada
    if model_user_id:
        try:
            await context.bot.send_message(chat_id=int(model_user_id), text=summary, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.exception("No pude DM a la modelo: %s", e)

# ========= Arranque bot + web =========
def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def build_app():
    app = ApplicationBuilder().token(TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("donar", donate_cmd))
    app.add_handler(CommandHandler("startshow", start_show_cmd))
    app.add_handler(CommandHandler("endshow",   end_show_cmd))

    # Botones de donación
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, on_status))  # video_chat_started/ended
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, echo_msg))
    app.add_handler(ChannelPostHandler(channel_post))

    # Callback buttons
    app.add_handler(MessageHandler(filters.Regex(r"^pay:\d+(\.\d+)?$"), donate_buttons))
    return app

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Falta TELEGRAM_TOKEN en Environment.")

    # Servidor Flask en paralelo
    threading.Thread(target=run_web, daemon=True).start()

    # Bot
    from_bot = build_app()
    log.info("🤖 Iniciando bot (polling SINCRONO)…")
    from_bot.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
