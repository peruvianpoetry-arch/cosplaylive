# CosplayLive ULTRA — responde DMs y grupos, horario-offline, LIVE marketing, i18n, Stripe
# Requiere requirements.txt: python-telegram-bot==20.8, Flask==3.0.3, stripe==9.11.0, deep-translator==1.11.4, Pillow==10.4.0
import os, sys, threading, logging, queue, io, html, json, asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List
from flask import Flask, Response, request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import stripe

# ----- PIL opcional (tarjeta de "Gracias") -----
PIL_OK = True
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    PIL_OK = False

from deep_translator import GoogleTranslator

# ===== Logging =====
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

# ===== ENV =====
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()               # -100xxxxxxxxxx
BASE_URL = os.getenv("BASE_URL", "").strip()                   # https://tuapp.onrender.com
STRIPE_SK = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WH = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CURRENCY = os.getenv("CURRENCY", "EUR")
ANNOUNCE_EVERY_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN", "5")) # marketing en LIVE
ADMIN_IDS = [s.strip() for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()]
MODEL_USER_IDS = [s.strip() for s in os.getenv("MODEL_USER_IDS", "").split(",") if s.strip()]

if not TOKEN: raise SystemExit("Falta TELEGRAM_TOKEN")
stripe.api_key = STRIPE_SK

# ===== Persistencia =====
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_PATH = os.path.join(DATA_DIR, "data.json")

DEFAULT_CONFIG = {
    "model_name": "Cosplay Emma",
    "schedule": "Hoy 20:00–23:00 CET (DE/CH/SE/PL)",  # editable con /setschedule
    "bio": "Cosplay europea. Shows en DE/CH/SE/PL. Apoya con el menú.",
    "audience_langs": ["de","en","pl","sv","es"],
    "prices": [
        {"name":"💃 Dance", "price":3},
        {"name":"👗 Lingerie try-on", "price":10},
        {"name":"🙈 Topless", "price":5},
        {"name":"🎯 Group goal", "price":50},
    ],
}

def load_cfg()->Dict[str,Any]:
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH,"r",encoding="utf-8") as f: return json.load(f)
        except Exception as e: log.error(f"load_cfg: {e}")
    with open(DATA_PATH,"w",encoding="utf-8") as f: json.dump(DEFAULT_CONFIG,f,ensure_ascii=False,indent=2)
    return json.loads(json.dumps(DEFAULT_CONFIG))

def save_cfg(cfg:Dict[str,Any])->None:
    try:
        with open(DATA_PATH,"w",encoding="utf-8") as f: json.dump(cfg,f,ensure_ascii=False,indent=2)
    except Exception as e:
        log.error(f"save_cfg: {e}")

CFG = load_cfg()

# ===== I18N básica =====
SUPPORTED = ["de","en","es","pl","sv"]
def detect_lang(text:str)->str:
    try:
        code = GoogleTranslator(source="auto", target="en").detect(text).split("-")[0]
    except Exception:
        code = "de"
    return code if code in SUPPORTED else "de"

