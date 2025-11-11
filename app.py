# prices_ext.py
# Persistencia simple de lista de precios para python-telegram-bot v20.x
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Dict, Any, List

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- Config ----------
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"
CURRENCY = os.getenv("CURRENCY", "EUR")

def _load() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {"prices": []}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"prices": []}

def _save(db: Dict[str, Any]) -> None:
    DATA_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

def _admin_ids() -> List[int]:
    # Soporta ADMIN_USER_IDS y ADMIN_USER_ID (fallback)
    raw = os.getenv("ADMIN_USER_IDS") or os.getenv("ADMIN_USER_ID") or ""
    ids = []
    for token in raw.replace(";", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            pass
    return ids

def _is_admin(user_id: int) -> bool:
    return user_id in _admin_ids()

def _parse_addprice(text: str) -> tuple[str, str] | None:
    """
    Acepta:
      /addprice Nombre 10
      /addprice Nombre 10€
      /addprice Nombre 10 EUR
      /addprice "Nombre con espacios" 12
    Devuelve (name, price_str) donde price_str ya lleva moneda (p.ej. '10 EUR').
    """
    parts = text.strip().split(" ", maxsplit=1)
    if len(parts) < 2:
        return None
    rest = parts[1].strip()

    # Si viene entre comillas, tratamos el nombre como bloque.
    name = None
    price_part = None
    if rest.startswith('"'):
        # Buscar cierre
        try:
            closing = rest.index('"', 1)
            name = rest[1:closing].strip()
            price_part = rest[closing+1:].strip()
        except ValueError:
            return None
    else:
        # Nombre = todo menos la última "palabra" (el precio)
        tokens = rest.split()
        if len(tokens) < 2:
            return None
        name = " ".join(tokens[:-1])
        price_part = tokens[-1]

    if not name:
        return None

    # Normalizar precio
    p = price_part.replace("€", "").replace(",", ".").strip()
    # Si es solo número, añadimos moneda por defecto
    if p.replace(".", "", 1).isdigit():
        price_str = f"{p} {CURRENCY}"
    else:
        # Puede venir como "10EUR" o "10USD" o "10 eur"
        # Separamos primer bloque numérico del resto
        num = ""
        suf = ""
        for ch in p:
            if ch.isdigit() or ch == ".":
                num += ch
            else:
                suf += ch
        suf = suf.strip().upper().replace("EUR", "EUR").replace("EURO", "EUR")
        if not num:
            return None
        price_str = f"{num} {suf or CURRENCY}"
    return name.strip(), price_str.strip()

# ---------- Handlers ----------
async def cmd_addprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.message.reply_text("Solo admin.")
        return

    parsed = _parse_addprice(update.message.text)
    if not parsed:
        await update.message.reply_text("Usa: /addprice Nombre 10  (puedes usar 10€, 10 EUR o \"Nombre con espacios\" 10)")
        return

    name, price_str = parsed
    db = _load()
    prices = db.get("prices", [])

    # Si existe el nombre, actualiza; si no, agrega
    for row in prices:
        if row.get("name", "").lower() == name.lower():
            row["name"] = name
            row["price"] = price_str
            break
    else:
        prices.append({"name": name, "price": price_str})

    db["prices"] = prices
    _save(db)
    await update.message.reply_text("💰 Precio guardado.")

async def cmd_delprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.message.reply_text("Solo admin.")
        return

    args = update.message.text.split(" ", maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usa: /delprice Nombre")
        return
    target = args[1].strip().lower()

    db = _load()
    prices = db.get("prices", [])
    new_prices = [r for r in prices if r.get("name", "").lower() != target]
    if len(new_prices) == len(prices):
        await update.message.reply_text("No encontré ese nombre.")
        return
    db["prices"] = new_prices
    _save(db)
    await update.message.reply_text("🗑️ Precio eliminado.")

async def cmd_listprices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _load()
    prices = db.get("prices", [])
    if not prices:
        await update.message.reply_text("No hay precios configurados.")
        return
    lines = ["💵 *Lista de precios:*"]
    for row in prices:
        lines.append(f"• {row.get('name')} — {row.get('price')}")
    await update.message.reply_markdown_v2("\n".join(lines))

async def cmd_resetprices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await update.message.reply_text("Solo admin.")
        return
    _save({"prices": []})
    await update.message.reply_text("♻️ Precios reiniciados.")

def register_price_handlers(app: Application) -> None:
    """
    Llama a esta función desde tu app principal para registrar los comandos.
    No interfiere con nada existente.
    """
    app.add_handler(CommandHandler("addprice", cmd_addprice))
    app.add_handler(CommandHandler("delprice", cmd_delprice))
    app.add_handler(CommandHandler("listprices", cmd_listprices))
    app.add_handler(CommandHandler("resetprices", cmd_resetprices))
