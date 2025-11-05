# CosplayLive — baseline estable + nick Stripe + redirect Telegram + audio test
import os, sys, logging, threading, asyncio, io, json, html, queue
from datetime import datetime, timedelta
from typing import Dict, Any, List
from flask import Flask, Response, request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import stripe

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

TOKEN       = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PORT        = int(os.getenv("PORT", "10000"))
CHANNEL_ID  = os.getenv("CHANNEL_ID", "").strip()          # -100xxxxxxxxxx
BASE_URL    = os.getenv("BASE_URL", "").strip()
CURRENCY    = os.getenv("CURRENCY", "EUR").strip()
STRIPE_SK   = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WH   = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
ADMIN_IDS   = [s.strip() for s in os.getenv("ADMIN_IDS","").split(",") if s.strip()]
MODEL_USER_IDS = [s.strip() for s in os.getenv("MODEL_USER_IDS","").split(",") if s.strip()]
ANNOUNCE_EVERY_MIN = int(os.getenv("ANNOUNCE_EVERY_MIN","5"))
# Para redirigir tras el pago:
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME","").strip()  # sin @
BOT_USERNAME     = os.getenv("BOT_USERNAME","").strip()       # sin @

if not TOKEN:
    raise SystemExit("⚠️ Falta TELEGRAM_TOKEN")

stripe.api_key = STRIPE_SK or None

DATA_DIR  = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_PATH = os.path.join(DATA_DIR, "data.json")

DEFAULT_CONFIG = {
  "model_name":"Cosplay Emma",
  "schedule":"Hoy 20:00–23:00 CET (DE/CH/SE/PL)",
  "bio":"Cosplay europea. Shows en DE/CH/SE/PL. Apoya con el menú.",
  "audience_langs":["de","en","pl","sv","es"],
  "prices":[
    {"name":"💃 Dance","price":3},
    {"name":"👗 Lingerie try-on","price":10},
    {"name":"🙈 Topless","price":5},
    {"name":"🎯 Group goal","price":50}
  ]
}

def load_cfg()->Dict[str,Any]:
    try:
        if os.path.exists(DATA_PATH):
            with open(DATA_PATH,"r",encoding="utf-8") as f: cfg=json.load(f)
        else:
            cfg=DEFAULT_CONFIG
        for k,v in DEFAULT_CONFIG.items():
            if k not in cfg: cfg[k]=v
        with open(DATA_PATH,"w",encoding="utf-8") as f: json.dump(cfg,f,ensure_ascii=False,indent=2)
        return cfg
    except Exception as e:
        log.error(f"load_cfg: {e}")
        with open(DATA_PATH,"w",encoding="utf-8") as f: json.dump(DEFAULT_CONFIG,f,ensure_ascii=False,indent=2)
        return json.loads(json.dumps(DEFAULT_CONFIG))

def save_cfg(cfg:Dict[str,Any]):
    try:
        with open(DATA_PATH,"w",encoding="utf-8") as f: json.dump(cfg,f,ensure_ascii=False,indent=2)
    except Exception as e: log.error(f"save_cfg: {e}")

CFG = load_cfg()

PIL_OK=True
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    PIL_OK=False

try:
    from deep_translator import GoogleTranslator
    def detect_lang(text:str)->str:
        try:
            code=GoogleTranslator(source="auto", target="en").detect(text).split("-")[0]
        except Exception:
            code="de"
        return code if code in {"de","en","es","pl","sv"} else "de"
except Exception:
    def detect_lang(text:str)->str: return "de"

