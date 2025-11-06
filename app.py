import os, io, json, time, logging, asyncio
from threading import Thread
from typing import Dict, Any, List, Optional
from queue import Queue

from flask import Flask, request, Response

# --- Telegram ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackContext, filters
)

# --- Stripe ---
import stripe

# --- Imagen (Pillow) ---
from PIL import Image, ImageDraw, ImageFont

# =========================
# Configuración
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))           # -100xxxxxxxxxx
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")     # sin @
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "EUR")

# Puede venir como STRIPE_SECRET o STRIPE_SECRET_KEY
STRIPE_SECRET = os.getenv("STRIPE_SECRET") or os.getenv("STRIPE_SECRET_KEY", "")
stripe.api_key = STRIPE_SECRET

# Admins vía ENV (coma separada)
ADMIN_USER_IDS_ENV = os.getenv("ADMIN_USER_IDS", "").strip()

PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("cosplaylive")

# =========================
# Estado en disco
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

def _parse_admins_env() -> List[int]:
    return [int(x) for x in ADMIN_USER_IDS_ENV.split(",") if x.strip().isdigit()]

def load_state() -> Dict[str, Any]:
    """Carga estado; fusiona admins del ENV aunque exista el archivo."""
    base = {
        "admins": [],
        "model_name": "Cosplay Emma",
        "langs": ["de","en","es","pl"],
        "marketing_on": False,
        "prices": [["Besito",1],["Cariño",3],["Te amo",5],["Regalito",7]],
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                base.update(json.load(f) or {})
        except Exception as e:
            log.warning(f"No se pudo leer data.json: {e}")

    # Fusionar admins de ENV siempre
    seed_env = _parse_admins_env()
    current = set(int(x) for x in base.get("admins", []))
    for a in seed_env:
        current.add(a)
    base["admins"] = sorted(current)
    return base

def save_state(s: Dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

STATE = load_state()

# =========================
# Telegram Application (singleton)
# =========================
_app_singleton: Optional[Application] = None
def telegram_app_singleton() -> Application:
    global _app_singleton
    if _app_singleton is None:
        _app_singleton = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    return _app_singleton

# =========================
# Overlay (SSE)
# =========================
class EventBus:
    def __init__(self): self._subs: List[Queue] = []
    def subscribe(self) -> Queue:
        q: Queue = Queue(); self._subs.append(q); return q
    def unsubscribe(self, q: Queue):
        try: self._subs.remove(q)
        except ValueError: pass
    def push(self, payload: Dict[str, Any]):
        dead = []
        for q in list(self._subs):
            try: q.put_nowait(payload)
            except Exception: dead.append(q)
        for d in dead:
            try: self._subs.remove(d)
            except ValueError: pass

EVENTS = EventBus()

def _center(draw, txt, font, y, width=1200):
    l,t,r,b = draw.textbbox((0,0), txt, font=font)
    return ((width - (r-l))//2, y)

def build_card(title: str, subtitle: str) -> bytes:
    W,H = 1200,500
    img = Image.new("RGB",(W,H),(8,12,22))
    d = ImageDraw.Draw(img)
    try:
        f1 = ImageFont.truetype("DejaVuSans-Bold.ttf", 68)
        f2 = ImageFont.truetype("DejaVuSans.ttf", 44)
    except Exception:
        f1 = f2 = ImageFont.load_default()
    d.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    d.text(_center(d,title,f1,140), title, font=f1, fill=(255,255,255))
    d.text(_center(d,subtitle,f2,260), subtitle, font=f2, fill=(190,220,255))
    buf=io.BytesIO(); img.save(buf,"PNG"); buf.seek(0); return buf.read()

async def celebrate(bot, chat_id: int, payer_name: str, amount: str, memo: str):
    title = f"{payer_name} apoyó {amount}"
    png = build_card(title, memo)
    msg = await bot.send_message(chat_id, f"💝 *{payer_name}* apoyó *{amount}*.\n_{memo}_",
                                 parse_mode=ParseMode.MARKDOWN)
    await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    await bot.send_photo(chat_id, png, caption=f"Gracias {payer_name} 🫶")
    EVENTS.push({"type":"donation","data":{"payer":payer_name,"amount":amount,"memo":memo},"ts":int(time.time())})

def kb_donaciones(user=None) -> InlineKeyboardMarkup:
    rows=[]; uid=uname=""
    if user:
        uid=getattr(user,"id","")
        uname=getattr(user,"username","") or ""
    base=f"{BASE_URL}/donar"
    def url_for(price):
        q=f"?amt={price}&c={CURRENCY}"
        if uid: q+=f"&uid={uid}"
        if uname: q+=f"&uname={uname}"
        return base+q
    for name,price in STATE.get("prices",[]):
        rows.append([InlineKeyboardButton(f"{name} · {price} {CURRENCY}", url=url_for(price))])
    q=f"?c={CURRENCY}"
    if uid: q+=f"&uid={uid}"
    if uname: q+=f"&uname={uname}"
    rows.append([InlineKeyboardButton("💝 Donar libre", url=base+q)])
    return InlineKeyboardMarkup(rows)

def _env_admin_ids_set() -> set:
    return set(str(x).strip() for x in ADMIN_USER_IDS_ENV.split(",") if x.strip())

def is_admin(uid:int)->bool:
    # Admin si:
    # - está en data.json, o
    # - su id aparece en ADMIN_USER_IDS (ENV)
    env_ok = str(uid) in _env_admin_ids_set()
    file_ok = uid in STATE.get("admins", [])
    return env_ok or file_ok

def set_marketing(on:bool):
    STATE["marketing_on"]=on; save_state(STATE)

# =========================
# Handlers
# =========================
async def cmd_start(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        f"Hola {update.effective_user.first_name or ''} 👋\n"
        f"Asistente de *{STATE.get('model_name')}*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_donaciones(update.effective_user)
    )

async def cmd_menu(update: Update, ctx: CallbackContext):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones(update.effective_user))

async def cmd_whoami(update: Update, ctx: CallbackContext):
    u = update.effective_user
    uid = u.id
    uname = f"@{u.username}" if u.username else "(sin username)"
    await update.message.reply_text(f"Tu user_id: {uid}\nUsername: {uname}")

async def cmd_admins(update: Update, ctx: CallbackContext):
    ids_file = STATE.get("admins", [])
    ids_env = sorted(_env_admin_ids_set())
    txt = "Admins (archivo): " + (", ".join(str(x) for x in ids_file) or "—")
    txt += "\nAdmins (ENV): " + (", ".join(ids_env) or "—")
    await update.message.reply_text(txt)

async def cmd_iamadmin(update: Update, ctx: CallbackContext):
    uid=update.effective_user.id
    if uid not in STATE["admins"]:
        STATE["admins"].append(uid); save_state(STATE)
    await update.message.reply_text("✅ Ya eres admin de este bot.")

async def _admin_guard(update: Update): 
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return False
    return True

async def cmd_liveon(update: Update, ctx: CallbackContext):
    if not await _admin_guard(update): return
    set_marketing(True); await update.message.reply_text("🟢 LIVE activado.")

async def cmd_liveoff(update: Update, ctx: CallbackContext):
    if not await _admin_guard(update): return
    set_marketing(False); await update.message.reply_text("🔴 LIVE desactivado.")

async def cmd_addprice(update: Update, ctx: CallbackContext):
    if not await _admin_guard(update): return
    arg=update.message.text.split(" ",1)[-1].strip()
    if "·" not in arg: return await update.message.reply_text("Usa: /addprice Nombre · 7")
    name,price=[s.strip() for s in arg.split("·",1)]
    try: price=float(price)
    except: return await update.message.reply_text("Precio inválido.")
    STATE["prices"].append([name,price]); save_state(STATE)
    await update.message.reply_text("✅ Precio agregado.")

async def cmd_delprice(update: Update, ctx: CallbackContext):
    if not await _admin_guard(update): return
    name=update.message.text.split(" ",1)[-1].strip()
    before=len(STATE["prices"])
    STATE["prices"]=[p for p in STATE["prices"] if p[0]!=name]; save_state(STATE)
    await update.message.reply_text("✅ Eliminado." if len(STATE["prices"])<before else "No encontrado.")

async def cmd_listprices(update: Update, ctx: CallbackContext):
    lines=[f"• {n} · {v} {CURRENCY}" for n,v in STATE.get("prices",[])]
    await update.message.reply_text("Precios actuales:\n"+"\n".join(lines))

# Marketing en grupos
async def on_group_text(update: Update, ctx: CallbackContext):
    if STATE.get("marketing_on"):
        await ctx.bot.send_message(
            update.effective_chat.id,
            f"💝 Apoya a *{STATE.get('model_name')}* y aparece en pantalla.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_donaciones()
        )

# LIVE START/END — grupos
async def on_group_live_start(update: Update, ctx: CallbackContext):
    set_marketing(True)
    await ctx.bot.send_message(update.effective_chat.id, "🔴 LIVE detectado (grupo). Marketing activado.")

async def on_group_live_end(update: Update, ctx: CallbackContext):
    set_marketing(False)
    await ctx.bot.send_message(update.effective_chat.id, "⚫️ LIVE finalizado. Marketing detenido.")

# LIVE START/END — canales (PTB 20.8)
async def on_channel_live_start(update: Update, ctx: CallbackContext):
    set_marketing(True)
    await ctx.bot.send_message(CHANNEL_ID, "🔴 LIVE detectado (canal). Marketing activado.")

async def on_channel_live_end(update: Update, ctx: CallbackContext):
    set_marketing(False)
    await ctx.bot.send_message(CHANNEL_ID, "⚫️ LIVE finalizado. Marketing detenido.")

# =========================
# Flask (web)
# =========================
web = Flask(__name__)

@web.get("/")
def index(): return "CosplayLive bot OK"

@web.get("/overlay")
def overlay_page():
    return """
<!doctype html><meta charset="utf-8">
<title>Overlay</title>
<style>body{background:#0b1020;color:#fff;font-family:system-ui;margin:0}
.event{padding:16px;margin:12px;border-radius:14px;background:#16213a}
.big{font-size:22px;font-weight:700}</style>
<div id="log"></div>
<script>
 const log=document.getElementById('log');
 const ev=new EventSource('/events');
 const ding=new Audio('https://actions.google.com/sounds/v1/cartoon/clang_and_wobble.ogg');
 ev.onmessage=(m)=>{
   const o=JSON.parse(m.data);
   if(o.type==='donation'){try{ding.currentTime=0;ding.play();}catch(e){}}
   const d=document.createElement('div'); d.className='event';
   d.innerHTML=`<div class="big">${o.type.toUpperCase()}</div><div>${JSON.stringify(o.data)}</div>`;
   log.prepend(d);
 };
</script>"""

@web.get("/events")
def sse_events():
    q = EVENTS.subscribe()
    def stream():
        try:
            while True:
                item = q.get()
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            EVENTS.unsubscribe(q)
    return Response(stream(), mimetype="text/event-stream")

@web.get("/studio")
def studio_page():
    return f"""<!doctype html><meta charset="utf-8">
<h2>Studio – {STATE.get('model_name')}</h2>
<p><a href="{BASE_URL}/overlay" target="_blank">Abrir Overlay</a></p>
<form method="post" action="{BASE_URL}/studio/ding"><button>🔔 Probar sonido</button></form>"""

@web.post("/studio/ding")
def studio_ding():
    EVENTS.push({"type":"donation","data":{"payer":"TestUser","amount":"0.00","memo":"Test"},
                 "ts":int(time.time())})
    return "<p>OK (revisa el Overlay).</p>"

@web.get("/donar")
def donate_page():
    amt = request.args.get("amt","")
    ccy = request.args.get("c", CURRENCY)
    uid = request.args.get("uid",""); uname = request.args.get("uname","")
    title = f"Apoyo a {STATE.get('model_name')}"
    if amt and amt.replace(".","",1).isdigit():
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data":{
                "currency":ccy.lower(),
                "product_data":{"name":title},
                "unit_amount":int(float(amt)*100)}, "quantity":1}],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"channel_id":str(CHANNEL_ID),"amount":f"{amt} {ccy}","uid":uid,"uname":uname},
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">'
    return "<h3>Monto inválido</h3>"

