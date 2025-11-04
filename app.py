# app.py — CosplayLive PRO: Overlay/mirror + Assistant + Stripe + SSE
# Requiere requirements.txt:
# python-telegram-bot==20.8
# Flask==3.0.3
# stripe==9.11.0
# deep-translator==1.11.4
# Pillow==10.4.0

import os, sys, threading, logging, queue, io, html
from datetime import datetime, timedelta

from flask import Flask, Response, request

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode, ChatType
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

import asyncio
import stripe

# ====== PIL opcional ======
PIL_OK = True
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    PIL_OK = False

from deep_translator import GoogleTranslator

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
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()   # -100xxxxxxxxxx (canal o grupo destino)
BASE_URL = os.getenv("BASE_URL", "")               # https://tuapp.onrender.com
STRIPE_SK = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WH = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CURRENCY = os.getenv("CURRENCY", "EUR")

# Auto marketing
ANNOUNCE_EVERY_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN", "5"))
AUTO_MARKETING = os.getenv("AUTO_MARKETING", "on").lower() in ("1","on","true","yes")
QUIET_HOURS = os.getenv("QUIET_HOURS", "")  # ej "02-08"

# Modelo(s) para traducción dirigida
MODEL_USER_IDS = [s.strip() for s in os.getenv("MODEL_USER_IDS", "").split(",") if s.strip()]
AUDIENCE_LANGS = [s.strip() for s in os.getenv("AUDIENCE_LANGS", "es,de").split(",") if s.strip()]

if not TOKEN:
    raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")
if not STRIPE_SK:
    log.warning("⚠️ Falta STRIPE_SECRET_KEY (solo pruebas de UI)")
if not PIL_OK:
    log.warning("⚠️ Pillow no disponible: tarjetas gráficas desactivadas temporalmente.")

stripe.api_key = STRIPE_SK

# ========= ESTADO =========
last_activity = datetime.utcnow() - timedelta(hours=1)
last_ad = datetime.utcnow() - timedelta(hours=1)
LIVE_FORCED = True  # comienza activo
PRICE_MENU = [
    ("💃 Baile", 3),
    ("👗 Probar lencería", 10),
    ("🙈 Topless", 5),
    ("🎯 Meta grupal", 50),
]
ROTATION = [
    "✨ *Menú de apoyos y pedidos* (elige un botón) ↓",
    "🎯 *Meta grupal:* cuando lleguemos a 50 EUR desbloqueamos *show especial*.",
    "💡 *Tip:* Puedes dejar un mensaje con tu pedido cuando apoyes.",
    "🔥 *Gracias por apoyar el show!* Usa los botones para participar.",
]
_rot_idx = 0
SUPPRESS_AFTER_DONATION_SEC = 90

# Overlay ON/OFF (para /overlay y /liveview)
OVERLAY_ENABLED = True

# ========= COLAS DE EVENTOS (SSE) =========
events_studio: "queue.Queue[str]" = queue.Queue(maxsize=300)   # /events (studio)
events_overlay: "queue.Queue[str]" = queue.Queue(maxsize=600)  # /overlay-events (mirror público)

def _push(q: "queue.Queue[str]", text: str):
    text = (text or "").replace("\n", " ").strip()
    if not text: return
    try:
        q.put_nowait(text)
    except queue.Full:
        try: q.get_nowait()
        except queue.Empty: pass
        q.put_nowait(text)

def push_studio(text: str): _push(events_studio, text)
def push_overlay(text: str):
    if OVERLAY_ENABLED:
        _push(events_overlay, text)

# ========= FLASK =========
web = Flask(__name__)

@web.get("/")
def home():
    return "✅ CosplayLive PRO corriendo"

