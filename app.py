# app.py — CosplayLive ULTRA (Overlay + Assistant + Stripe + Editable Prices + i18n)
# Requiere requirements.txt:
# python-telegram-bot==20.8
# Flask==3.0.3
# stripe==9.11.0
# deep-translator==1.11.4
# Pillow==10.4.0

import os, sys, threading, logging, queue, io, html, json
from datetime import datetime, timedelta
from typing import List, Dict, Any

from flask import Flask, Response, request

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
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
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()            # -100xxxxxxxxxx
BASE_URL = os.getenv("BASE_URL", "").strip()                # https://tuapp.onrender.com
STRIPE_SK = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WH = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CURRENCY = os.getenv("CURRENCY", "EUR")

# Marketing
ANNOUNCE_EVERY_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN", "5"))
AUTO_MARKETING = os.getenv("AUTO_MARKETING", "on").lower() in ("1","on","true","yes")
QUIET_HOURS = os.getenv("QUIET_HOURS", "")

# Control
ADMIN_IDS = [s.strip() for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()]
MODEL_USER_IDS = [s.strip() for s in os.getenv("MODEL_USER_IDS", "").split(",") if s.strip()]
DEFAULT_AUDIENCE_LANGS = [s.strip() for s in os.getenv("AUDIENCE_LANGS", "de,en,sv,pl,es").split(",") if s.strip()]

if not TOKEN:
    raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")
if not STRIPE_SK:
    log.warning("⚠️ Falta STRIPE_SECRET_KEY (solo pruebas de UI)")
if not PIL_OK:
    log.warning("⚠️ Pillow no disponible: tarjetas gráficas desactivadas temporalmente.")

stripe.api_key = STRIPE_SK

