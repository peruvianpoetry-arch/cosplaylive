# app.py — CosplayLive vFinal: IA + autoactividad + traducción + donaciones
import os, sys, time, threading, logging, queue, random
from flask import Flask, Response, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ==== Config / Logging ====
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("cosplaylive")

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT  = int(os.getenv("PORT", "10000"))
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
ALLOW_ADULT = os.getenv("ALLOW_ADULT", "0") == "1"

# ==== Cola Overlay ====
events: "queue.Queue[str]" = queue.Queue(maxsize=200)
def push_event(msg:str):
    msg=(msg or"").replace("\n"," ").strip()
    if not msg: return
    try: events.put_nowait(msg)
    except queue.Full:
        try: events.get_nowait()
        except queue.Empty: pass
        events.put_nowait(msg)

# ==== Flask (overlay + webhook) ====
web=Flask(__name__)

@web.get("/")
def home(): return "✅ CosplayLive bot online"

@web.get("/overlay")
def overlay():
    html="""<!doctype html><html><head><meta charset=utf-8>
<style>
body{background:transparent;margin:0;font:18px system-ui;color:#fff}
.msg{background:rgba(0,0,0,.4);margin:6px;padding:8px 12px;border-radius:12px}
</style></head><body><div id=c></div>
<script>
let c=document.getElementById('c');
let es=new EventSource('/events');
es.onmessage=e=>{
 let d=document.createElement('div');d.className='msg';d.textContent=e.data;
 c.appendChild(d);while(c.children.length>40)c.removeChild(c.firstChild);
 window.scrollTo(0,document.body.scrollHeight);
};
</script></body></html>"""
    return html

@web.get("/events")
def sse():
    def stream():
        yield "event: ping\ndata: 💓\n\n"
        while True:
            import time
            try: yield f"data: {events.get(timeout=20)}\n\n"
            except queue.Empty: yield "event: ping\ndata: 💓\n\n"
    headers={"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"}
    return Response(stream(),mimetype="text/event-stream",headers=headers)

# ==== Donaciones y meta ====
META_OBJETIVO = 50.0
meta_actual = 0.0

def registrar_donacion(monto:float, usuario:str):
    global meta_actual
    meta_actual += monto
    if meta_actual > META_OBJETIVO: meta_actual = META_OBJETIVO
    texto=f"🎉 {usuario} aportó {monto:.2f} € · Meta: {meta_actual:.2f}/{META_OBJETIVO:.2f} €"
    push_event(texto)
    return texto

# Stripe (solo registra, no crea pagos reales en esta demo)
@web.post("/stripe/webhook")
def stripe_webhook():
    log.info("Webhook Stripe recibido.")
    return ("ok",200)

# ==== IA y traducción ====
from deep_translator import GoogleTranslator
def traducir(txt,src="auto",dest="es"):
    try: return GoogleTranslator(source=src,target=dest).translate(txt)
    except: return txt

async def ia_responder(prompt:str)->str:
    """Usa GPT-4o si hay API_KEY; si no, respuestas locales."""
    if OPENAI_KEY:
        import openai
        openai.api_key=OPENAI_KEY
        try:
            rsp=openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":"Eres un asistente divertido y amigable para un canal de cosplay."},
                          {"role":"user","content":prompt}],
                temperature=0.8,
                max_tokens=120
            )
            return rsp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("OpenAI error: %s",e)
    # fallback local
    base=["😄 Hola! ¿Listo para el show?","✨ Gracias por pasarte por el canal!",
          "💬 Puedo contarte cómo donar o cuándo será el próximo show.","🎁 Cada aporte ayuda a seguir transmitiendo."]
    return random.choice(base)

# ==== Autoactividad ====
ULTIMO_MSG=time.time()
INACT_MIN=15
ACTIVO=False
def watcher():
    global ACTIVO
    while True:
        if ACTIVO and (time.time()-ULTIMO_MSG>INACT_MIN*60):
            ACTIVO=False
            push_event("💤 Sala en pausa por inactividad.")
        time.sleep(30)

# ==== Handlers ====
async def start_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🤖 Bot activo. Usa /donar para apoyar o escribe para chatear.")

async def donar_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("💖 5 €",callback_data="tip_5"),
         InlineKeyboardButton("🎁 10 €",callback_data="tip_10"),
         InlineKeyboardButton("⭐ 20 €",callback_data="tip_20")],
        [InlineKeyboardButton("💶 Importe libre",callback_data="tip_custom")]]
    await u.message.reply_text("Selecciona tu donación:",reply_markup=InlineKeyboardMarkup(kb))

async def botones_cb(u:Update,c:ContextTypes.DEFAULT_TYPE):
    query=u.callback_query;await query.answer()
    data=query.data
    usuario=u.effective_user.full_name
    monto=0
    if data=="tip_5":monto=5
    elif data=="tip_10":monto=10
    elif data=="tip_20":monto=20
    elif data=="tip_custom":monto=random.choice([3,7,12])
    txt=registrar_donacion(monto,usuario)
    await query.message.reply_text(txt)

async def texto(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global ACTIVO,ULTIMO_MSG
    ACTIVO=True;ULTIMO_MSG=time.time()
    user=u.effective_user.full_name
    txt=u.message.text or ""
    log.info("%s: %s",user,txt)
    resp=await ia_responder(txt)
    resp_trad=traducir(resp,src="auto",dest="de")  # ejemplo alemán
    push_event(f"{user}: {txt}")
    push_event(f"🤖 {resp}")
    await u.message.reply_text(resp_trad)

# ==== Run ====
def run_web(): web.run(host="0.0.0.0",port=PORT,debug=False,use_reloader=False)

def run_polling():
    if not TOKEN: raise SystemExit("Falta TELEGRAM_TOKEN")
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CommandHandler("donar",donar_cmd))
    app.add_handler(CallbackQueryHandler(botones_cb))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND),texto))
    log.info("🤖 Bot CosplayLive ejecutándose…")
    app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)

if __name__=="__main__":
    threading.Thread(target=run_web,daemon=True).start()
    threading.Thread(target=watcher,daemon=True).start()
    run_polling()