# ---------- Studio (solo modelo, con sonido) ----------
@web.get("/studio")
def studio():
    return """
<!doctype html><html><head><meta charset="utf-8"><title>Cosplay Studio</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial;background:#0b0f17;color:#fff;margin:0}
#wrap{max-width:940px;margin:0 auto;padding:16px}
.event{background:#121b2e;border-radius:14px;padding:14px;margin:10px 0;
box-shadow:0 6px 24px rgba(0,0,0,.35);font-size:20px}
h1{font-weight:700} .muted{opacity:.7} a{color:#9bd}
</style></head><body><div id="wrap">
<h1>👩‍🎤 Cosplay Studio</h1>
<p class="muted">Mantén esta página abierta. Sonará y mostrará avisos cuando haya donaciones o pedidos.</p>
<div id="events"></div>
<audio id="ding"><source src="https://actions.google.com/sounds/v1/alarms/beep_short.ogg" type="audio/ogg"></audio>
</div>
<script>
const box=document.getElementById('events'); const ding=document.getElementById('ding');
const es=new EventSource('/events');
es.onmessage=(e)=>{ const div=document.createElement('div'); div.className='event';
div.textContent=e.data; box.prepend(div); try{ding.currentTime=0; ding.play();}catch(_){}};</script>
</body></html>"""