def tr(lang:str, key:str, **kw)->str:
    T = {
        "assistant": {
            "de":"🤖 *Assistent von {name}*. Frag etwas oder nutze die Buttons.",
            "en":"🤖 *{name}’s assistant*. Ask anything or use the buttons.",
            "es":"🤖 *Asistente de {name}*. Pregunta o usa los botones.",
            "pl":"🤖 *Asystent {name}*. Pytaj lub użyj przycisków.",
            "sv":"🤖 *{name}s assistent*. Fråga eller använd knapparna."
        },
        "about": {
            "de":"ℹ️ *Über das Model:* {bio}",
            "en":"ℹ️ *About the model:* {bio}",
            "es":"ℹ️ *Sobre la modelo:* {bio}",
            "pl":"ℹ️ *O modelce:* {bio}",
            "sv":"ℹ️ *Om modellen:* {bio}",
        },
        "menu_title":{
            "de":"✨ *Wunsch-/Trinkgeldmenü*",
            "en":"✨ *Tip & Request Menu*",
            "es":"✨ *Menú de apoyos y pedidos*",
            "pl":"✨ *Menu napiwków i próśb*",
            "sv":"✨ *Dricks- & önskemålmeny*",
        },
        "cta":{
            "de":"Zahl mit Karte, Apple/Google Pay oder lokalen Methoden (Stripe).",
            "en":"Pay with card, Apple/Google Pay or local methods (Stripe).",
            "es":"Paga con tarjeta, Apple/Google Pay o métodos locales (Stripe).",
            "pl":"Płać kartą, Apple/Google Pay lub lokalnymi metodami (Stripe).",
            "sv":"Betala med kort, Apple/Google Pay eller lokala metoder (Stripe).",
        },
        "offline":{
            "de":"⏳ *{name} ist gerade offline.* Nächster Live: {schedule}",
            "en":"⏳ *{name} is offline right now.* Next live: {schedule}",
            "es":"⏳ *{name} está offline ahora.* Próximo live: {schedule}",
            "pl":"⏳ *{name} jest teraz offline.* Następny live: {schedule}",
            "sv":"⏳ *{name} är offline nu.* Nästa live: {schedule}",
        },
        "thanks":{
            "de":"🎉 *Danke, {user}!* Support: *{amount}*.\n_{memo}_",
            "en":"🎉 *Thanks, {user}!* Support: *{amount}*.\n_{memo}_",
            "es":"🎉 *¡Gracias, {user}!* Apoyo: *{amount}*.\n_{memo}_",
            "pl":"🎉 *Dzięki, {user}!* Wsparcie: *{amount}*.\n_{memo}_",
            "sv":"🎉 *Tack, {user}!* Stöd: *{amount}*.\n_{memo}_",
        },
        "overlay_on":{"de":"🟢 Overlay aktiviert","en":"🟢 Overlay ON","es":"🟢 Overlay activado","pl":"🟢 Overlay włączony","sv":"🟢 Overlay på"},
        "overlay_off":{"de":"🔴 Overlay deaktiviert","en":"🔴 Overlay OFF","es":"🔴 Overlay desactivado","pl":"🔴 Overlay wyłączony","sv":"🔴 Overlay av"},
    }
    lang = lang if lang in SUPPORTED else "de"
    s = T[key][lang]
    try: return s.format(**kw)
    except: return s

# ===== Estado =====
LIVE_ACTIVE = False
last_ad = datetime.utcnow() - timedelta(hours=1)
last_user_lang = "de"                 # idioma detectado más reciente
OVERLAY_ENABLED = True
SUPPRESS_AFTER_DONATION_SEC = 90
OFFLINE_HINT_COOLDOWN_MIN = 10        # cada cuánto puede repetir el aviso offline
last_offline_hint = datetime.utcnow() - timedelta(hours=1)

# ===== SSE (Studio/Overlay) =====
q_studio: "queue.Queue[str]" = queue.Queue(maxsize=300)
q_overlay: "queue.Queue[str]" = queue.Queue(maxsize=600)
def _push(q, text:str):
    text = (text or "").replace("\n"," ").strip()
    if not text: return
    try: q.put_nowait(text)
    except queue.Full:
        try: q.get_nowait()
        except queue.Empty: pass
        q.put_nowait(text)
def push_studio(t:str): _push(q_studio, t)
def push_overlay(t:str):
    if OVERLAY_ENABLED: _push(q_overlay, t)

# ===== Flask =====
web = Flask(__name__)

@web.get("/")
def home(): return "CosplayLive ULTRA OK"

@web.get("/ok")
def ok_page():
    return "<h2>✅ Pago recibido (test). Vuelve a Telegram.</h2>"

@web.get("/cancel")
def cancel_page():
    return "<h2>❌ Pago cancelado.</h2>"