# ========= RUTAS DE PERSISTENCIA =========
DATA_DIR = "/mnt/data"  # persiste en Render/containers entre reinicios, no entre imágenes
DATA_PATH = os.path.join(DATA_DIR, "data.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ========= I18N =========
SUPPORTED_LANGS = ["de","en","es","pl","sv"]
GREETS = {
    "de": {"hallo","hi","servus","moin","hey"},
    "en": {"hello","hi","hey","sup"},
    "es": {"hola","buenas","holi","hey"},
    "pl": {"cześć","hej","siema"},
    "sv": {"hej","tjena","hallå"}
}
# Textos fijos (mínimos). Llave -> traducciones
I18N: Dict[str, Dict[str,str]] = {
    "assistant.greet":
        {"de":"🤖 *Assistent der Model*: Schreib deine Frage oder wähle unten.",
         "en":"🤖 *Model’s assistant*: Type your request or use the buttons below.",
         "es":"🤖 *Asistente de la modelo*: Escribe tu pedido o usa los botones.",
         "pl":"🤖 *Asystent modelki*: Napisz prośbę albo użyj przycisków poniżej.",
         "sv":"🤖 *Modellens assistent*: Skriv din förfrågan eller använd knapparna."},
    "assistant.about":
        {"de":"ℹ️ *Über das Model:* {bio}",
         "en":"ℹ️ *About the model:* {bio}",
         "es":"ℹ️ *Sobre la modelo:* {bio}",
         "pl":"ℹ️ *O modelce:* {bio}",
         "sv":"ℹ️ *Om modellen:* {bio}"},
    "assistant.menu_title":
        {"de":"✨ *Wunsch-/Trinkgeldmenü*",
         "en":"✨ *Tip & Request Menu*",
         "es":"✨ *Menú de apoyos y pedidos*",
         "pl":"✨ *Menu napiwków i próśb*",
         "sv":"✨ *Dricks- & önskemålmeny*"},
    "assistant.cta":
        {"de":"Zahl mit Karte, Apple/Google Pay oder lokalen Methoden (Stripe).",
         "en":"Pay with card, Apple/Google Pay or local methods (Stripe).",
         "es":"Paga con tarjeta, Apple/Google Pay o métodos locales (Stripe).",
         "pl":"Płać kartą, Apple/Google Pay lub lokalnymi metodami (Stripe).",
         "sv":"Betala med kort, Apple/Google Pay eller lokala metoder (Stripe)."},
    "assistant.translation_from_model":
        {"de":"🗣️ *Übersetzung vom Model:*",
         "en":"🗣️ *Model’s translation:*",
         "es":"🗣️ *Traducción de la modelo:*",
         "pl":"🗣️ *Tłumaczenie od modelki:*",
         "sv":"🗣️ *Modellens översättning:*"},
    "assistant.translation_for_model":
        {"de":"👥 *Übersetzung für das Model:*",
         "en":"👥 *Translation for the model:*",
         "es":"👥 *Traducción para la modelo:*",
         "pl":"👥 *Tłumaczenie dla modelki:*",
         "sv":"👥 *Översättning för modellen:*"},
    "assistant.open_studio":
        {"de":"🎛️ Studio: {url}",
         "en":"🎛️ Studio: {url}",
         "es":"🎛️ Studio: {url}",
         "pl":"🎛️ Studio: {url}",
         "sv":"🎛️ Studio: {url}"},
    "overlay.on":{"de":"🟢 Overlay aktiviert","en":"🟢 Overlay ON","es":"🟢 Overlay activado","pl":"🟢 Overlay włączony","sv":"🟢 Overlay på"},
    "overlay.off":{"de":"🔴 Overlay deaktiviert","en":"🔴 Overlay OFF","es":"🔴 Overlay desactivado","pl":"🔴 Overlay wyłączony","sv":"🔴 Overlay av"},
    "menu.item":
        {"de":"• {name} — *{amt}* {ccy}",
         "en":"• {name} — *{amt}* {ccy}",
         "es":"• {name} — *{amt}* {ccy}",
         "pl":"• {name} — *{amt}* {ccy}",
         "sv":"• {name} — *{amt}* {ccy}"},
    "announce.header":
        {"de":"🚀 *Show live:* nutze das Menü unten",
         "en":"🚀 *Live show:* use the menu",
         "es":"🚀 *Show en vivo:* usa el menú",
         "pl":"🚀 *Na żywo:* użyj menu",
         "sv":"🚀 *Liveshow:* använd menyn"},
    "thanks.title":
        {"de":"Danke {user}!","en":"Thanks {user}!","es":"¡Gracias {user}!","pl":"Dziękujemy {user}!","sv":"Tack {user}!"},
    "thanks.subtitle":
        {"de":"Support: {amount}","en":"Support: {amount}","es":"Apoyo: {amount}","pl":"Wsparcie: {amount}","sv":"Stöd: {amount}"},
    "thanks.message":
        {"de":"🎉 *Danke, {user}!* Du hast *{amount}* gespendet.\n_{memo}_",
         "en":"🎉 *Thanks, {user}!* You supported with *{amount}*.\n_{memo}_",
         "es":"🎉 *¡Gracias, {user}!* Has apoyado con *{amount}*.\n_{memo}_",
         "pl":"🎉 *Dzięki, {user}!* Wsparłeś/aś *{amount}*.\n_{memo}_",
         "sv":"🎉 *Tack, {user}!* Du stödde med *{amount}*.\n_{memo}_"},
}

def t(lang: str, key: str, **kw) -> str:
    lang = (lang or "en").split("-")[0]
    if lang not in SUPPORTED_LANGS: lang = "en"
    val = I18N.get(key, {}).get(lang) or I18N.get(key, {}).get("en") or key
    if kw:
        try: val = val.format(**kw)
        except Exception: pass
    return val

def detect_lang(text: str) -> str:
    try:
        code = GoogleTranslator(source="auto", target="en").detect(text)  # returns 'de','es',...
        code = (code or "en").split("-")[0]
    except Exception:
        code = "en"
    if code not in SUPPORTED_LANGS:
        code = "en"
    return code

# ========= ESTADO =========
LIVE_FORCED = True
last_activity = datetime.utcnow() - timedelta(hours=1)
last_ad = datetime.utcnow() - timedelta(hours=1)
SUPPRESS_AFTER_DONATION_SEC = 90
OVERLAY_ENABLED = True

# ========= CONFIG (persistente) =========
DEFAULT_CONFIG = {
    "prices": [  # lista de dicts {"name":..., "price": int}
        {"name":"💃 Dance", "price":3},
        {"name":"👗 Lingerie try-on", "price":10},
        {"name":"🙈 Topless", "price":5},
        {"name":"🎯 Group goal", "price":50},
    ],
    "bio": "Cosplay europe@DE/CH/SE/PL/ES. Shows y pedidos con menú dinámico.",
    "audience_langs": DEFAULT_AUDIENCE_LANGS,  # para traducción dirigida
    "rotation_texts": {
        "de": ["✨ *Wunsch-/Trinkgeldmenü*", "🎯 *Gruppenziel:* bei 50 EUR spezieller Show", "💡 Tipp: Du kannst eine Nachricht in der Spende lassen.", "🔥 Danke für euren Support!"],
        "en": ["✨ *Tip & Request Menu*", "🎯 *Group goal:* 50 EUR unlocks special show", "💡 Tip: Leave a message with your support.", "🔥 Thanks for supporting!"],
        "es": ["✨ *Menú de apoyos y pedidos*", "🎯 *Meta grupal:* a 50 EUR desbloqueamos show especial", "💡 Tip: deja mensaje en tu apoyo.", "🔥 ¡Gracias por apoyar!"],
        "pl": ["✨ *Menu napiwków i próśb*", "🎯 *Cel grupowy:* 50 EUR = specjalny show", "💡 Tip: dodaj wiadomość do wpłaty.", "🔥 Dzięki za wsparcie!"],
        "sv": ["✨ *Dricks- & önskemålmeny*", "🎯 *Gruppmål:* 50 EUR = specialshow", "💡 Tips: lämna meddelande i din gåva.", "🔥 Tack för stödet!"],
    }
}

def load_config() -> Dict[str, Any]:
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                # sane defaults:
                if "prices" not in cfg: cfg["prices"] = DEFAULT_CONFIG["prices"]
                if "bio" not in cfg: cfg["bio"] = DEFAULT_CONFIG["bio"]
                if "audience_langs" not in cfg: cfg["audience_langs"] = DEFAULT_CONFIG["audience_langs"]
                if "rotation_texts" not in cfg: cfg["rotation_texts"] = DEFAULT_CONFIG["rotation_texts"]
                return cfg
        except Exception as e:
            log.error(f"load_config error: {e}")
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_config error: {e}")

CFG = load_config()

# ========= COLAS SSE =========
events_studio: "queue.Queue[str]" = queue.Queue(maxsize=300)
events_overlay: "queue.Queue[str]" = queue.Queue(maxsize=600)

def _push(q: "queue.Queue[str]", text: str):
    text = (text or "").replace("\n", " ").strip()
    if not text: return
    try: q.put_nowait(text)
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
    return "✅ CosplayLive ULTRA corriendo"

# ----- Studio (modelo) -----
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
const box=document.getElementById('events');const ding=document.getElementById('ding');
const es=new EventSource('/events');
es.onmessage=(e)=>{const d=document.createElement('div');d.className='event';d.textContent=e.data;
box.prepend(d);try{ding.currentTime=0;ding.play();}catch(_){}};</script></body></html>"""

@web.get("/events")
def sse_studio():
    def stream():
        while True:
            msg = events_studio.get()
            yield f"data: {msg}\n\n"
    headers = {"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)

# ----- Overlay público -----
@web.get("/overlay")
def overlay_only():
    return """
<!doctype html><html><head><meta charset="utf-8"><title>Overlay</title>
<style>
html,body{background:rgba(0,0,0,0);margin:0;overflow:hidden}
#stack{position:fixed;left:0;right:0;bottom:10px;display:flex;flex-direction:column-reverse;gap:10px;padding:10px;pointer-events:none}
.msg{align-self:center;max-width:88vw;background:rgba(0,0,0,.55);color:#fff;border-radius:18px;padding:12px 18px;
font:600 22px system-ui,Segoe UI,Roboto,Arial;box-shadow:0 8px 30px rgba(0,0,0,.45);animation:pop .25s ease-out}
@keyframes pop{from{transform:scale(.95);opacity:.2}to{transform:scale(1);opacity:1}}
</style></head><body>
<div id="stack"></div>
<script>
const st=document.getElementById('stack'); const es=new EventSource('/overlay-events');
es.onmessage=(e)=>{const div=document.createElement('div');div.className='msg';
div.textContent=e.data; st.append(div); setTimeout(()=>div.remove(), 12000);};
</script></body></html>
    """

@web.get("/liveview")
def liveview():
    src = request.args.get("src","")
    safe = html.escape(src)
    video_html = f'<video src="{safe}" autoplay playsinline controls style="width:100%;height:auto;background:#000"></video>' if src else "<div style='background:#000;height:48vh;border-radius:14px'></div>"
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
    <div>🔴 Vista pública con chat superpuesto</div>
    <div><button class="btn" id="toggle">Ocultar overlay</button></div>
  </div>
  <div id="stage">{video_html}
    <div id="overlay"><div id="stack"></div></div>
  </div>
</div>
<script>
let on=true; const btn=document.getElementById('toggle'); const st=document.getElementById('stack');
btn.onclick=()=>{{on=!on; btn.textContent=on?'Ocultar overlay':'Mostrar overlay';}};
const es=new EventSource('/overlay-events');
es.onmessage=(e)=>{{ if(!on) return; const d=document.createElement('div'); d.className='msg';
d.textContent=e.data; st.append(d); setTimeout(()=>d.remove(),12000); }};
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

# ----- Stripe / Donar -----
def build_card(title: str, subtitle: str):
    if not PIL_OK: return None
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

def kb_donaciones(lang: str) -> InlineKeyboardMarkup:
    rows = []
    for item in CFG["prices"]:
        name = item["name"]; price = item["price"]
        rows.append([InlineKeyboardButton(f"{name} · {price} {CURRENCY}",
                                          url=f"{BASE_URL}/donar?amt={price}&c={CURRENCY}")])
    rows.append([InlineKeyboardButton("💝 " + {"de":"Freier Betrag","en":"Free amount","es":"Importe libre","pl":"Dowolna kwota","sv":"Valfritt belopp"}[lang],
                                      url=f"{BASE_URL}/donar")])
    return InlineKeyboardMarkup(rows)

@web.get("/donar")
def donate_page():
    amt = request.args.get("amt","")
    ccy = request.args.get("c", CURRENCY)
    title = "Support CosplayLive"
    if not STRIPE_SK or not BASE_URL:
        return "<b>Stripe no está configurado</b> (STRIPE_SECRET_KEY/BASE_URL)."
    # checkout directo
    if amt.isdigit() and int(amt) > 0:
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
            allow_promotion_codes=True,
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    # formulario libre
    options = "".join([f'<a href="/donar?amt={p["price"]}&c={ccy}">{html.escape(p["name"])} · {p["price"]} {ccy}</a><br>' for p in CFG["prices"]])
    return f"""
<!doctype html><html><head><meta charset="utf-8"><title>Donate</title>
<style>body{{font-family:system-ui;padding:20px;max-width:640px;margin:auto}}</style></head><body>
<h3>Choose amount</h3>
<form method="get" action="/donar">
  <input type="hidden" name="c" value="{ccy}">
  <input name="amt" type="number" min="1" step="1" value="5" style="padding:8px"> {ccy}
  <button type="submit" style="padding:8px 12px">Pay</button>
</form>
<p>Or quick select:</p>
{options}
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
        memo = "Gracias por tu apoyo"
        try:
            app = telegram_app_singleton()
            app.create_task(celebrate(app.bot, int(CHANNEL_ID), payer, amount, memo))
        except Exception as e:
            log.error(f"No se pudo anunciar en TG: {e}")
        log.info(f"✅ Stripe: pago recibido — {amount}")
    return "ok", 200

# ========= TELEGRAM =========
def _rot_text(lang_cycle: List[str]) -> str:
    # rota textos por idiomas para anuncios
    now_idx = int(datetime.utcnow().timestamp() // (ANNOUNCE_EVERY_MIN*60)) % len(lang_cycle)
    lang = lang_cycle[now_idx]
    arr = CFG["rotation_texts"].get(lang) or CFG["rotation_texts"]["en"]
    return (arr[now_idx % len(arr)]), lang

async def announce_prices(bot, chat_id: int, lang_hint: str | None = None):
    global last_ad
    # rotamos por audiencias si no hay pista
    lang_cycle = CFG.get("audience_langs") or DEFAULT_AUDIENCE_LANGS
    lang = (lang_hint or (lang_cycle[0] if lang_cycle else "en")).split("-")[0]
    if lang not in SUPPORTED_LANGS: lang = "en"
    header, chosen_lang = _rot_text(lang_cycle)
    # construir texto de menú
    items = "\n".join([t(chosen_lang, "menu.item", name=p["name"], amt=p["price"], ccy=CURRENCY) for p in CFG["prices"]])
    text = f"{header}\n\n{items}\n\n{t(chosen_lang,'assistant.cta')}"
    await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones(chosen_lang))
    last_ad = datetime.utcnow()

async def celebrate(bot, chat_id: int, user: str, amount: str, memo: str):
    global last_activity, last_ad
    # idioma base alemán para canal; puedes cambiar a "en"
    lang = "de"
    msg = await bot.send_message(chat_id, t(lang,"thanks.message", user=user, amount=amount, memo=memo), parse_mode=ParseMode.MARKDOWN)
    # pin temporal
    try:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        await asyncio.sleep(15); await bot.unpin_chat_message(chat_id, msg.message_id)
    except Exception as e:
        log.info(f"Pin opcional: {e}")
    # título temporal
    try:
        old = (await bot.get_chat(chat_id)).title or ""
        new = f"🔥 {t(lang,'thanks.title', user=user)} ({amount})"
        await bot.set_chat_title(chat_id, new); await asyncio.sleep(15); await bot.set_chat_title(chat_id, old)
    except Exception as e:
        log.info(f"Título opcional: {e}")
    # tarjeta gráfica
    buf = build_card(t(lang,'thanks.title', user=user), t(lang,'thanks.subtitle', amount=amount))
    if buf: await bot.send_photo(chat_id, photo=InputFile(buf, filename="thanks.png"))
    push_studio(f"🎉 Donación: {user} → {amount}")
    push_overlay(f"🎉 {user}: {amount}")
    last_activity = datetime.utcnow(); last_ad = datetime.utcnow()

# ---- Comandos de admin (editar menú, bio, idiomas) ----
def is_admin(user_id: int) -> bool:
    return (str(user_id) in ADMIN_IDS) or (str(user_id) in MODEL_USER_IDS)

async def addprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Uso: /addprice <importe> <nombre>")
        return
    try:
        amt = int(context.args[0])
        name = " ".join(context.args[1:]).strip()
        if amt <= 0 or not name: raise ValueError()
        CFG["prices"].append({"name":name, "price":amt})
        save_config(CFG)
        await update.message.reply_text(f"✅ Añadido: {name} · {amt} {CURRENCY}")
    except Exception:
        await update.message.reply_text("Formato inválido. Ej: /addprice 3 Beso")

async def delprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Uso: /delprice <índice|nombre>")
        return
    arg = " ".join(context.args).strip()
    removed = None
    # por índice
    if arg.isdigit():
        idx = int(arg)-1
        if 0 <= idx < len(CFG["prices"]):
            removed = CFG["prices"].pop(idx)
    else:
        for i,p in enumerate(CFG["prices"]):
            if p["name"].lower() == arg.lower():
                removed = CFG["prices"].pop(i); break
    if removed:
        save_config(CFG)
        await update.message.reply_text(f"🗑️ Eliminado: {removed['name']}")
    else:
        await update.message.reply_text("No encontrado.")

async def listprices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for i,p in enumerate(CFG["prices"], start=1):
        lines.append(f"{i}. {p['name']} · {p['price']} {CURRENCY}")
    await update.message.reply_text("Precios actuales:\n" + "\n".join(lines))

async def setlangs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Uso: /setlangs de,en,sv,pl,es")
        return
    arr = " ".join(context.args).replace(" ","").split(",")
    arr = [x for x in arr if x in SUPPORTED_LANGS]
    if not arr:
        await update.message.reply_text("Idiomas no válidos.")
        return
    CFG["audience_langs"] = arr; save_config(CFG)
    await update.message.reply_text("✅ Idiomas de traducción: " + ",".join(arr))

async def setbio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    text = (update.message.text or "").split(" ",1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text("Uso: /setbio <texto>")
        return
    CFG["bio"] = text[1].strip(); save_config(CFG)
    await update.message.reply_text("✅ Bio actualizada.")

async def exportconfig_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    data = json.dumps(CFG, ensure_ascii=False, indent=2).encode("utf-8")
    await update.message.reply_document(document=InputFile(io.BytesIO(data), filename="data.json"))

# ---- Otros comandos ----
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot listo. /menu /precios /studio /overlay /liveview /addprice /delprice /listprices /setbio /setlangs")

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # idioma del usuario
    txt = update.message.text or ""
    lang = detect_lang(txt)
    text = t(lang,"assistant.greet") + "\n" + t(lang,"assistant.about", bio=CFG["bio"])
    items = "\n".join([t(lang,"menu.item", name=p["name"], amt=p["price"], ccy=CURRENCY) for p in CFG["prices"]])
    text += "\n\n" + t(lang,"assistant.menu_title") + "\n\n" + items + "\n\n" + t(lang,"assistant.cta")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones(lang))

async def precios_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_cmd(update, context)

async def studio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t("en","assistant.open_studio", url=f"{BASE_URL}/studio"))