@web.get("/events")
def sse_studio():
    def stream():
        while True:
            msg = events_studio.get()
            yield f"data: {msg}\n\n"
    headers = {"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ---------- Overlay público (mirror) ----------
@web.get("/overlay")
def overlay_only():
    # Overlay puro (fondo transparente): útil para OBS o player externo con CSS overlay
    return """
<!doctype html><html><head><meta charset="utf-8"><title>Overlay</title>
<style>
html,body{background:rgba(0,0,0,0);margin:0;overflow:hidden}
#stack{position:fixed;left:0;right:0;bottom:10px;display:flex;flex-direction:column-reverse;gap:10px;padding:10px;pointer-events:none}
.msg{align-self:center;max-width:88vw;background:rgba(0,0,0,.55);color:#fff;border-radius:18px;padding:12px 18px;
font:600 22px system-ui,Segoe UI,Roboto,Arial;box-shadow:0 8px 30px rgba(0,0,0,.45);animation:pop .25s ease-out}
@keyframes pop{from{transform:scale(.95);opacity:.2}to{transform:scale(1);opacity:1}}
.hidden{display:none}
</style></head><body>
<div id="stack"></div>
<script>
const st=document.getElementById('stack'); let on=true;
const es=new EventSource('/overlay-events');
es.onmessage=(e)=>{ if(!on) return; const div=document.createElement('div'); div.className='msg';
div.textContent=e.data; st.append(div); setTimeout(()=>div.remove(), 12000); };
</script></body></html>
    """

@web.get("/liveview")
def liveview():
    # Página con reproductor + overlay encima (si pones una URL HLS/MP4 en ?src=)
    src = request.args.get("src","")
    safe = html.escape(src)
    video_html = f'<video id="v" src="{safe}" autoplay playsinline controls style="width:100%;height:auto;background:#000"></video>' if src else "<div style='background:#000;height:48vh;border-radius:14px'></div>"
    return f"""
<!doctype html><html><head><meta charset="utf-8"><title>Live + Overlay</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
body{{margin:0;background:#0b0f17;color:#e7f2ff;font:16px system-ui,Segoe UI,Roboto}}
.wrap{{max-width:980px;margin:0 auto;padding:14px}}
.bar{{display:flex;justify-content:space-between;align-items:center;margin:8px 0}}
.btn{{appearance:none;border:0;border-radius:10px;padding:10px 14px;background:#1b2a48;color:#fff;cursor:pointer}}
#stage{{position:relative}}
#overlay{{position:absolute;left:0;right:0;top:0;bottom:0;pointer-events:none}}
#stack{{position:absolute;left:0;right:0;bottom:10px;display:flex;flex-direction:column-reverse;gap:10px;padding:10px}}
.msg{{align-self:center;max-width:88%;background:rgba(0,0,0,.55);color:#fff;border-radius:18px;padding:12px 18px;
font:600 22px system-ui,Segoe UI,Roboto,Arial;box-shadow:0 8px 30px rgba(0,0,0,.45);animation:pop .25s ease-out}}
@keyframes pop{{from{{transform:scale(.95);opacity:.2}}to{{transform:scale(1);opacity:1}}}}
</style></head><body>
<div class="wrap">
  <div class="bar">
    <div>🔴 Vista pública con *mirror* del chat</div>
    <div>
      <button class="btn" id="toggle">Ocultar overlay</button>
    </div>
  </div>
  <div id="stage">{video_html}
    <div id="overlay"><div id="stack"></div></div>
  </div>
  <p style="opacity:.75;margin-top:8px">Tip: agrega tu stream HLS/MP4 como <code>?src=URL</code>. El overlay funciona siempre, aunque no haya video embebido.</p>
</div>
<script>
let on=true; const btn=document.getElementById('toggle'); const st=document.getElementById('stack');
btn.onclick=()=>{{on=!on; btn.textContent= on? 'Ocultar overlay':'Mostrar overlay';}};
const es=new EventSource('/overlay-events');
es.onmessage=(e)=>{{ if(!on) return; const div=document.createElement('div'); div.className='msg';
div.textContent=e.data; st.append(div); setTimeout(()=>div.remove(),12000); }};
</script></body></html>
    """

@web.get("/overlay-events")
def sse_overlay():
    def stream():
        while True:
            msg = events_overlay.get()
            yield f"data: {msg}\n\n"
    headers = {"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ---------- Stripe: donaciones ----------
def build_card(title: str, subtitle: str):
    if not PIL_OK:
        return None
    W,H = 1200,500
    img = Image.new("RGB",(W,H),(8,12,22))
    d = ImageDraw.Draw(img)
    try:
        f1 = ImageFont.truetype("DejaVuSans-Bold.ttf", 68)
        f2 = ImageFont.truetype("DejaVuSans.ttf", 44)
    except Exception:
        f1 = ImageFont.load_default(); f2 = ImageFont.load_default()
    d.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    tw,th = d.textsize(title, font=f1); d.text(((W-tw)//2,140), title, font=f1, fill=(255,255,255))
    sw,sh = d.textsize(subtitle, font=f2); d.text(((W-sw)//2,260), subtitle, font=f2, fill=(190,220,255))
    for x in range(50):
        d.ellipse((40+x*22, 60+(x*11)%320, 50+x*22, 70+(x*11)%320), fill=(255,120+(x*3)%120,80))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0); return buf

def kb_donaciones() -> InlineKeyboardMarkup:
    rows=[]
    for name, price in PRICE_MENU:
        rows.append([InlineKeyboardButton(f"{name} · {price} {CURRENCY}",
                                          url=f"{BASE_URL}/donar?amt={price}&c={CURRENCY}")])
    rows.append([InlineKeyboardButton("💝 Donar libre", url=f"{BASE_URL}/donar")])
    return InlineKeyboardMarkup(rows)

@web.get("/donar")
def donate_page():
    amt = request.args.get("amt","")
    ccy = request.args.get("c", CURRENCY)
    title = "Apoyo CosplayLive"
    if not STRIPE_SK or not BASE_URL:
        return "<b>Stripe no está configurado</b> (STRIPE_SECRET_KEY/BASE_URL)."
    # Si llega con ?amt= -> Checkout directo
    if amt.isdigit():
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": ccy.lower(),
                    "product_data": {"name": title},
                    "unit_amount": int(float(amt)*100),
                },
                "quantity": 1
            }],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"channel_id": CHANNEL_ID, "amount": f"{amt} {ccy}"},
            allow_promotion_codes=True,
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    # Donación libre: formulario simple
    return f"""
<!doctype html><html><head><meta charset="utf-8"><title>Donar</title>
<style>body{{font-family:system-ui;padding:20px}}</style></head><body>
<h3>Elige un importe</h3>
<form method="get" action="/donar">
  <input type="hidden" name="c" value="{ccy}">
  <input name="amt" type="number" min="1" step="1" value="5" style="padding:8px"> {ccy}
  <button type="submit" style="padding:8px 12px">Pagar</button>
</form>
<p>O elige rápido:</p>
{"".join([f'<a href="/donar?amt={p}&c={ccy}">{n} · {p} {ccy}</a><br>' for n,p in PRICE_MENU])}
</body></html>
    """

@web.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature","")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WH)
    except Exception as e:
        log.error(f"Webhook inválido: {e}"); return "bad", 400

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        metadata = sess.get("metadata") or {}
        amount = metadata.get("amount") or f"{(sess.get('amount_total') or 0)/100:.2f} {sess.get('currency','').upper()}"
        payer = (sess.get("customer_details") or {}).get("email","usuario")
        memo = "¡Gracias por tu apoyo!"
        try:
            app = telegram_app_singleton()
            app.create_task(celebrate(app.bot, int(CHANNEL_ID), payer, amount, memo))
        except Exception as e:
            log.error(f"No se pudo anunciar en TG: {e}")
        log.info(f"✅ Stripe: pago recibido — {amount}")
    return "ok", 200

# ========= Telegram features =========
async def announce_prices(bot, chat_id: int, prefix: str | None = None):
    global last_ad, _rot_idx
    header = prefix or ROTATION[_rot_idx % len(ROTATION)]; _rot_idx += 1
    text = (f"{header}\n\n" + "\n".join([f"• {n} — *{p}* {CURRENCY}" for n,p in PRICE_MENU])
            + "\n\nToca un botón para pagar con tarjeta/PayPal alternativos (Stripe muestra métodos locales).")
    await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones())
    last_ad = datetime.utcnow()