SUPPORTED={"de","en","es","pl","sv"}
def tr(lang,key,**kw):
    lang = lang if lang in SUPPORTED else "de"
    T={
      "assistant":{
        "de":"🤖 *Assistent von {name}*. Frag etwas oder nutze die Buttons.",
        "en":"🤖 *{name}’s assistant*. Ask anything or use the buttons.",
        "es":"🤖 *Asistente de {name}*. Pregunta o usa los botones.",
        "pl":"🤖 *Asystent {name}*. Pytaj lub użyj przycisków.",
        "sv":"🤖 *{name}s assistent*. Fråga eller använd knapparna."
      },
      "about":{
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
      "only_admin":{
        "de":"Nur für Admins.",
        "en":"Admins only.",
        "es":"Solo para admins.",
        "pl":"Tylko dla adminów.",
        "sv":"Endast admin.",
      }
    }
    s=T[key][lang]
    try: return s.format(**kw)
    except: return s

LIVE_ACTIVE=False
last_ad = datetime.utcnow() - timedelta(hours=1)
last_user_lang="de"
OVERLAY_ENABLED=True
OFFLINE_HINT_COOLDOWN_MIN=10
last_offline_hint = datetime.utcnow() - timedelta(hours=1)

q_studio: "queue.Queue[str]" = queue.Queue(maxsize=300)
q_overlay:"queue.Queue[str]" = queue.Queue(maxsize=600)
def _push(q, t):
    t=(t or "").replace("\n"," ").strip()
    if not t: return
    try: q.put_nowait(t)
    except queue.Full:
        try: q.get_nowait()
        except queue.Empty: pass
        q.put_nowait(t)
def push_studio(t): _push(q_studio,t)
def push_overlay(t):
    if OVERLAY_ENABLED: _push(q_overlay,t)

web = Flask(__name__)

@web.get("/health")
def health(): return "ok"

@web.get("/")
def home(): return "CosplayLive baseline OK"

@web.get("/ok")
def ok_page():
    # Intento de volver a Telegram automáticamente si hay usernames
    tg_target = ""
    if CHANNEL_USERNAME:
        tg_target = f"tg://resolve?domain={CHANNEL_USERNAME}"
    elif BOT_USERNAME:
        tg_target = f"tg://resolve?domain={BOT_USERNAME}"
    html_page = f"""
<!doctype html><meta charset="utf-8"><title>Pago OK</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<h2>✅ Pago recibido (modo test)</h2>
<p>En unos segundos volverás a Telegram.</p>
<script>
var tg = {json.dumps(tg_target)};
if (tg) {{
  setTimeout(function(){{ window.location.href = tg; }}, 600);
}}
</script>
<p><a href="{html.escape(tg_target)}">⬅️ Volver a Telegram</a></p>
"""
    return html_page

@web.get("/cancel")
def cancel_page(): return "<h2>❌ Pago cancelado.</h2>"

@web.get("/studio")
def studio():
    return """
<!doctype html><meta charset="utf-8"><title>Cosplay Studio</title>
<style>body{background:#0b0f17;color:#fff;font:16px system-ui;margin:0}
.wrap{max-width:940px;margin:0 auto;padding:16px}.ev{background:#121b2e;border-radius:14px;padding:12px;margin:8px 0}
.btn{background:#1f2a44;color:#fff;border-radius:12px;padding:10px 14px;display:inline-block;margin:6px 0;text-decoration:none}
</style>
<div class=wrap><h1>👩‍🎤 Cosplay Studio</h1>
<p>Mantén esta página abierta. Si no oyes sonido, toca “Probar sonido”.</p>
<a class=btn href="#" onclick="try{ding.currentTime=0;ding.play()}catch(e){}">🔊 Probar sonido</a>
<div id=evs></div>
<audio id="ding"><source src="https://actions.google.com/sounds/v1/alarms/beep_short.ogg" type="audio/ogg"></audio></div>
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

def build_card(title:str, subtitle:str):
    if not PIL_OK: return None
    W,H=1200,500
    img=Image.new("RGB",(W,H),(8,12,22))
    d=ImageDraw.Draw(img)
    try:
        f1=ImageFont.truetype("DejaVuSans-Bold.ttf",68); f2=ImageFont.truetype("DejaVuSans.ttf",44)
    except: f1=f2=ImageFont.load_default()
    # compat: en PIL modernos usar textbbox en lugar de textsize si no existe
    def center_y(text,font,y):
        if hasattr(d,"textbbox"):
            w=d.textbbox((0,0),text,font=font)[2]
        else:
            w=d.textsize(text,font=font)[0]
        x=(W-w)//2; d.text((x,y),text,font=font,fill=(255,255,255))
    d.rounded_rectangle([(20,20),(W-20,H-20)], radius=28, fill=(18,27,46))
    center_y(title,f1,140)
    center_y(subtitle,f2,260)
    buf=io.BytesIO(); img.save(buf,"PNG"); buf.seek(0); return buf

# ==== BOTONES con nick ====
def kb_don(lang:str, nick:str|None=None)->InlineKeyboardMarkup:
    rows=[]
    nick_q = f"&nick={html.escape(nick)}" if nick else ""
    for p in CFG["prices"]:
        rows.append([InlineKeyboardButton(
            f"{p['name']} · {p['price']} {CURRENCY}",
            url=f"{BASE_URL}/donar?amt={p['price']}&c={CURRENCY}{nick_q}"
        )])
    free_text = {"de":"💝 Freier Betrag","en":"💝 Free amount","es":"💝 Importe libre","pl":"💝 Dowolna kwota","sv":"💝 Valfritt belopp"}[lang if lang in SUPPORTED else 'de']
    rows.append([InlineKeyboardButton(free_text, url=f"{BASE_URL}/donar{('?nick='+html.escape(nick)) if nick else ''}")])
    return InlineKeyboardMarkup(rows)

async def celebrate(bot, chat_id:int, user:str, amount:str, memo:str, lang:str):
    msg = await bot.send_message(chat_id, tr(lang,"thanks", user=user, amount=amount, memo=memo), parse_mode=ParseMode.MARKDOWN)
    try:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        await asyncio.sleep(15); await bot.unpin_chat_message(chat_id, msg.message_id)
    except Exception as e: log.info(f"pin opcional: {e}")
    try:
        old=(await bot.get_chat(chat_id)).title or ""
        await bot.set_chat_title(chat_id, f"🔥 Danke {user} ({amount})"); await asyncio.sleep(15); await bot.set_chat_title(chat_id, old)
    except Exception as e: log.info(f"title opcional: {e}")
    buf=build_card(f"Danke {user}!", f"Support: {amount}")
    if buf: await bot.send_photo(chat_id, photo=InputFile(buf, filename="thanks.png"))
    push_studio(f"🎉 Donación: {user} → {amount}")
    push_overlay(f"🎉 {user}: {amount}")

pending_celebrates: "queue.Queue[tuple]" = queue.Queue()

@web.get("/donar")
def donar():
    try:
        amt=(request.args.get("amt") or "").strip()
        ccy=(request.args.get("c") or CURRENCY).strip()
        nick=(request.args.get("nick") or "").strip()[:32] or "Supporter"
        if not STRIPE_SK or not BASE_URL:
            return "<b>Stripe no configurado (STRIPE_SECRET_KEY/BASE_URL).</b>",200
        title=f"Support {CFG['model_name']}"
        if not amt.isdigit() or int(amt)<=0:
            opts="".join([f'<a href="/donar?amt={p["price"]}&c={ccy}&nick={html.escape(nick)}">{html.escape(p["name"])} · {p["price"]} {ccy}</a><br>' for p in CFG["prices"]])
            return f"<h3>Elige una opción</h3>{opts}<p>Tip libre: usa enteros (3, 5, 10). Nick: {html.escape(nick)}</p>",200
        session=stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data":{
                    "currency":ccy.lower(),
                    "product_data":{"name":title},
                    "unit_amount":int(amt)*100,
                },
                "quantity":1
            }],
            client_reference_id=nick,
            success_url=f"{BASE_URL}/ok",
            cancel_url=f"{BASE_URL}/cancel",
            metadata={"channel_id":CHANNEL_ID, "amount":f"{amt} {ccy}", "nick":nick},
            allow_promotion_codes=True,
        )
        return f'<meta http-equiv="refresh" content="0;url={session.url}">',302
    except Exception as e:
        return f"<h3>Server error</h3><pre>{html.escape(str(e))}</pre>",200

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
        payer  = meta.get("nick") or sess.get("client_reference_id") or (sess.get("customer_details") or {}).get("email","Supporter")
        lang = "de"
        try:
            app = telegram_app_singleton()
            loop = getattr(app, "_running_loop", None)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    celebrate(app.bot, int(CHANNEL_ID), payer, amount, "¡Gracias por tu apoyo!", lang),
                    loop
                )
            else:
                pending_celebrates.put((payer, amount, lang))
        except Exception as e:
            log.error(f"notify fail: {e}")
        push_studio(f"Webhook OK: {payer} {amount}")
        log.info(f"Stripe OK — {amount}")
    return "ok",200

def is_admin(uid:int)->bool:
    return (str(uid) in ADMIN_IDS) or (str(uid) in MODEL_USER_IDS)

async def send_menu(chat_id:int, bot, lang:str, nick:str|None=None):
    title=tr(lang,"assistant", name=CFG["model_name"])
    about=tr(lang,"about", bio=CFG["bio"])
    items="\n".join([f"• {p['name']} — *{p['price']}* {CURRENCY}" for p in CFG["prices"]])
    text=f"{title}\n{about}\n\n{tr(lang,'menu_title')}\n\n{items}\n\n{tr(lang,'cta')}"
    await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_don(lang, nick))

async def start_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Listo: /menu /precios /status /studio /liveon /liveoff /addprice /delprice /listprices /setbio /setname /setschedule /setlangs")

async def studio_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"🎛️ Abre tu panel: {BASE_URL}/studio")

async def menu_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    lang=detect_lang(u.message.text or ""); nick=(u.effective_user.username or u.effective_user.first_name) if u.effective_user else None
    await send_menu(u.effective_chat.id, c.bot, lang, nick)
precios_cmd = menu_cmd

async def status_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"LIVE_ACTIVE={LIVE_ACTIVE}, cada {ANNOUNCE_EVERY_MIN} min, overlay={'on' if OVERLAY_ENABLED else 'off'}")

async def liveon_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LIVE_ACTIVE; LIVE_ACTIVE=True; await u.message.reply_text("🟢 LIVE activado.")
async def liveoff_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LIVE_ACTIVE; LIVE_ACTIVE=False; await u.message.reply_text("🔴 LIVE desactivado.")

async def addprice_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    lang=detect_lang(u.message.text or "")
    if not is_admin(u.effective_user.id):
        return await u.message.reply_text(tr(lang,"only_admin"))
    if not c.args or len(c.args)<2:
        return await u.message.reply_text("Uso: /addprice <importe> <nombre>")
    try:
        amt=int(c.args[0]); name=" ".join(c.args[1:])
        CFG["prices"].append({"name":name,"price":amt}); save_cfg(CFG)
        await u.message.reply_text(f"Añadido: {name} · {amt} {CURRENCY}")
    except:
        await u.message.reply_text("Formato inválido. Ej: /addprice 3 Beso")

async def delprice_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    lang=detect_lang(u.message.text or "")
    if not is_admin(u.effective_user.id):
        return await u.message.reply_text(tr(lang,"only_admin"))
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
    if not is_admin(u.effective_user.id): return await u.message.reply_text(tr(detect_lang(""),"only_admin"))
    parts=(u.message.text or "").split(" ",1)
    if len(parts)<2: return await u.message.reply_text("Uso: /setbio <texto>")
    CFG["bio"]=parts[1].strip(); save_cfg(CFG); await u.message.reply_text("Bio actualizada.")

async def setname_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return await u.message.reply_text(tr(detect_lang(""),"only_admin"))
    parts=(u.message.text or "").split(" ",1)
    if len(parts)<2: return await u.message.reply_text("Uso: /setname <nombre>")
    CFG["model_name"]=parts[1].strip(); save_cfg(CFG); await u.message.reply_text("Nombre actualizado.")

async def setschedule_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return await u.message.reply_text(tr(detect_lang(""),"only_admin"))
    parts=(u.message.text or "").split(" ",1)
    if len(parts)<2: return await u.message.reply_text("Uso: /setschedule <horario>")
    CFG["schedule"]=parts[1].strip(); save_cfg(CFG); await u.message.reply_text("Horario actualizado.")

async def setlangs_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return await u.message.reply_text(tr(detect_lang(""),"only_admin"))
    arr=[x for x in " ".join(c.args).replace(" ","").split(",") if x in SUPPORTED]
    if not arr: return await u.message.reply_text("Idiomas no válidos. Usa de,en,pl,sv,es")
    CFG["audience_langs"]=arr; save_cfg(CFG); await u.message.reply_text("Idiomas: "+",".join(arr))

async def dm_text(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global last_user_lang
    txt=u.message.text or ""; last_user_lang=detect_lang(txt)
    nick=(u.effective_user.username or u.effective_user.first_name) if u.effective_user else None
    await send_menu(u.effective_chat.id, c.bot, last_user_lang, nick)

async def group_text(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global last_user_lang, last_offline_hint
    text=u.message.text or ""; lang=detect_lang(text); last_user_lang=lang
    if str(u.effective_chat.id)==str(CHANNEL_ID) and not (u.effective_user and u.effective_user.is_bot):
        push_overlay(f"{u.effective_user.full_name}: {text}")
        push_studio(f"{u.effective_user.full_name}: {text}")
    if not LIVE_ACTIVE and (datetime.utcnow()-last_offline_hint) >= timedelta(minutes=OFFLINE_HINT_COOLDOWN_MIN):
        last_offline_hint = datetime.utcnow()
        await c.bot.send_message(u.effective_chat.id, tr(lang,"offline", name=CFG["model_name"], schedule=CFG["schedule"]),
                                 parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=kb_don(lang, (u.effective_user.username if u.effective_user else None)))
    else:
        await send_menu(u.effective_chat.id, c.bot, lang, (u.effective_user.username if u.effective_user else None))

async def channel_post(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.channel_post and u.channel_post.text:
        push_overlay(f"📢 {u.channel_post.text}")
        push_studio(f"📢 {u.channel_post.text}")

async def tick(app):
    global last_ad
    while True:
        try:
            try:
                while True:
                    payer, amount, lang = pending_celebrates.get_nowait()
                    await celebrate(app.bot, int(CHANNEL_ID), payer, amount, "¡Gracias por tu apoyo!", lang)
            except queue.Empty:
                pass
            if LIVE_ACTIVE and CHANNEL_ID:
                if (datetime.utcnow()-last_ad) >= timedelta(minutes=ANNOUNCE_EVERY_MIN):
                    lang = last_user_lang or "de"
                    await send_menu(int(CHANNEL_ID), app.bot, lang)
                    last_ad = datetime.utcnow()
            await asyncio.sleep(20)
        except Exception as e:
            log.error(f"scheduler: {e}")
            await asyncio.sleep(5)

async def on_startup(app): app.create_task(tick(app))

_app=None
def telegram_app_singleton():
    global _app
    if _app: return _app
    _app=(ApplicationBuilder().token(TOKEN).post_init(on_startup).build())
    _app.add_handler(CommandHandler("start", start_cmd))
    _app.add_handler(CommandHandler("studio", studio_cmd))
    _app.add_handler(CommandHandler("menu", menu_cmd))
    _app.add_handler(CommandHandler("precios", precios_cmd))
    _app.add_handler(CommandHandler("status", status_cmd))
    _app.add_handler(CommandHandler("liveon", liveon_cmd))
    _app.add_handler(CommandHandler("liveoff", liveoff_cmd))
    _app.add_handler(CommandHandler("addprice", addprice_cmd))
    _app.add_handler(CommandHandler("delprice", delprice_cmd))
    _app.add_handler(CommandHandler("listprices", listprices_cmd))
    _app.add_handler(CommandHandler("setbio", setbio_cmd))
    _app.add_handler(CommandHandler("setname", setname_cmd))
    _app.add_handler(CommandHandler("setschedule", setschedule_cmd))
    _app.add_handler(CommandHandler("setlangs", setlangs_cmd))
    _app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, dm_text))
    _app.add_handler(MessageHandler(filters.TEXT & (~filters.ChatType.PRIVATE) & ~filters.ChatType.CHANNEL, group_text))
    _app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post))
    return _app

def run_web(): web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__=="__main__":
    app=telegram_app_singleton()
    t=threading.Thread(target=run_web, daemon=True); t.start()
    log.info("🤖 Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
