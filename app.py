# app.py — CosplayLive (Telegram-only, sin OBS, con “superchat” y Stripe)
import os, sys, time, threading, logging, queue, io
from datetime import datetime, timedelta

from flask import Flask, Response, request, send_file

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

import stripe
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont

# ========= LOGGING =========
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ========= ENV =========
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # -100xxxxxxxxxx
BASE_URL = os.getenv("BASE_URL", "")              # https://tuapp.onrender.com
STRIPE_SK = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WH = os.getenv("STRIPE_WEBHOOK_SECRET", "")  # whsec_***

if not TOKEN:
    raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")
if not STRIPE_SK:
    log.warning("⚠️ Falta STRIPE_SECRET_KEY (solo pruebas de UI)")

# Stripe
stripe.api_key = STRIPE_SK

# ========= ESTADO SIMPLE =========
last_activity = datetime.utcnow() - timedelta(hours=1)
last_ad = datetime.utcnow() - timedelta(hours=1)
LIVE_FORCED = False     # /liveon /liveoff
PRICE_MENU = [
    ("💃 Baile", 3),
    ("👗 Probar lencería", 10),
    ("🙈 Topless", 5),
    ("🎯 Meta grupal", 50),
]
CURRENCY = os.getenv("CURRENCY", "EUR")

# ========= COLA DE EVENTOS PARA /studio (SSE) =========
events: "queue.Queue[str]" = queue.Queue(maxsize=200)

def push_event(text: str) -> None:
    text = (text or "").replace("\n", " ").strip()
    if not text: return
    try:
        events.put_nowait(text)
    except queue.Full:
        try: events.get_nowait()
        except queue.Empty: pass
        events.put_nowait(text)

# ========= FLASK =========
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive bot está corriendo"

# Página para la modelo: muestra alertas y suena
@web.get("/studio")
def studio():
    html = """
<!doctype html><html><head><meta charset="utf-8"><title>Cosplay Studio</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial;background:#0b0f17;color:#fff;margin:0}
#wrap{max-width:940px;margin:0 auto;padding:16px}
.event{background:#121b2e;border-radius:14px;padding:14px;margin:10px 0;
box-shadow:0 6px 24px rgba(0,0,0,.35);font-size:20px}
h1{font-weight:700} .muted{opacity:.7}
</style></head><body><div id="wrap">
<h1>👩‍🎤 Cosplay Studio</h1>
<p class="muted">Mantén esta página abierta. Sonará y mostrará avisos cuando haya donaciones o pedidos.</p>
<div id="events"></div>
<audio id="ding"><source src="https://actions.google.com/sounds/v1/alarms/beep_short.ogg" type="audio/ogg"></audio>
</div>
<script>
const box = document.getElementById('events');
const ding = document.getElementById('ding');
const es = new EventSource('/events');
es.onmessage = (e)=>{
  const div = document.createElement('div');
  div.className='event';
  div.textContent = e.data;
  box.prepend(div);
  try{ ding.currentTime = 0; ding.play(); }catch(_){}
};
</script></body></html>
    """
    return html