async def celebrate(bot, chat_id: int, user: str, amount: str, memo: str):
    global last_activity, last_ad
    txt = f"🎉 *¡Gracias, {user}!*\nHas apoyado con *{amount}*.\n_{memo or '¡A tope con el show!'}_"
    msg = await bot.send_message(chat_id, txt, parse_mode=ParseMode.MARKDOWN)
    try:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        await asyncio.sleep(15); await bot.unpin_chat_message(chat_id, msg.message_id)
    except Exception as e:
        log.info(f"Pin opcional: {e}")
    try:
        old = (await bot.get_chat(chat_id)).title or ""
        new = f"🔥 Gracias {user} ({amount})"
        await bot.set_chat_title(chat_id, new)
        await asyncio.sleep(15); await bot.set_chat_title(chat_id, old)
    except Exception as e:
        log.info(f"Título opcional: {e}")
    buf = build_card(f"¡Gracias {user}!", f"Apoyo: {amount}")
    if buf: await bot.send_photo(chat_id, photo=InputFile(buf, filename="thanks.png"))
    else:   await bot.send_message(chat_id, "🖼️ (Tarjeta gráfica desactivada temporalmente)")
    # Avisos
    push_studio(f"🎉 Donación: {user} → {amount} | {memo or ''}")
    push_overlay(f"🎉 {user}: {amount}")
    # Pausa anuncios
    last_activity = datetime.utcnow(); last_ad = datetime.utcnow()

# --- Assistant triggers ---
GREET_WORDS = {"hola","hello","hi","hallo","servus","hey","buenas","holi"}
ASSISTANT_REPLY = ("🤖 *Asistente de la modelo*: escribe tu pedido o pregunta.\n"
                   "Puedo traducir tus mensajes y darte info del menú con /menu o /precios.")

def _language_auto_detect(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="en").detect(text)
    except Exception:
        return "auto"

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        "🤖 ¡Bot activo! Usa /menu para donaciones o /precios.\n/studio (modelo) • /overlay (OBS) • /liveview (público).")
    if chat and str(chat.id) == str(CHANNEL_ID):
        await announce_prices(context.bot, chat.id, prefix="🚀 *Show en vivo:* usa el menú para participar")

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones())

async def precios_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await announce_prices(context.bot, update.effective_chat.id)

async def studio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🎛️ Abre tu panel: {BASE_URL}/studio")

async def overlay_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OVERLAY_ENABLED; OVERLAY_ENABLED=True
    await update.message.reply_text("🟢 Overlay activado")

async def overlay_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OVERLAY_ENABLED; OVERLAY_ENABLED=False
    await update.message.reply_text("🔴 Overlay desactivado")

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LIVE_FORCED; LIVE_FORCED=True
    await update.message.reply_text("🟢 Marketing automático ACTIVADO")

async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LIVE_FORCED; LIVE_FORCED=False
    await update.message.reply_text("🔴 Marketing automático PAUSADO")

