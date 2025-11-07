# app.py — CosplayLive (versión grupo + canal + traducción + precios + studio GET/POST)
import os, io, json, time, logging, asyncio, re
from threading import Thread
from typing import Dict, Any, List, Optional
from queue import Queue
from flask import Flask, request, Response

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationBuilder, CommandHandler,
                          MessageHandler, CallbackContext, filters)

import stripe
from PIL import Image, ImageDraw, ImageFont

# ===== Config =====
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN","")
CHANNEL_ID       = int(os.getenv("CHANNEL_ID","0"))     # -100xxxxxxxx
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME","")
BASE_URL         = os.getenv("BASE_URL","").rstrip("/")
CURRENCY         = os.getenv("CURRENCY","EUR")
ADMIN_USER_IDS   = os.getenv("ADMIN_USER_IDS","").strip()
STRIPE_SECRET    = os.getenv("STRIPE_SECRET") or os.getenv("STRIPE_SECRET_KEY","")
stripe.api_key   = STRIPE_SECRET
PORT             = int(os.getenv("PORT","10000"))
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO").upper())
log = logging.getLogger("cosplaylive")

# Traducción (opcional)
ENABLE_TRANSLATION = os.getenv("ENABLE_TRANSLATION","0") == "1"
if ENABLE_TRANSLATION:
    try:
        from deep_translator import GoogleTranslator
    except Exception:
        ENABLE_TRANSLATION = False

# ===== Estado en disco =====
DATA_DIR  = os.getenv("DATA_DIR","/var/data"); os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

def _parse_admins_env() -> List[int]:
    return [int(x) for x in ADMIN_USER_IDS.split(",") if x.strip().isdigit()]

def _default_state():
    return {
        "admins": [],
        "model_name": "Cosplay Emma",
        "model_id": 0,
        "langs": ["de","en","es","pl"],
        "marketing_on": False,
        "last_push_ts": 0,
        "prices": [["Goal 10s", 3], ["Kiss", 5], ["Song", 7], ["Dance", 10]],
    }

def load_state() -> Dict[str, Any]:
    st = _default_state()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE,"r",encoding="utf-8") as f: st.update(json.load(f) or {})
        except Exception as e: log.warning(f"data.json read: {e}")
    # fusionar admins ENV
    current = set(int(x) for x in st.get("admins",[]))
    for a in _parse_admins_env(): current.add(a)
    st["admins"] = sorted(current)
    return st

def save_state(st: Dict[str, Any]):
    tmp = DATA_FILE + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(st,f,ensure_ascii=False,indent=2)
    os.replace(tmp, DATA_FILE)

STATE = load_state()

# ===== Telegram app singleton =====
_app_singleton: Optional[Application] = None
def telegram_app_singleton() -> Application:
    global _app_singleton
    if _app_singleton is None:
        _app_singleton = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    return _app_singleton

# ===== Overlay (SSE) =====
class EventBus:
    def __init__(self): self._subs: List[Queue] = []
    def subscribe(self) -> Queue:
        q: Queue = Queue(); self._subs.append(q); return q
    def unsubscribe(self, q: Queue):
        try: self._subs.remove(q)
        except ValueError: pass
    def push(self, payload: Dict[str, Any]):
        for q in list(self._subs):
            try: q.put_nowait(payload)
            except Exception:
                try: self._subs.remove(q)
                except ValueError: pass

EVENTS = EventBus()