async def overlay_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OVERLAY_ENABLED; OVERLAY_ENABLED=True
    await update.message.reply_text(t("es","overlay.on"))

async def overlay_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OVERLAY_ENABLED; OVERLAY_ENABLED=False
    await update.message.reply_text(t("es","overlay.off"))

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LIVE_FORCED; LIVE_FORCED=True
    await update.message.reply_text("🟢 Marketing ON")

async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LIVE_FORCED; LIVE_FORCED=False
    await update.message.reply_text("🔴 Marketing OFF")

# ---- Mensajes en grupos/canales: asistente + mirror + traducción dirigida ----
def _is_greet(text: str) -> bool:
    low = text.lower().strip()
    for s in GREETS.values():
        if low in s: return True
    return False

async def group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    msg = update.message
    if not msg or not msg.text: return
    chat_id = update.effective_chat.id
    user = update.effective_user
    name = user.full_name if user else "User"
    text = msg.text.strip()
    last_activity = datetime.utcnow()

    # mirror al overlay (solo si estamos en el canal/grupo objetivo)
    try:
        if str(chat_id) == str(CHANNEL_ID) and not (user and user.is_bot):
            push_overlay(f"{name}: {text}")
    except Exception as e:
        log.info(f"overlay mirror: {e}")

    # asistente a saludos
    if _is_greet(text):
        lang = detect_lang(text)
        response = t(lang,"assistant.greet") + "\n" + t(lang,"assistant.about", bio=CFG["bio"])
        await msg.reply_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones(lang))
        return

    # traducción dirigida
    try:
        langs = CFG.get("audience_langs") or DEFAULT_AUDIENCE_LANGS
        if str(user.id) in MODEL_USER_IDS:
            # modelo → traducción para público
            parts = []
            for L in langs:
                tr = GoogleTranslator(source="auto", target=L).translate(text)
                parts.append(f"{L.upper()}: {tr}")
            await msg.reply_text(t("en","assistant.translation_from_model") + "\n" + "\n".join(parts))
        else:
            # usuario → traducción para la modelo
            parts = []
            for L in langs:
                tr = GoogleTranslator(source="auto", target=L).translate(text)
                parts.append(f"{L.upper()}: {tr}")
            await msg.reply_text(t("en","assistant.translation_for_model") + "\n" + "\n".join(parts))
    except Exception as e:
        log.info(f"translate fail: {e}")