@web.get("/ok")
def ok_page():
    chan = CHANNEL_USERNAME.strip()
    tg_link = f"tg://resolve?domain={chan}" if chan else ""
    btn = f'<p><a href="{tg_link}">Volver a Telegram</a></p>' if tg_link else ""
    return f"<h2>✅ Pago recibido</h2><p>Pronto verás el anuncio en el canal.</p>{btn}"

@web.get("/cancel")
def cancel_page(): return "<h3>Pago cancelado</h3>"

@web.post("/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature","")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET","")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret) if secret else json.loads(payload)
    except Exception as e:
        log.error(f"stripe webhook error: {e}"); return ("",400)

    if event.get("type") == "checkout.session.completed":
        sess = event["data"]["object"]; md = (sess.get("metadata") or {})
        uname = (md.get("uname") or "").strip()
        payer_name = f"@{uname}" if uname else "Supporter"
        amount = md.get("amount") or f"{(sess.get('amount_total') or 0)/100:.2f} {(sess.get('currency') or '').upper()}"
        memo = "¡Gracias por tu apoyo!"
        app = telegram_app_singleton()
        asyncio.run_coroutine_threadsafe(
            celebrate(app.bot, int(CHANNEL_ID), payer_name, amount, memo),
            app.loop
        )
    return ("",200)

# =========================
# Arranque
# =========================
def run_flask(): web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    app = telegram_app_singleton()

    # Comandos
    app.add_handler(CommandHandler(["start","help"], cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("iamadmin", cmd_iamadmin))
    app.add_handler(CommandHandler("liveon", cmd_liveon))
    app.add_handler(CommandHandler("liveoff", cmd_liveoff))
    app.add_handler(CommandHandler("addprice", cmd_addprice))
    app.add_handler(CommandHandler("delprice", cmd_delprice))
    app.add_handler(CommandHandler("listprices", cmd_listprices))

    # Grupo: texto y LIVE start/end
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_text))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.VIDEO_CHAT_STARTED, on_group_live_start))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.VIDEO_CHAT_ENDED, on_group_live_end))

    # Canal: LIVE start/end (PTB 20.8)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.StatusUpdate.VIDEO_CHAT_STARTED, on_channel_live_start))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.StatusUpdate.VIDEO_CHAT_ENDED, on_channel_live_end))

    # Lanzar Flask en hilo
    Thread(target=run_flask, daemon=True).start()
    log.info("Starting polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