@web.get("/studio")
def studio():
    return """
<!doctype html><meta charset="utf-8"><title>Cosplay Studio</title>
<style>body{background:#0b0f17;color:#fff;font:16px system-ui;margin:0}
.wrap{max-width:940px;margin:0 auto;padding:16px} .ev{background:#121b2e;border-radius:14px;padding:12px 14px;margin:8px 0}</style>
<div class=wrap><h1>👩‍🎤 Cosplay Studio</h1><p>Mantén esta página abierta.</p><div id=evs></div>
<audio id=ding><source src="https://actions.google.com/sounds/v1/alarms/beep_short.ogg" type="audio/ogg"></audio></div>
<script>const b=document.getElementById('evs'),d=document.getElementById('ding');const es=new EventSource('/events');
es.onmessage=(e)=>{const x=document.createElement('div');x.className='ev';x.textContent=e.data;b.prepend(x);try{d.currentTime=0;d.play()}catch(_){}}</script>
"""

@web.get("/events")
def sse_studio():
    def stream():
        while True: yield f"data: {q_studio.get()}\n\n"
    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"})

@web.get("/overlay")
def overlay():
    return """
<!doctype html><meta charset="utf-8"><title>Overlay</title>
<style>html,body{margin:0;background:transparent;overflow:hidden}
#stack{position:fixed;left:0;right:0;bottom:10px;display:flex;flex-direction:column-reverse;gap:10px;padding:10px}
.msg{align-self:center;background:rgba(0,0,0,.55);color:#fff;border-radius:18px;padding:12px 18px;font:600 22px system-ui}</style>
<div id=stack></div><script>const s=document.getElementById('stack'),es=new EventSource('/overlay-events');
es.onmessage=(e)=>{const d=document.createElement('div');d.className='msg';d.textContent=e.data;s.append(d);setTimeout(()=>d.remove(),12000);};</script>
"""

@web.get("/overlay-events")
def sse_overlay():
    def stream():
        while True: yield f"data: {q_overlay.get()}\n\n"
    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"})