async def channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_activity
    last_activity = datetime.utcnow()
    post = update.channel_post
    if not post: return
    if post.text:
        push_overlay(f"📢 {post.text}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("❌ Handler error", exc_info=context.error)

# ========= Utils =========
def _in_quiet_hours(now_utc: datetime) -> bool:
    if not QUIET_HOURS: return False
    try:
        start_s, end_s = QUIET_HOURS.split("-")
        start_h, end_h = int(start_s), int(end_s)
        h = now_utc.hour
        if start_h <= end_h:
            return start_h <= h < end_h
        else:
            return h >= start_h or h < end_h
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
    # comandos
    _app_singleton.add_handler(CommandHandler("start", start_cmd))
    _app_singleton.add_handler(CommandHandler("menu", menu_cmd))
    _app_singleton.add_handler(CommandHandler("precios", precios_cmd))
    _app_singleton.add_handler(CommandHandler("studio", studio_cmd))
    _app_singleton.add_handler(CommandHandler("overlayon", overlay_on))
    _app_singleton.add_handler(CommandHandler("overlayoff", overlay_off))
    _app_singleton.add_handler(CommandHandler("liveon", liveon))
    _app_singleton.add_handler(CommandHandler("liveoff", liveoff))
    _app_singleton.add_handler(CommandHandler("addprice", addprice_cmd))
    _app_singleton.add_handler(CommandHandler("delprice", delprice_cmd))
    _app_singleton.add_handler(CommandHandler("listprices", listprices_cmd))
    _app_singleton.add_handler(CommandHandler("setlangs", setlangs_cmd))
    _app_singleton.add_handler(CommandHandler("setbio", setbio_cmd))
    _app_singleton.add_handler(CommandHandler("exportconfig", exportconfig_cmd))
    # mensajes en grupos/supergrupos/canales (texto)
    _app_singleton.add_handler(MessageHandler(filters.TEXT & (~filters.ChatType.PRIVATE), group_msg))
    # posts del canal
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
