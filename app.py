import os
import threading
from datetime import datetime
from collections import defaultdict

from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from deep_translator import GoogleTranslator

# ==========================
# CONFIGURACIÓN BÁSICA
# ==========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN en variables de entorno")

# URL base de tu servicio en Render (para construir los links de overlay)
BASE_URL = os.environ.get("BASE_URL", "https://cosplaylive.onrender.com")

# Flask
app = Flask(__name__)

# Telegram Application (async, PTB v20)
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Traductor (a alemán, desde cualquier idioma)
translator_de = GoogleTranslator(source="auto", target="de")

# ==========================
# LOG DE CHAT PARA MIRROR / HUD
# ==========================

CHAT_LOGS = defaultdict(list)
MAX_LOG_MESSAGES = 200


def add_chat_log(chat_id: int, user_display: str, text: str, translated: str):
    """
    Guarda un mensaje en el log de un chat para mostrarlo en el overlay/HUD.
    """
    if not text:
        return
    entry = {
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "user": user_display,
        "text": text,
        "translated": translated or "",
    }
    lst = CHAT_LOGS[chat_id]
    lst.append(entry)
    # Limitamos el tamaño del log
    if len(lst) > MAX_LOG_MESSAGES:
        CHAT_LOGS[chat_id] = lst[-100:]


# ==========================
# COMANDOS BÁSICOS DEL BOT
# ==========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mensaje de bienvenida: explica traducción + overlays.
    """
    await update.effective_message.reply_text(
        "Hallo! Ich bin der Cosplay Live Helper Bot. 🧋\n\n"
        "Funktionen:\n"
        "• Übersetzt Nachrichten automatisch ins Deutsche, damit das Model alles versteht. 🇩🇪\n"
        "• /overlaylink – gibt dir einen Link zum Spiegel (Mirror) des Chats.\n"
        "• /overlayhud – gibt dir einen Link zu einem transparenten HUD-Overlay,\n"
        "  das du als schwebendes Fenster über deiner Kamera verwenden kannst."
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Solo informa el ID del usuario (útil para debug).
    """
    user = update.effective_user
    await update.effective_message.reply_text(f"✅ Dein User-ID ist: {user.id}")


async def cmd_overlaylink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Devuelve el link del overlay clásico (lista de mensajes) para el chat actual.
    """
    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    overlay_url = f"{BASE_URL}/overlay?chat_id={chat_id}"

    await update.effective_message.reply_text(
        "🪞 Spiegel / Mirror dieses Chats:\n"
        f"{overlay_url}\n\n"
        "Öffne diesen Link z.B. auf einem zweiten Gerät, um den Chat groß zu sehen."
    )


async def cmd_overlayhud(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Devuelve el link del HUD transparente para el chat actual.
    Ideal para usarlo en un navegador flotante sobre la pantalla.
    """
    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    hud_url = f"{BASE_URL}/overlayhud?chat_id={chat_id}"

    await update.effective_message.reply_text(
        "🪞 Transparentes HUD-Overlay für diesen Chat:\n"
        f"{hud_url}\n\n"
        "Tipp: Öffne diesen Link in einem schwebenden/Overlay-Browser, "
        "dann siehst du die Kamera in Telegram und den Chat darüber."
    )


# ==========================
# TRADUCCIÓN EN EL CHAT
# ==========================

async def translate_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Traduce cada mensaje de texto al alemán y responde con la traducción,
    además de guardarlo en el log para el overlay/HUD.
    """
    msg = update.effective_message
    if not msg:
        return
    if msg.from_user and msg.from_user.is_bot:
        return

    text = msg.text or msg.caption
    if not text:
        return

    # Nombre amigable del usuario
    user = msg.from_user
    if user:
        if user.username:
            user_display = f"@{user.username}"
        else:
            fullname = (user.first_name or "") + " " + (user.last_name or "" if user.last_name else "")
            user_display = fullname.strip() or "User"
    else:
        user_display = "User"

    translated = ""
    try:
        translated = translator_de.translate(text)
    except Exception as e:
        print(f"[translate_in_chat] Error traduciendo: {e}")
        translated = ""

    # Guardar en el log del chat (con o sin traducción)
    chat = update.effective_chat
    if chat:
        add_chat_log(chat.id, user_display, text, translated)

    # Si la traducción es vacía o igual al original, no respondemos
    if not translated:
        return
    if translated.strip().lower() == text.strip().lower():
        return

    await msg.reply_text(f"🌐 {translated}")


# ==========================
# FLASK: RUTAS WEB (OVERLAYS)
# ==========================

@app.route("/")
def index():
    return (
        "<h1>Cosplay Live Helper Bot</h1>"
        "<p>Bot de traducción y overlays funcionando.</p>"
        "<p>Usa /overlaylink o /overlayhud en Telegram para obtener los enlaces.</p>"
    )


@app.route("/overlay")
def overlay():
    """
    Página HTML que muestra el mirror del chat (lista completa de mensajes).
    Se llama con ?chat_id=<id>.
    """
    chat_id = request.args.get("chat_id", "").strip()
    if not chat_id:
        return (
            "<h2>Overlay / Mirror</h2>"
            "<p>Falta el parámetro <code>chat_id</code>.</p>"
            "<p>Genera el enlace desde Telegram con /overlaylink en el Chat.</p>"
        )

    html = f"""
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8" />
<title>Chat Overlay</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<style>
    body {{
        margin: 0;
        padding: 0;
        background: #000;
        color: #fff;
        font-family: system-ui, sans-serif;
        display: flex;
        flex-direction: column;
        height: 100vh;
    }}
    header {{
        padding: 8px 12px;
        font-size: 14px;
        background: #111;
        border-bottom: 1px solid #333;
    }}
    #chat {{
        flex: 1;
        overflow-y: auto;
        padding: 8px;
    }}
    .msg {{
        margin-bottom: 8px;
        padding: 6px 8px;
        border-radius: 6px;
        background: #111;
        border: 1px solid #333;
        font-size: 14px;
    }}
    .meta {{
        font-size: 11px;
        color: #aaa;
        margin-bottom: 3px;
    }}
    .original {{
        font-size: 13px;
        color: #eee;
    }}
    .translated {{
        font-size: 13px;
        color: #6cf;
        margin-top: 3px;
    }}