# ===== Stripe =====
def build_card(title:str, subtitle:str):
    if not PIL_OK: return None
    W,H=1200,500; img=Image.new("RGB",(W,H),(8,12,22)); d=ImageDraw.Draw(img)
    try:
        f1=ImageFont.truetype("DejaVuSans-Bold.ttf",68); f2=ImageFont.truetype("DejaVuSans.ttf",44)
    except: f1=f2=ImageFont.load_default()
    d.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    tw,th=d.textsize(title,font=f1); d.text(((W-tw)//2,140),title,font=f1,fill=(255,255,255))
    sw,sh=d.textsize(subtitle,font=f2); d.text(((W-sw)//2,260),subtitle,font=f2,fill=(190,220,255))
    buf=io.BytesIO(); img.save(buf,"PNG"); buf.seek(0); return buf

def kb_don(lang:str)->InlineKeyboardMarkup:
    rows=[]
    for p in CFG["prices"]:
        rows.append([InlineKeyboardButton(f"{p['name']} · {p['price']} {CURRENCY}",
            url=f"{BASE_URL}/donar?amt={p['price']}&c={CURRENCY}")])
    rows.append([InlineKeyboardButton({"de":"💝 Freier Betrag","en":"💝 Free amount","es":"💝 Importe libre","pl":"💝 Dowolna kwota","sv":"💝 Valfritt belopp"}[lang],
            url=f"{BASE_URL}/donar")])
    return InlineKeyboardMarkup(rows)

@web.get("/donar")
def donar():
    amt = request.args.get("amt",""); ccy=request.args.get("c",CURRENCY)
    if not STRIPE_SK or not BASE_URL: return "<b>Stripe no configurado</b>"
    title = f"Support {CFG['model_name']}"
    if amt.isdigit() and int(amt)>0:
        s = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data":{"currency":ccy.lower(),"product_data":{"name":title},"unit_amount":int(amt)*100},"quantity":1}],
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"channel_id":CHANNEL_ID,"amount":f"{amt} {ccy}"},
            allow_promotion_codes=True,
        )
        return f'<meta http-equiv="refresh" content="0;url={s.url}">'
    opts="".join([f'<a href="/donar?amt={p["price"]}&c={ccy}">{html.escape(p["name"])} · {p["price"]} {ccy}</a><br>' for p in CFG["prices"]])
    return f"<h3>Choose</h3>{opts}<p>Or set amount in the form.</p>"

@web.post("/stripe/webhook")
def stripe_webhook():
    payload=request.data; sig=request.headers.get("Stripe-Signature","")
    try:
        event=stripe.Webhook.construct_event(payload,sig,STRIPE_WH)
    except Exception as e:
        log.error(f"Webhook inválido: {e}"); return "bad",400
    if event["type"]=="checkout.session.completed":
        sess=event["data"]["object"]; meta=sess.get("metadata") or {}
        amount = meta.get("amount") or f"{(sess.get('amount_total') or 0)/100:.2f} {sess.get('currency','').upper()}"
        payer  = (sess.get("customer_details") or {}).get("email","usuario")
        memo   = "¡Gracias por tu apoyo!"
        try:
            app=telegram_app_singleton()
            app.create_task(celebrate(app.bot, int(CHANNEL_ID), payer, amount, memo))
        except Exception as e:
            log.error(f"notify fail: {e}")
        push_studio(f"Webhook OK: {payer} {amount}")
        log.info(f"Stripe OK — {amount}")
    return "ok",200

# ===== Telegram =====
def is_admin(uid:int)->bool:
    return str(uid) in ADMIN_IDS or str(uid) in MODEL_USER_IDS

async def send_menu(chat_id:int, bot, lang:str):
    title = tr(lang,"assistant", name=CFG["model_name"])
    about = tr(lang,"about", bio=CFG["bio"])
    items = "\n".join([f"• {p['name']} — *{p['price']}* {CURRENCY}" for p in CFG["prices"]])
    text = f"{title}\n{about}\n\n{tr(lang,'menu_title')}\n\n{items}\n\n{tr(lang,'cta')}"
    await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_don(lang))

# --- Celebración de pago ---
async def celebrate(bot, chat_id:int, user:str, amount:str, memo:str):
    global last_ad
    lang = last_user_lang or "de"
    msg = await bot.send_message(chat_id, tr(lang,"thanks", user=user, amount=amount, memo=memo), parse_mode=ParseMode.MARKDOWN)
    try:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        await asyncio.sleep(15); await bot.unpin_chat_message(chat_id, msg.message_id)
    except Exception as e: log.info(f"pin opcional: {e}")
    try:
        old=(await bot.get_chat(chat_id)).title or ""
        await bot.set_chat_title(chat_id, f"🔥 Danke {user} ({amount})"); await asyncio.sleep(15); await bot.set_chat_title(chat_id, old)
    except Exception as e: log.info(f"title opcional: {e}")
    buf = build_card(f"Danke {user}!", f"Support: {amount}")
    if buf: await bot.send_photo(chat_id, photo=InputFile(buf, filename="thanks.png"))
    push_studio(f"🎉 Donación: {user} → {amount}")
    push_overlay(f"🎉 {user}: {amount}")
    last_ad = datetime.utcnow()

# --- Handlers: comandos ---
async def start_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE): await u.message.reply_text("Listo: /menu /precios /status /golive /endlive /addprice /delprice /listprices /setbio /setname /setschedule /setlangs /overlayon /overlayoff")

async def menu_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    lang = detect_lang(u.message.text or "")
    await send_menu(u.effective_chat.id, c.bot, lang)

precios_cmd = menu_cmd

