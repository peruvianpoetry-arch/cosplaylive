# --- reemplaza la función de anuncio actual por esta ---
async def announce_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(chat_id=chat_id,
                                       text=ANNOUNCE_TEXT_DE)
        # Log visible en Render
        print(f"[ANNOUNCE] Sent to {chat_id}")
    except Exception as e:
        print(f"[ANNOUNCE][ERR] {e}")

# --- reemplaza liveon por esta versión robusta ---
async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    target = _state["chat_id"] or chat_id   # usa chat fijado si existe
    set_live(target, True)

    kb = InlineKeyboardMarkup(price_rows())
    await context.bot.send_message(chat_id=target, text=WELCOME_TEXT_DE)
    await context.bot.send_message(
        chat_id=target, text=prices_menu_text_de(),
        parse_mode="Markdown", reply_markup=kb
    )

    # Limpia jobs anteriores con el mismo nombre
    for job in context.job_queue.get_jobs_by_name(f"auto_ads_{target}"):
        job.schedule_removal()

    # Programa anuncios usando chat_id del Job (más fiable)
    context.job_queue.run_repeating(
        announce_job,
        interval=ANNOUNCE_EVERY_MIN * 60,
        first=ANNOUNCE_EVERY_MIN * 60,
        chat_id=target,
        name=f"auto_ads_{target}",
    )

    if update.message and (chat_id != target):
        await update.message.reply_text("🟢 Live activado en la sala fijada.")

# --- reemplaza liveoff por esta ---
async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = _state["chat_id"] or (update.effective_chat.id if update.effective_chat else None)
    if not chat_id:
        return
    set_live(chat_id, False)
    for job in context.job_queue.get_jobs_by_name(f"auto_ads_{chat_id}"):
        job.schedule_removal()
    if update.message:
        await update.message.reply_text("🔴 Live desactiviert.")