def _center(draw, txt, font, y, width=1200):
    l,t,r,b = draw.textbbox((0,0), txt, font=font); return ((width - (r-l))//2, y)

def build_card(title: str, subtitle: str) -> bytes:
    W,H=1200,500; img=Image.new("RGB",(W,H),(8,12,22)); d=ImageDraw.Draw(img)
    try:
        f1=ImageFont.truetype("DejaVuSans-Bold.ttf",68)
        f2=ImageFont.truetype("DejaVuSans.ttf",44)
    except: f1=f2=ImageFont.load_default()
    d.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    d.text(_center(d,title,f1,140), title, font=f1, fill=(255,255,255))
    d.text(_center(d,subtitle,f2,260), subtitle, font=f2, fill=(190,220,255))
    buf=io.BytesIO(); img.save(buf,"PNG"); buf.seek(0); return buf.read()

async def celebrate(bot, chat_id: int, payer_name: str, amount: str, memo: str):
    png = build_card(f"{payer_name} apoyó {amount}", memo)
    msg = await bot.send_message(chat_id, f"💝 *{payer_name}* apoyó *{amount}*.\n_{memo}_",
                                 parse_mode=ParseMode.MARKDOWN)
    await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    await bot.send_photo(chat_id, png, caption=f"Gracias {payer_name} 🫶")
    EVENTS.push({"type":"donation","data":{"payer":payer_name,"amount":amount,"memo":memo},"ts":int(time.time())})

def env_admin_set() -> set[str]:
    return set(str(x).strip() for x in ADMIN_USER_IDS.split(",") if x.strip())
def is_admin(uid: int) -> bool:
    return uid in STATE.get("admins",[]) or (str(uid) in env_admin_set())

def kb_donaciones(user=None) -> InlineKeyboardMarkup:
    rows=[]; uid=uname=""
    if user: uid=getattr(user,"id",""); uname=getattr(user,"username","") or ""
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

def now() -> int: return int(time.time())

def translate(txt: str, target: str) -> str:
    if not ENABLE_TRANSLATION or not txt.strip(): return txt
    try: return GoogleTranslator(source='auto', target=target).translate(txt)
    except Exception: return txt

# ===== Handlers =====
async def cmd_start(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        f"Hola {update.effective_user.first_name or ''} 👋\nAsistente de *{STATE['model_name']}*.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones(update.effective_user))

async def cmd_menu(update: Update, ctx: CallbackContext):
    await update.message.reply_text("💝 Opciones de apoyo:", reply_markup=kb_donaciones(update.effective_user))

async def cmd_whoami(update: Update, ctx: CallbackContext):
    u=update.effective_user
    await update.message.reply_text(f"Tu user_id: {u.id}\nUsername: @{u.username}" if u.username else f"Tu user_id: {u.id}\nUsername: (sin username)")

async def cmd_admins(update: Update, ctx: CallbackContext):
    await update.message.reply_text("Admins (archivo): "+(", ".join(map(str,STATE.get('admins',[]))) or "—")
                                    +"\nAdmins (ENV): "+(", ".join(sorted(env_admin_set())) or "—"))

async def cmd_iamadmin(update: Update, ctx: CallbackContext):
    uid=update.effective_user.id
    if uid not in STATE["admins"]:
        STATE["admins"].append(uid); save_state(STATE)
    await update.message.reply_text("✅ Ya eres admin de este bot.")

async def guard_admin(update: Update) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Solo admin."); return False
    return True

async def cmd_liveon(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    STATE["marketing_on"]=True; save_state(STATE)
    await update.message.reply_text("🟢 LIVE activado.")

async def cmd_liveoff(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    STATE["marketing_on"]=False; save_state(STATE)
    await update.message.reply_text("🔴 LIVE desactivado.")

async def cmd_setmodel(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    name = update.message.text.split(" ",1)[-1].strip() or "Modelo"
    STATE["model_name"]=name; save_state(STATE); await update.message.reply_text("✅ Modelo cambiado.")

async def cmd_setmodelid(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    arg = update.message.text.split(" ",1)[-1].strip()
    uid = update.effective_user.id if arg.lower()=="me" else int(re.sub(r"\D","",arg) or "0")
    STATE["model_id"]=uid; save_state(STATE); await update.message.reply_text(f"✅ model_id = {uid}")

async def cmd_setlangs(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    langs=[s.strip() for s in update.message.text.split(" ",1)[-1].split(",") if s.strip()]
    if langs: STATE["langs"]=langs; save_state(STATE); await update.message.reply_text("✅ Idiomas: "+", ".join(langs))
    else: await update.message.reply_text("Usa: /setlangs de,en,es,pl")

def parse_price_line(arg: str) -> Optional[tuple[str,float]]:
    # acepta "Nombre · 7", "Nombre : 7", "Nombre - 7", "Nombre 7"
    m = re.match(r"^(.*?)[\s·:\-]+(\d+(?:[.,]\d{1,2})?)\s*$", arg)
    if not m: return None
    name = m.group(1).strip()
    val = float(m.group(2).replace(",", "."))    
    return (name, val)

async def cmd_addprice(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    arg=update.message.text.split(" ",1)[-1].strip()
    parsed = parse_price_line(arg)
    if not parsed: return await update.message.reply_text("Usa: /addprice Nombre · 7")
    name, price = parsed
    STATE["prices"].append([name,price]); save_state(STATE)
    await update.message.reply_text(f"✅ Agregado: {name} · {price} {CURRENCY}")

async def cmd_delprice(update: Update, ctx: CallbackContext):
    if not await guard_admin(update): return
    name=update.message.text.split(" ",1)[-1].strip()
    before=len(STATE["prices"]); STATE["prices"]=[p for p in STATE["prices"] if p[0]!=name]; save_state(STATE)
    await update.message.reply_text("✅ Eliminado." if len(STATE["prices"])<before else "No encontrado.")

async def cmd_listprices(update: Update, ctx: CallbackContext):
    lines=[f"• {n} · {v} {CURRENCY}" for n,v in STATE.get("prices",[])]
    await update.message.reply_text("Precios actuales:\n"+"\n".join(lines) if lines else "Sin precios aún.")

# Grupo: texto + marketing + traducción
async def on_group_text(update: Update, ctx: CallbackContext):
    chat_id = update.effective_chat.id
    u = update.effective_user
    txt = update.message.text or ""

    # marketing (enfriamiento 10 min)
    if now() - STATE.get("last_push_ts",0) > 600:
        STATE["last_push_ts"]=now(); save_state(STATE)
        await ctx.bot.send_message(chat_id,
            f"💝 Apoya a *{STATE['model_name']}* y aparece en pantalla.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_donaciones())

    # traducción
    if ENABLE_TRANSLATION and txt:
        model_id = STATE.get("model_id",0)
        if u and u.id == model_id:
            target = STATE.get("langs",["de"])[0]
            out = translate(txt, target)
            if out and out != txt:
                await ctx.bot.send_message(chat_id, f"🌐 {target}: {out}")
        else:
            out = translate(txt, "es")
            if out and out != txt:
                await ctx.bot.send_message(chat_id, f"🌐 es: {out}")

# LIVE start/end (grupo)
async def on_group_live_start(update: Update, ctx: CallbackContext):
    STATE["marketing_on"]=True; save_state(STATE)
    await ctx.bot.send_message(update.effective_chat.id, "🔴 LIVE detectado (grupo). Marketing activado.")
async def on_group_live_end(update: Update, ctx: CallbackContext):
    STATE["marketing_on"]=False; save_state(STATE)
    await ctx.bot.send_message(update.effective_chat.id, "⚫️ LIVE finalizado. Marketing detenido.")

# LIVE start/end (canal)
async def on_channel_live_start(update: Update, ctx: CallbackContext):
    STATE["marketing_on"]=True; save_state(STATE)
    await ctx.bot.send_message(CHANNEL_ID, "🔴 LIVE detectado (canal). Marketing activado.")
async def on_channel_live_end(update: Update, ctx: CallbackContext):
    STATE["marketing_on"]=False; save_state(STATE)
    await ctx.bot.send_message(CHANNEL_ID, "⚫️ LIVE finalizado. Marketing detenido.")

# ===== Web (Flask) =====
web = Flask(__name__)

@web.get("/")
def index(): return "CosplayLive OK"

@web.get("/overlay")
def overlay_page():
    return """
<!doctype html><meta charset="utf-8"><title>Overlay</title>
<style>body{background:#0b1020;color:#fff;font-family:system-ui;margin:0}
.event{padding:16px;margin:12px;border-radius:14px;background:#16213a}
.big{font-size:22px;font-weight:700}</style>
<div id="log"></div>
<script>
const log=document.getElementById('log');
const ev=new EventSource('/events');
const ding=new Audio('https://actions.google.com/sounds/v1/cartoon/clang_and_wobble.ogg');
ev.onmessage=(m)=>{const o=JSON.parse(m.data);
 if(o.type==='donation'){try{ding.currentTime=0;ding.play();}catch(e){}}
 const d=document.createElement('div'); d.className='event';
 d.innerHTML=`<div class="big">${o.type.toUpperCase()}</div><div>${JSON.stringify(o.data)}</div>`;
 log.prepend(d);};
</script>"""

@web.get("/events")
def sse_events():
    q = EVENTS.subscribe()
    def stream():
        try:
            while True:
                yield f"data: {json.dumps(q.get(), ensure_ascii=False)}\n\n"
        finally:
            EVENTS.unsubscribe(q)
    return Response(stream(), mimetype="text/event-stream")

@web.get("/studio")
def studio_page():
    return f"""<!doctype html><meta charset="utf-8">
<h2>Studio – {STATE.get('model_name')}</h2>
<p><a href="{BASE_URL}/overlay" target="_blank">Abrir Overlay</a></p>
<form method="post" action="{BASE_URL}/studio/ding"><button>🔔 Probar sonido</button></form>
<p>Si el botón no funciona en tu navegador, prueba aquí: <a href="{BASE_URL}/studio/ding">/studio/ding</a></p>"""

@web.route("/studio/ding", methods=["GET","POST"])
def studio_ding():
    EVENTS.push({"type":"donation","data":{"payer":"TestUser","amount":"0.00","memo":"Test"}, "ts":now()})
    return "<p>OK (revisa el Overlay).</p>"

def _parse_amount(amt: str) -> Optional[int]:
    if not amt: return None
    amt = amt.replace(",", ".").strip()
    if not re.match(r"^\d+(\.\d{1,2})?$", amt): return None
    return int(round(float(amt)*100))

@web.get("/donar")
def donate_page():
    amt = request.args.get("amt", "").strip()
    ccy = request.args.get("c", CURRENCY)
    uid = request.args.get("uid", ""); uname = request.args.get("uname", "")
    title = f"Apoyo a {STATE.get('model_name')}"
    cents = _parse_amount(amt)
    if cents is None:
        base = f"{BASE_URL}/donar?c={ccy}"
        if uid: base += f"&uid={uid}"
        if uname: base += f"&uname={uname}"
        return f"""<!doctype html><meta charset="utf-8"><h3>Monto inválido</h3>
<form method="get" action="{BASE_URL}/donar">
<input type="hidden" name="c" value="{ccy}">
<input type="hidden" name="uid" value="{uid}"><input type="hidden" name="uname" value="{uname}">
<label>Monto ({ccy}): <input name="amt" placeholder="5"></label>
<button>Pagar</button></form>"""
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price_data":{"currency":ccy.lower(), "product_data":{"name":title},
                                   "unit_amount":cents}, "quantity":1}],
        success_url=f"{BASE_URL}/ok",
        cancel_url=f"{BASE_URL}/cancel",
        metadata={"channel_id":str(CHANNEL_ID),"amount":f"{amt} {ccy}","uid":uid,"uname":uname},
    )
    return f'<meta http-equiv="refresh" content="0;url={session.url}">'

@web.get("/ok")
def ok_page():
    chan = CHANNEL_USERNAME.strip()
    tg = f"tg://resolve?domain={chan}" if chan else ""
    btn = f'<p><a href="{tg}">Volver a Telegram</a></p>' if tg else ""
    return f"<h2>✅ Pago recibido</h2><p>Pronto verás el anuncio en el canal.</p>{btn}"

@web.get("/cancel")
def cancel_page(): return "<h3>Pago cancelado</h3>"

@web.post("/webhook")
def stripe_webhook():
    payload = request.data; sig = request.headers.get("Stripe-Signature","")
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
            celebrate(app.bot, int(CHANNEL_ID), payer_name, amount, memo), app.loop
        )
    return ("",200)

# ===== Arranque =====
def run_flask(): web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    app = telegram_app_singleton()
    app.add_handler(CommandHandler(["start","help"], cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("iamadmin", cmd_iamadmin))
    app.add_handler(CommandHandler("liveon", cmd_liveon))
    app.add_handler(CommandHandler("liveoff", cmd_liveoff))
    app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    app.add_handler(CommandHandler("setmodelid", cmd_setmodelid))
    app.add_handler(CommandHandler("setlangs", cmd_setlangs))
    app.add_handler(CommandHandler("addprice", cmd_addprice))
    app.add_handler(CommandHandler("delprice", cmd_delprice))
    app.add_handler(CommandHandler("listprices", cmd_listprices))

    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_text))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.VIDEO_CHAT_STARTED, on_group_live_start))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.VIDEO_CHAT_ENDED, on_group_live_end))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.StatusUpdate.VIDEO_CHAT_STARTED, on_channel_live_start))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.StatusUpdate.VIDEO_CHAT_ENDED, on_channel_live_end))

    Thread(target=run_flask, daemon=True).start()
    log.info("Starting polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