async def status_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"LIVE_ACTIVE={LIVE_ACTIVE}, anuncios cada {ANNOUNCE_EVERY_MIN} min, overlay={'on' if OVERLAY_ENABLED else 'off'}")

async def golive_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LIVE_ACTIVE; LIVE_ACTIVE=True
    await u.message.reply_text("🟢 LIVE activado (manual).")

async def endlive_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LIVE_ACTIVE; LIVE_ACTIVE=False
    await u.message.reply_text("🔴 LIVE desactivado (manual).")

async def overlay_on(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global OVERLAY_ENABLED; OVERLAY_ENABLED=True
    await u.message.reply_text(tr("de","overlay_on"))

async def overlay_off(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global OVERLAY_ENABLED; OVERLAY_ENABLED=False
    await u.message.reply_text(tr("de","overlay_off"))

async def addprice_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not c.args or len(c.args)<2: return await u.message.reply_text("Uso: /addprice <importe> <nombre>")
    try:
        amt=int(c.args[0]); name=" ".join(c.args[1:])
        CFG["prices"].append({"name":name,"price":amt}); save_cfg(CFG)
        await u.message.reply_text(f"Añadido: {name} · {amt} {CURRENCY}")
    except: await u.message.reply_text("Formato inválido. Ej: /addprice 3 Beso")

async def delprice_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not c.args: return await u.message.reply_text("Uso: /delprice <índice|nombre>")
    arg=" ".join(c.args).strip(); removed=None
    if arg.isdigit():
        i=int(arg)-1
        if 0<=i<len(CFG["prices"]): removed=CFG["prices"].pop(i)
    else:
        for i,p in enumerate(CFG["prices"]):
            if p["name"].lower()==arg.lower(): removed=CFG["prices"].pop(i); break
    if removed: save_cfg(CFG); await u.message.reply_text(f"Eliminado: {removed['name']}")
    else: await u.message.reply_text("No encontrado.")

async def listprices_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    lines=[f"{i}. {p['name']} · {p['price']} {CURRENCY}" for i,p in enumerate(CFG['prices'],start=1)]
    await u.message.reply_text("Precios:\n"+"\n".join(lines))

async def setbio_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    txt=(u.message.text or "").split(" ",1)
    if len(txt)<2: return await u.message.reply_text("Uso: /setbio <texto>")
    CFG["bio"]=txt[1].strip(); save_cfg(CFG); await u.message.reply_text("Bio actualizada.")

async def setname_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    txt=(u.message.text or "").split(" ",1)
    if len(txt)<2: return await u.message.reply_text("Uso: /setname <nombre de la modelo>")
    CFG["model_name"]=txt[1].strip(); save_cfg(CFG); await u.message.reply_text("Nombre actualizado.")

async def setschedule_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    txt=(u.message.text or "").split(" ",1)
    if len(txt)<2: return await u.message.reply_text("Uso: /setschedule <horario>")
    CFG["schedule"]=txt[1].strip(); save_cfg(CFG); await u.message.reply_text("Horario actualizado.")

async def setlangs_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not c.args: return await u.message.reply_text("Uso: /setlangs de,en,pl,sv,es")
    arr=[x for x in " ".join(c.args).replace(" ","").split(",") if x in SUPPORTED]
    if not arr: return await u.message.reply_text("Idiomas no válidos.")
    CFG["audience_langs"]=arr; save_cfg(CFG); await u.message.reply_text("Idiomas: "+",".join(arr))

# --- Mensajes normales (grupo y privado) ---
async def dm_text(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global last_user_lang
    txt = u.message.text or ""
    last_user_lang = detect_lang(txt)
    await send_menu(u.effective_chat.id, c.bot, last_user_lang)

async def group_text(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global last_user_lang, last_offline_hint
    msg=u.message; text=msg.text or ""; lang=detect_lang(text); last_user_lang=lang
    # espejo
    if str(u.effective_chat.id)==str(CHANNEL_ID) and not (u.effective_user and u.effective_user.is_bot):
        push_overlay(f"{u.effective_user.full_name}: {text}")
        push_studio(f"{u.effective_user.full_name}: {text}")
    # si modelo offline, avisa horario una vez por cooldown
    if not LIVE_ACTIVE and (datetime.utcnow()-last_offline_hint) >= timedelta(minutes=OFFLINE_HINT_COOLDOWN_MIN):
        last_offline_hint = datetime.utcnow()
        await c.bot.send_message(u.effective_chat.id, tr(lang,"offline", name=CFG["model_name"], schedule=CFG["schedule"]), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_don(lang))
    else:
        # respuesta básica + menú
        await send_menu(u.effective_chat.id, c.bot, lang)

# --- Posts del canal + eventos de live (video chat) ---
async def channel_post(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.channel_post and u.channel_post.text:
        push_overlay(f"📢 {u.channel_post.text}")
        push_studio(f"📢 {u.channel_post.text}")

async def svc_event(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LIVE_ACTIVE
    m=u.effective_message
    if m and m.video_chat_started:
        LIVE_ACTIVE=True; push_studio("🟢 LIVE ON"); push_overlay("🟢 LIVE ON")
    if m and m.video_chat_ended:
        LIVE_ACTIVE=False; push_studio("🔴 LIVE OFF"); push_overlay("🔴 LIVE OFF")

# ===== Scheduler (marketing solo en LIVE) =====
async def tick(app):
    global last_ad
    while True:
        try:
            if LIVE_ACTIVE and CHANNEL_ID:
                if (datetime.utcnow()-last_ad) >= timedelta(minutes=ANNOUNCE_EVERY_MIN):
                    lang = last_user_lang or "de"
                    await send_menu(int(CHANNEL_ID), app.bot, lang)
                    last_ad = datetime.utcnow()
            await asyncio.sleep(20)
        except Exception as e:
            log.error(f"scheduler: {e}"); await asyncio.sleep(5)

async def on_startup(app): app.create_task(tick(app))

# ===== App TG =====
_app=None
def telegram_app_singleton():
    global _app
    if _app: return _app
    _app=(ApplicationBuilder().token(TOKEN).post_init(on_startup).build())
    _app.add_handler(CommandHandler("start", start_cmd))
    _app.add_handler(CommandHandler("menu", menu_cmd))
    _app.add_handler(CommandHandler("precios", precios_cmd))
    _app.add_handler(CommandHandler("status", status_cmd))
    _app.add_handler(CommandHandler("golive", golive_cmd))
    _app.add_handler(CommandHandler("endlive", endlive_cmd))
    _app.add_handler(CommandHandler("overlayon", overlay_on))
    _app.add_handler(CommandHandler("overlayoff", overlay_off))
    _app.add_handler(CommandHandler("addprice", addprice_cmd))
    _app.add_handler(CommandHandler("delprice", delprice_cmd))
    _app.add_handler(CommandHandler("listprices", listprices_cmd))
    _app.add_handler(CommandHandler("setbio", setbio_cmd))
    _app.add_handler(CommandHandler("setname", setname_cmd))
    _app.add_handler(CommandHandler("setschedule", setschedule_cmd))
    _app.add_handler(CommandHandler("setlangs", setlangs_cmd))
    # DMs
    _app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, dm_text))
    # Grupos/supergrupos (no canal)
    _app.add_handler(MessageHandler(filters.TEXT & (~filters.ChatType.PRIVATE) & ~filters.ChatType.CHANNEL, group_text))
    # Posts del canal
    _app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    # Eventos de live
    _app.add_handler(MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED | filters.StatusUpdate.VIDEO_CHAT_ENDED, svc_event))
    return _app

# ===== Main =====
def run_web(): web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__=="__main__":
    app=telegram_app_singleton()
    t=threading.Thread(target=run_web, daemon=True); t.start()
    log.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