# --- Mensajes en PRIVADOS/GRUPOS: asistente + traducción dirigida + mirror overlay ---
async def group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    msg = update.message
    if not msg or not msg.text: return
    text = msg.text.strip()
    author = update.effective_user
    author_id = str(author.id) if author else ""
    name = author.full_name if author else "Usuario"

    # 1) Mirror al overlay (público) si estamos en el canal/grupo objetivo
    try:
        if str(update.effective_chat.id) == str(CHANNEL_ID):
            # Evitar espejear mensajes del propio bot
            if not (author and author.is_bot):
                push_overlay(f"{name}: {text}")
    except Exception as e:
        log.info(f"overlay mirror: {e}")

    # 2) Respuesta asistente básica a saludos
    if text.lower() in GREET_WORDS:
        await msg.reply_text(ASSISTANT_REPLY, parse_mode=ParseMode.MARKDOWN)

    # 3) Traducción dirigida
    try:
        if author_id and author_id in MODEL_USER_IDS:
            # Mensaje de la modelo -> traducir al público (idiomas AUDIENCE_LANGS)
            parts=[]
            for lang in AUDIENCE_LANGS:
                tr = GoogleTranslator(source="auto", target=lang).translate(text)
                parts.append(f"{lang.upper()}: {tr}")
            await msg.reply_text("🗣️ Traducción de la modelo:\n" + "\n".join(parts))
        else:
            # Mensaje de usuario -> traducir para la modelo (ES y DE por defecto)
            parts=[]
            for lang in AUDIENCE_LANGS:
                tr = GoogleTranslator(source="auto", target=lang).translate(text)
                parts.append(f"{lang.upper()}: {tr}")
            await msg.reply_text("👥 Traducción para la modelo:\n" + "\n".join(parts))
    except Exception as e:
        log.info(f"translate fail: {e}")

# --- Mensajes del canal (posts) -> actualizar actividad y rotación de anuncios ---
async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    post = update.channel_post
    if not post: return
    # Mirror al overlay también
    try:
        if post.text:
            push_overlay(f"📢 Canal: {post.text}")
    except Exception: pass

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)

# ========= Utils =========
def _in_quiet_hours(now_utc: datetime) -> bool:
    if not QUIET_HOURS: return False
    try:
        start_s, end_s = QUIET_HOURS.split("-")
        start_h, end_h = int(start_s), int(end_s)
        h = now_utc.hour
        if start_h <= end_h:  return start_h <= h < end_h
        else:                 return h >= start_h or h < end_h
    except Exception:
        return False

# ========= Scheduler =========
async def tick(app):
    global last_activity, last_ad
    while True:
        try:
            now = datetime.utcnow()
            active = AUTO_MARKETING or LIVE_FORCED or (now - last_activity) < timedelta(minutes=15)
            due = (now - last_ad) >= timedelta(minutes=ANNOUNCE_EVERY_MIN)
            quiet = _in_quiet_hours(now)
            cooldown = (now - last_activity) < timedelta(seconds=SUPPRESS_AFTER_DONATION_SEC)
            if active and due and (not quiet) and CHANNEL_ID and (not cooldown):
                await announce_prices(app.bot, int(CHANNEL_ID))
            await asyncio.sleep(20)
        except Exception as e:
            log.error(f"scheduler: {e}"); await asyncio.sleep(5)

async def on_startup(app):
    app.create_task(tick(app))
    log.info(f"✅ Scheduler cada {ANNOUNCE_EVERY_MIN} min | AUTO_MARKETING={'on' if AUTO_MARKETING else 'off'}")

# ========= SINGLETON =========
_app_singleton = None
def telegram_app_singleton():
    global _app_singleton
    if _app_singleton: return _app_singleton

    _app_singleton = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(on_startup)
        .build()
    )

    # Comandos
    _app_singleton.add_handler(CommandHandler("start", start_cmd))
    _app_singleton.add_handler(CommandHandler("menu", menu_cmd))
    _app_singleton.add_handler(CommandHandler("precios", precios_cmd))
    _app_singleton.add_handler(CommandHandler("studio", studio_cmd))
    _app_singleton.add_handler(CommandHandler("overlayon", overlay_on))
    _app_singleton.add_handler(CommandHandler("overlayoff", overlay_off))
    _app_singleton.add_handler(CommandHandler("liveon", liveon))
    _app_singleton.add_handler(CommandHandler("liveoff", liveoff))

    # Mensajes en grupos/supergrupos/canales (no-bot)
    _app_singleton.add_handler(MessageHandler(
        (filters.TEXT & (~filters.ChatType.PRIVATE)),
        group_msg
    ))

    # Posts del canal
    _app_singleton.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))

    _app_singleton.add_error_handler(on_error)
    return _app_singleton

# ========= MAIN =========
def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    app = telegram_app_singleton()
    t = threading.Thread(target=run_web, daemon=True); t.start()
    log.info("🤖 Iniciando bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