@web.get("/events")
def sse():
    def stream():
        while True:
            msg = events.get()
            yield f"data: {msg}\n\n"
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# Imagen “tarjeta” grande para el chat
def build_card(title: str, subtitle: str) -> bytes:
    W, H = 1200, 500
    img = Image.new("RGB", (W,H), (8,12,22))
    draw = ImageDraw.Draw(img)
    # tipografías del sistema si PIL no encuentra otras
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 68)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 44)
    except:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
    # caja
    draw.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    # título
    tw, th = draw.textsize(title, font=font_big)
    draw.text(((W-tw)//2, 140), title, font=font_big, fill=(255,255,255))
    # subtítulo
    sw, sh = draw.textsize(subtitle, font=font_small)
    draw.text(((W-sw)//2, 260), subtitle, font=font_small, fill=(190,220,255))
    # confeti simple
    for x in range(50):
        draw.ellipse((40+x*22, 60+(x*11)%320, 40+x*22+10, 70+(x*11)%320+10), fill=(255, 120+(x*3)%120, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ========= TELEGRAM =========
def kb_donaciones() -> InlineKeyboardMarkup:
    rows = []
    for name, price in PRICE_MENU:
        rows.append([InlineKeyboardButton(f"{name} · {price} {CURRENCY}",
                                          url=f"{BASE_URL}/donar?amt={price}&c={CURRENCY}")])
    rows.append([InlineKeyboardButton("💝 Donar libre", url=f"{BASE_URL}/donar")])
    return InlineKeyboardMarkup(rows)

async def announce_prices(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    global last_ad
    text = (
        "✨ *Menú de apoyos y pedidos:*\n\n" +
        "\n".join([f"• {n} — *{p}* {CURRENCY}" for n,p in PRICE_MENU]) +
        "\n\nToca un botón para pagar con tarjeta/PayPal (Stripe)."
    )
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones())
    last_ad = datetime.utcnow()

async def celebrate(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: str, amount: str, memo: str):
    # 1) Mensaje grande
    txt = f"🎉 *¡Gracias, {user}!*\nHas apoyado con *{amount}*.\n_{memo or '¡A tope con el show!'}_"
    msg = await context.bot.send_message(chat_id, txt, parse_mode=ParseMode.MARKDOWN)
    # 2) Pin temporal
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        await asyncio_sleep(15)
        await context.bot.unpin_chat_message(chat_id, msg.message_id)
    except Exception as e:
        log.info(f"Pin opcional: {e}")
    # 3) Cambiar título temporal
    try:
        old = (await context.bot.get_chat(chat_id)).title or ""
        new = f"🔥 Gracias {user} ({amount})"
        await context.bot.set_chat_title(chat_id, new)
        await asyncio_sleep(15)
        await context.bot.set_chat_title(chat_id, old)
    except Exception as e:
        log.info(f"Título opcional: {e}")
    # 4) Tarjeta gráfica
    card = build_card(f"¡Gracias {user}!", f"Apoyo: {amount}")
    await context.bot.send_photo(chat_id, photo=InputFile(io.BytesIO(card), filename="thanks.png"))
    # 5) Evento para /studio (sonido)
    push_event(f"🎉 Donación: {user} → {amount} | {memo or ''}")

async def asyncio_sleep(s: float):
    # pequeño helper para no importar asyncio explícito arriba
    import asyncio
    await asyncio.sleep(s)

# ====== STRIPE CHECKOUT PAGES (muy simple) ======
@web.get("/donar")
def donate_page():
    # si hay amt, iniciamos Checkout directo; si no, elegimos importe
    amt = request.args.get("amt", "")
    ccy = request.args.get("c", CURRENCY)
    title = "Apoyo CosplayLive"
    if not STRIPE_SK or not BASE_URL:
        return "<b>Stripe no está configurado</b> (STRIPE_SECRET_KEY/BASE_URL)."
    if amt.isdigit():
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": ccy.lower(),
                    "product_data": {"name": title},
                    "unit_amount": int(float(amt) * 100),
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"channel_id": CHANNEL_ID, "amount": f"{amt} {ccy}"},
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    # selector sencillo
    opts = "".join([f'<a href="/donar?amt={p}&c={ccy}">{n} · {p} {ccy}</a><br>' for n,p in PRICE_MENU])
    return f"<h3>Seleccione un apoyo</h3>{opts}<p><a href='{BASE_URL}/ok'>Volver</a></p>"

@web.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WH)
    except Exception as e:
        log.error(f"Webhook inválido: {e}")
        return "bad", 400

    et = event["type"]
    if et == "checkout.session.completed":
        sess = event["data"]["object"]
        metadata = sess.get("metadata") or {}
        amount = metadata.get("amount") or f"{(sess.get('amount_total') or 0)/100:.2f} {sess.get('currency','').upper()}"
        payer = (sess.get("customer_details") or {}).get("email", "usuario")
        memo = "¡Gracias por tu apoyo!"
        # avisar al bot
        try:
            app = telegram_app_singleton()
            app.create_task( celebrate(app.bot, int(CHANNEL_ID), payer, amount, memo) )
        except Exception as e:
            log.error(f"No se pudo anunciar en TG: {e}")
        log.info(f"✅ Evento Stripe recibido: {et} — {amount}")
    return "ok", 200

# ========= HANDLERS TELEGRAM =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    chat = update.effective_chat
    last_activity = datetime.utcnow()
    await update.message.reply_text(
        "🤖 ¡Bot activo! Usa /menu para ver donaciones o /precios.\n/studio te da la consola con alertas y sonido."
    )
    if chat and chat.id == int(CHANNEL_ID):
        await announce_prices(context, chat.id)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones())

async def precios_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    await announce_prices(context, update.effective_chat.id)

async def studio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🎛️ Abre tu panel: {BASE_URL}/studio")

# Mensajes en DM o grupo → eco + traducción básica
async def echo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    txt = update.message.text or ""
    user = update.effective_user
    name = user.full_name if user else "Usuario"
    # detectar idioma y traducir hacia ES y DE solo como demo corta
    try:
        src = GoogleTranslator(source="auto", target="es").detect(txt)  # devuelve código estimado
    except Exception:
        src = "auto"
    try:
        a_es = GoogleTranslator(source="auto", target="es").translate(txt)
        a_de = GoogleTranslator(source="auto", target="de").translate(txt)
        reply = f"📩 {name} dijo:\n{txt}\n\nES: {a_es}\nDE: {a_de}"
    except Exception:
        reply = f"📩 {name}: {txt}"
    await update.message.reply_text(reply)

# Mensajes publicados por el canal (cuando escribes en el canal)
async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    post = update.channel_post
    if not post: return
    # si alguien escribe en el canal, de vez en cuando recordamos el menú
    now = datetime.utcnow()
    if (now - last_ad) > timedelta(minutes=10):
        await announce_prices(context, post.chat.id)

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LIVE_FORCED
    LIVE_FORCED = True
    await update.message.reply_text("🟢 Marketing automático ACTIVADO")
async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LIVE_FORCED
    LIVE_FORCED = False
    await update.message.reply_text("🔴 Marketing automático PAUSADO")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)

# ========= TAREA: marketing periódico (sin spam) =========
def scheduler(app):
    import asyncio
    async def tick():
        while True:
            try:
                now = datetime.utcnow()
                active = LIVE_FORCED or (now - last_activity) < timedelta(minutes=15)
                due = (now - last_ad) > timedelta(minutes=10)
                if active and due and CHANNEL_ID:
                    await announce_prices(app.bot, int(CHANNEL_ID))
                await asyncio.sleep(30)
            except Exception as e:
                log.error(f"scheduler: {e}")
                await asyncio.sleep(5)
    app.create_task(tick())

# ========= SINGLETON DEL APP =========
_app_singleton = None
def telegram_app_singleton():
    global _app_singleton
    if _app_singleton: return _app_singleton
    _app_singleton = ApplicationBuilder().token(TOKEN).build()
    _app_singleton.add_handler(CommandHandler("start", start_cmd))
    _app_singleton.add_handler(CommandHandler("menu", menu_cmd))
    _app_singleton.add_handler(CommandHandler("precios", precios_cmd))
    _app_singleton.add_handler(CommandHandler("studio", studio_cmd))
    _app_singleton.add_handler(CommandHandler("liveon", liveon))
    _app_singleton.add_handler(CommandHandler("liveoff", liveoff))
    _app_singleton.add_handler(MessageHandler(filters.TEXT & ~filters.ChatType.CHANNEL, echo_msg))
    _app_singleton.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    _app_singleton.add_error_handler(on_error)
    scheduler(_app_singleton)
    return _app_singleton

# ========= MAIN =========
def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    app = telegram_app_singleton()
    t = threading.Thread(target=run_web, daemon=True)
    t.start()
    log.info("🤖 Iniciando bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