</style>
</head>
<body>
<header>
    Chat Overlay – Chat ID: {chat_id}
</header>
<div id="chat"></div>

<script>
const chatId = "{chat_id}";
const chatDiv = document.getElementById('chat');

async function loadMessages() {{
    try {{
        const res = await fetch('/overlay_data?chat_id=' + encodeURIComponent(chatId));
        if (!res.ok) return;
        const data = await res.json();
        const msgs = data.messages || [];
        chatDiv.innerHTML = '';

        for (const m of msgs) {{
            const div = document.createElement('div');
            div.className = 'msg';

            const meta = document.createElement('div');
            meta.className = 'meta';
            meta.textContent = `[${{m.time}}] ${{m.user}}`;
            div.appendChild(meta);

            const orig = document.createElement('div');
            orig.className = 'original';
            orig.textContent = m.text || '';
            div.appendChild(orig);

            if (m.translated && m.translated.trim() !== '') {{
                const tr = document.createElement('div');
                tr.className = 'translated';
                tr.textContent = '🌐 ' + m.translated;
                div.appendChild(tr);
            }}

            chatDiv.appendChild(div);
        }}

        chatDiv.scrollTop = chatDiv.scrollHeight;
    }} catch (e) {{
        console.error('Error loading messages', e);
    }}
}}

loadMessages();
setInterval(loadMessages, 2000);
</script>
</body>
</html>
    """
    return html


@app.route("/overlay_data")
def overlay_data():
    """
    Devuelve los mensajes del chat en JSON para el overlay / HUD.
    """
    chat_id = request.args.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"messages": []})

    try:
        cid = int(chat_id)
    except ValueError:
        return jsonify({"messages": []})

    msgs = CHAT_LOGS.get(cid, [])
    return jsonify({"messages": msgs})


@app.route("/overlayhud")
def overlay_hud():
    """
    HUD transparente para superponer sobre la pantalla.
    Muestra hasta 3 mensajes recientes, con fondo semitransparente.
    Ideal para usar en un navegador flotante / Picture-in-Picture.
    """
    chat_id = request.args.get("chat_id", "").strip()
    if not chat_id:
        return "<h3>Falta chat_id</h3>"

    html = f"""
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8" />
<title>HUD Overlay</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0" />

<style>
    body {{
        margin: 0;
        padding: 0;
        background: rgba(0,0,0,0);
        font-family: system-ui, sans-serif;
        overflow: hidden;
    }}

    #msgs {{
        position: fixed;
        bottom: 5%;
        left: 5%;
        right: 5%;
        display: flex;
        flex-direction: column;
        gap: 12px;
        color: #ffffff;
        font-size: 22px;
        text-shadow: 0px 0px 5px #000;
        pointer-events: none; /* para que no interfiera con toques en pantalla */
    }}

    .msg {{
        background: rgba(0,0,0,0.35);
        padding: 8px 12px;
        border-radius: 10px;
        animation: fadein 0.4s ease-out;
    }}

    @keyframes fadein {{
        from {{ opacity: 0; transform: translateY(20px); }}
        to {{ opacity: 1; transform: translateY(0); }}
    }}
</style>
</head>

<body>
<div id="msgs"></div>

<script>
const chatId = "{chat_id}";
const div = document.getElementById("msgs");

async function loadHUD() {{
    try {{
        const res = await fetch("/overlay_data?chat_id=" + encodeURIComponent(chatId));
        const data = await res.json();
        const list = data.messages || [];

        const last = list.slice(-3);  // últimos 3 mensajes
        div.innerHTML = "";

        last.forEach(m => {{
            const d = document.createElement("div");
            d.className = "msg";
            const translated = m.translated || "";
            d.innerHTML = `
                <b>${{m.user}}:</b> ${{m.text}}<br>
                <span style="color:#6cf;">${{translated}}</span>
            `;
            div.appendChild(d);
        }});
    }} catch (e) {{
        console.error("HUD error", e);
    }}
}}

setInterval(loadHUD, 1500);
loadHUD();
</script>

</body>
</html>
    """
    return html


# ==========================
# REGISTRO DE HANDLERS
# ==========================

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("whoami", cmd_whoami))
application.add_handler(CommandHandler("overlaylink", cmd_overlaylink))
application.add_handler(CommandHandler("overlayhud", cmd_overlayhud))

# Traducción para todos los mensajes de texto en chats donde esté el bot
application.add_handler(
    MessageHandler(filters.TEXT & (~filters.COMMAND), translate_in_chat)
)


# ==========================
# ARRANQUE BOT + FLASK
# ==========================

def start_bot():
    # stop_signals=None porque corremos en un hilo secundario (junto a Flask)
    application.run_polling(drop_pending_updates=True, stop_signals=None)


bot_thread = threading.Thread(target=start_bot, name="tg-bot", daemon=True)
bot_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
