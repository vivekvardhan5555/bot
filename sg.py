"""
OTPDoctor Telegram Bot — Railway Deployable Version
"""

import os
import re
import asyncio
import json
import time
import requests as req
from dataclasses import dataclass, field
from typing import Optional
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required!")

BASE_URL      = "https://otpdoctor.in/stubs/handler_api.php"
CHECK_URL     = "https://trickhack.in/Swiggy/"
PAGE_SIZE     = 10
POLL_INTERVAL = 10
HARD_TIMEOUT  = 600
CHECK_EVERY   = 120
MAX_RETRIES   = 177
SWIGGY_RETRIES = 5
SWIGGY_DELAY  = 3

# ── Conversation states ───────────────────────────────────────────────────────

WAIT_API_KEY, WAIT_SEARCH, WAIT_COUNT = range(3)

# ── Persistent stores ─────────────────────────────────────────────────────────

USER_KEYS: dict[int, str] = {}
USED_NUMBERS: set[str] = set()

# ── OTPDoctor helpers ─────────────────────────────────────────────────────────

def api_raw(params: dict, api_key: str, timeout: int = 60) -> str:
    p = dict(params)
    p["api_key"] = api_key
    try:
        r = req.get(BASE_URL, params=p, timeout=timeout)
        return r.text.strip()
    except Exception as e:
        return f"ERROR:{e}"

def get_balance(api_key: str) -> Optional[str]:
    raw = api_raw({"action": "getBalance"}, api_key)
    return raw.split(":", 1)[1] if raw.startswith("ACCESS_BALANCE:") else None

def fetch_services(api_key: str):
    try:
        r = req.get(BASE_URL, params={"action": "getServices", "api_key": api_key, "country": "in"}, timeout=30)
        text = r.text.strip()
        if text.startswith("{"):
            data = json.loads(text)
            return sorted([
                {"id": sid, "name": info.get("service_name", "").strip(),
                 "price": info.get("service_price", "?"), "server": info.get("server_name", "")}
                for sid, info in data.items()
            ], key=lambda x: x["name"].lower()), None
        return None, text
    except Exception as e:
        return None, str(e)

def get_number(api_key: str, service_id: str):
    raw = api_raw({"action": "getNumber", "service": service_id}, api_key)
    if raw.startswith("ACCESS_NUMBER:"):
        parts    = raw.split(":")
        order_id = parts[1].strip()
        phone    = parts[2].strip()
        if phone.startswith("91") and len(phone) == 12:
            phone = phone[2:]
        return order_id, phone, None
    return None, None, raw

def get_status(api_key: str, order_id: str) -> str:
    return api_raw({"action": "getStatus", "id": order_id}, api_key)

def set_status(api_key: str, order_id: str, code: int) -> str:
    return api_raw({"action": "setStatus", "id": order_id, "status": code}, api_key)

# ── Swiggy web check with RETRY ──────────────────────────────────────────────

def check_swiggy(phone: str) -> tuple[str, str]:
    try:
        r = req.post(CHECK_URL, data={"mobile": phone}, timeout=30)
        m = re.search(
            r'<div[^>]*class="result\s+(success|error|info)"[^>]*>(.*?)</div>',
            r.text, re.DOTALL
        )
        if m:
            cls  = m.group(1)
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            icon = {"success": "✅", "error": "❌", "info": "ℹ️"}.get(cls, "❓")
            return icon, text
        return "❓", "No result returned from site."
    except Exception as e:
        return "⚠️", f"Check failed: {e}"

def check_swiggy_with_retry(phone: str) -> tuple[str, str]:
    for attempt in range(1, SWIGGY_RETRIES + 1):
        icon, text = check_swiggy(phone)
        if icon in ["✅", "❌", "ℹ️"]:
            return icon, text
        if attempt < SWIGGY_RETRIES:
            time.sleep(SWIGGY_DELAY)
    return "❓", f"Unknown response after {SWIGGY_RETRIES} retries"

def is_registered_on_swiggy(phone: str) -> bool:
    icon, text = check_swiggy_with_retry(phone)
    if "already" in text.lower() or "registered" in text.lower():
        return True
    if "exists" in text.lower():
        return True
    if icon == "✅" and ("account" in text.lower() or "user" in text.lower()):
        return True
    return False

# ── Session / Order model ─────────────────────────────────────────────────────

@dataclass
class Order:
    idx:        int
    order_id:   str
    phone:      str
    otps:       list = field(default_factory=list)
    status:     str  = "pending"
    attempts:   int  = 0
    is_registered: bool = False
    swiggy_icon: str = ""
    swiggy_text: str = ""

@dataclass
class Session:
    api_key:      str    = ""
    services:     list   = field(default_factory=list)
    search_hits:  list   = field(default_factory=list)
    search_page:  int    = 0
    service_id:   str    = ""
    service_name: str    = ""
    listed_price: str    = ""
    count:        int    = 0
    orders:       list   = field(default_factory=list)
    poll_task:    object = None
    cancelled_numbers: set = field(default_factory=set)

SESSIONS: dict[int, Session] = {}

def sess(chat_id: int) -> Session:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = Session()
    return SESSIONS[chat_id]

# ── Safe Telegram helpers ─────────────────────────────────────────────────────

async def safe_answer(query, text=""):
    try:
        await query.answer(text)
    except (BadRequest, TimedOut):
        pass

async def safe_edit_text(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except (BadRequest, TimedOut):
        pass

async def safe_edit_markup(query, markup):
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except (BadRequest, TimedOut):
        pass

async def safe_send(chat_id, app, text, **kwargs):
    try:
        await app.bot.send_message(chat_id, text, **kwargs)
    except (BadRequest, TimedOut):
        pass

# ── Keyboards ─────────────────────────────────────────────────────────────────

def service_keyboard(hits: list, page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = hits[start: start + PAGE_SIZE]
    rows  = []
    for i, s in enumerate(chunk):
        label = f"{s['name'][:22]}  ₹{s['price']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"svc:{start+i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page:{page-1}"))
    if start + PAGE_SIZE < len(hits):
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔍 New search", callback_data="newsearch")])
    return InlineKeyboardMarkup(rows)

def pre_poll_keyboard(order_id: str, is_registered: bool) -> InlineKeyboardMarkup:
    rows = []
    if is_registered:
        rows.append([InlineKeyboardButton(
            "⚠️ Already Registered — Auto-cancel in 2 min",
            callback_data="dummy"
        )])
    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"precancel:{order_id}"),
        InlineKeyboardButton("✅ Keep & poll", callback_data=f"keepoll:{order_id}"),
    ])
    return InlineKeyboardMarkup(rows)

def start_poll_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Start polling all remaining numbers", callback_data="startpoll"),
    ]])

def otp_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done — finalize",    callback_data=f"done:{order_id}"),
        InlineKeyboardButton("🔄 Another OTP coming", callback_data=f"more:{order_id}"),
    ]])

def wait_keyboard(orders: list) -> InlineKeyboardMarkup:
    rows = []
    for o in orders:
        rows.append([InlineKeyboardButton(
            f"❌ Cancel {o.phone}", callback_data=f"cancel_one:{o.order_id}"
        )])
    rows.append([InlineKeyboardButton("❌ Cancel ALL",           callback_data="cancel_all")])
    rows.append([InlineKeyboardButton("⏳ Keep waiting (2 min)", callback_data="keep_wait")])
    return InlineKeyboardMarkup(rows)

# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in USER_KEYS:
        api_key = USER_KEYS[user_id]
        SESSIONS[chat_id] = Session(api_key=api_key)
        s = sess(chat_id)
        msg = await update.message.reply_text("⏳ Loading India services…")
        services, err = fetch_services(api_key)
        if err or not services:
            await msg.edit_text(f"❌ Could not load services: {err or 'empty'}. Try /start again.")
            return ConversationHandler.END
        s.services = services
        bal = get_balance(api_key)
        await msg.edit_text(
            f"✅ Balance: ₹{bal}  |  {len(services)} services available\n\n"
            "🔍 *Search a service* — type a name (e.g. `swiggy`, `telegram`, `zomato`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WAIT_SEARCH

    SESSIONS[chat_id] = Session()
    await update.message.reply_text(
        "👋 *OTPDoctor Bot*\n\nSend me your OTPDoctor API key to begin.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_API_KEY

# ── Step 1 — API key ──────────────────────────────────────────────────────────

async def recv_api_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key     = update.message.text.strip()
    msg     = await update.message.reply_text("🔑 Checking key…")

    bal = get_balance(key)
    if bal is None:
        await msg.edit_text("❌ Invalid API key. Send a valid key:")
        return WAIT_API_KEY

    USER_KEYS[user_id] = key
    s = sess(chat_id)
    s.api_key = key

    await msg.edit_text(f"✅ Balance: ₹{bal}\n\n⏳ Loading India services…")
    services, err = fetch_services(key)
    if err or not services:
        await msg.edit_text(f"❌ Could not load services: {err or 'empty'}. Try /start again.")
        return ConversationHandler.END

    s.services = services
    await msg.edit_text(
        f"✅ Balance: ₹{bal}  |  {len(services)} services available\n\n"
        "🔍 *Search a service* — type a name (e.g. `swiggy`, `telegram`, `zomato`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_SEARCH

# ── Step 2 — search / pick ────────────────────────────────────────────────────

async def recv_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    query   = update.message.text.strip().lower()
    s       = sess(chat_id)

    hits = [sv for sv in s.services if query in sv["name"].lower()] if query else s.services
    if not hits:
        await update.message.reply_text(
            f"❌ No service matching *{query}*. Try another name:",
            parse_mode=ParseMode.MARKDOWN
        )
        return WAIT_SEARCH

    s.search_hits = hits
    s.search_page = 0
    text = (f"Found *{len(hits)}* service(s) for `{query}` — pick one:"
            if query else f"All *{len(hits)}* services — pick one:")
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=service_keyboard(hits, 0),
    )
    return WAIT_SEARCH

async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query   = update.callback_query
    chat_id = query.message.chat_id
    page    = int(query.data.split(":")[1])
    s       = sess(chat_id)
    s.search_page = page
    await safe_answer(query)
    await safe_edit_markup(query, service_keyboard(s.search_hits, page))
    return WAIT_SEARCH

async def cb_newsearch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer(query)
    await safe_edit_text(query, "🔍 Type a service name to search:")
    return WAIT_SEARCH

async def cb_pick_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query   = update.callback_query
    chat_id = query.message.chat_id
    idx     = int(query.data.split(":")[1])
    s       = sess(chat_id)

    svc = s.search_hits[idx]
    s.service_id   = svc["id"]
    s.service_name = svc["name"]
    s.listed_price = svc["price"]

    await safe_answer(query)
    await safe_edit_text(
        query,
        f"✅ *{svc['name']}*  |  ₹{svc['price']}  |  {svc['server']}\n\n"
        "How many numbers do you want? (1–20)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_COUNT

# ── Step 3 — count → parallel purchase ──────────────────────────────────────

async def _buy_one(
    idx: int,
    api_key: str,
    service_id: str,
    status_map: dict,
    chat_id: int,
    app,
) -> Order:
    attempt = 0
    while attempt < MAX_RETRIES:
        attempt += 1
        status_map[idx] = f"⟳ [{idx}] attempt {attempt}/{MAX_RETRIES}…"
        
        if attempt % 30 == 0 and attempt > 1:
            await safe_send(
                chat_id,
                app,
                f"⏳ *{attempt} attempts* for number #{idx}.\n"
                f"Still waiting for OTPDoctor to assign a number.\n\n"
                f"Do you want to *continue* or *stop*?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶ Continue", callback_data=f"retry_continue:{idx}"),
                    InlineKeyboardButton("⏹ Stop", callback_data=f"retry_stop:{idx}"),
                ]])
            )
            s = sess(chat_id)
            if f"stop_{idx}" in s.__dict__:
                o = Order(idx=idx, order_id="", phone="")
                o.status = "cancelled"
                o.attempts = attempt
                return o

        order_id, phone, err = get_number(api_key, service_id)
        
        if order_id:
            if phone in USED_NUMBERS:
                await safe_send(
                    chat_id,
                    app,
                    f"ℹ️ Number `{phone}` is already checked.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                USED_NUMBERS.add(phone)
            
            status_map[idx] = f"✅ [{idx}] `{phone}`"
            o = Order(idx=idx, order_id=order_id, phone=phone)
            o.attempts = attempt
            return o
            
        elif err == "NO_NUMBERS":
            status_map[idx] = f"⟳ [{idx}] no numbers — attempt {attempt}/{MAX_RETRIES}, retrying…"
            await asyncio.sleep(3)
        else:
            status_map[idx] = f"❌ [{idx}] failed: {err}"
            o = Order(idx=idx, order_id="", phone="")
            o.status = "failed"
            o.attempts = attempt
            o._err = err
            return o

    status_map[idx] = f"⏰ [{idx}] max retries ({MAX_RETRIES}) reached — stopped"
    o = Order(idx=idx, order_id="", phone="")
    o.status = "timeout"
    o.attempts = MAX_RETRIES
    o._err = f"Max retries ({MAX_RETRIES}) exceeded"
    
    await safe_send(
        chat_id,
        app,
        f"🛑 *Operation stopped* for number #{idx}.\n"
        f"Max retries ({MAX_RETRIES}) reached.\n\n"
        f"Send /start to try again.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return o

async def _live_status_updater(
    status_msg,
    header: str,
    status_map: dict,
    count: int,
    done_evt: asyncio.Event,
):
    last_text = ""
    while not done_evt.is_set():
        lines    = [status_map.get(i, f"⏳ [{i}] waiting…") for i in range(1, count + 1)]
        new_text = f"{header}\n\n" + "\n".join(lines)
        if new_text != last_text:
            try:
                await status_msg.edit_text(new_text, parse_mode=ParseMode.MARKDOWN)
                last_text = new_text
            except Exception:
                pass
        await asyncio.sleep(1)

# ── Retry decision callbacks ──────────────────────────────────────────────────

async def cb_retry_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    idx = int(query.data.split(":")[1])
    chat_id = query.message.chat_id
    await safe_answer(query, "▶ Continuing…")
    await safe_edit_markup(query, None)
    await safe_send(chat_id, ctx.application, f"▶ Continuing attempts for number #{idx}…")

async def cb_retry_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    idx = int(query.data.split(":")[1])
    chat_id = query.message.chat_id
    s = sess(chat_id)
    s.__dict__[f"stop_{idx}"] = True
    await safe_answer(query, "⏹ Stopped")
    await safe_edit_markup(query, None)
    await safe_send(
        chat_id,
        ctx.application,
        f"🛑 *Operation stopped* for number #{idx}.\n\n"
        f"Send /start to try again.",
        parse_mode=ParseMode.MARKDOWN,
    )

# ── recv_count ───────────────────────────────────────────────────────────────

async def recv_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()
    s       = sess(chat_id)

    if not text.isdigit() or not (1 <= int(text) <= 20):
        await update.message.reply_text("⚠️ Enter a number between 1 and 20:")
        return WAIT_COUNT

    count    = int(text)
    s.count  = count
    s.orders = []

    header     = f"🛒 Purchasing *{count}* number(s) for *{s.service_name}*…"
    status_msg = await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)

    status_map: dict[int, str] = {i: f"⏳ [{i}] waiting…" for i in range(1, count + 1)}
    done_evt = asyncio.Event()

    updater_task = asyncio.create_task(
        _live_status_updater(status_msg, header, status_map, count, done_evt)
    )

    results: list[Order] = await asyncio.gather(
        *[_buy_one(i, s.api_key, s.service_id, status_map, chat_id, ctx.application)
          for i in range(1, count + 1)]
    )

    done_evt.set()
    await asyncio.sleep(1.2)
    updater_task.cancel()

    err_map = {"NO_BALANCE": "Insufficient balance", "BAD_SERVICE": "Invalid service ID", "TRY_AGAIN": "Temporary error"}
    lines = []
    for o in results:
        if o.status == "failed":
            err = getattr(o, "_err", "Unknown error")
            lines.append(f"❌ [{o.idx}] Failed: {err_map.get(err, err)}")
        elif o.status == "cancelled":
            lines.append(f"⏹ [{o.idx}] Stopped by user at attempt {o.attempts}")
        elif o.status == "timeout":
            lines.append(f"⏰ [{o.idx}] Max retries ({MAX_RETRIES}) reached")
        else:
            s.orders.append(o)
            lines.append(f"✅ [{o.idx}] `{o.phone}`")

    try:
        await status_msg.edit_text(
            f"🛒 *{s.service_name}* — {len(s.orders)}/{count} bought:\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except (BadRequest, TimedOut):
        await update.message.reply_text(
            f"🛒 *{s.service_name}* — {len(s.orders)}/{count} bought:\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )

    if not s.orders:
        await update.message.reply_text("❌ Could not buy any numbers. Use /start to try again.")
        return ConversationHandler.END

    is_swiggy = "swiggy" in s.service_name.lower()

    if is_swiggy:
        await update.message.reply_text("🔍 Checking each number on Swiggy...")
        for o in s.orders:
            icon, result_text = check_swiggy_with_retry(o.phone)
            
            o.swiggy_icon = icon
            o.swiggy_text = result_text
            o.is_registered = is_registered_on_swiggy(o.phone)
            
            registered_warning = " ⚠️ *Already registered!*" if o.is_registered else ""
            
            await update.message.reply_text(
                f"📞 `{o.phone}`\n"
                f"🌐 Swiggy check: {icon} {result_text}{registered_warning}\n\n"
                f"What do you want to do with this number?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=pre_poll_keyboard(o.order_id, o.is_registered),
            )
            await asyncio.sleep(0.3)

        await update.message.reply_text(
            "👆 Review numbers above — cancel any you don't need.\n\n"
            "⚠️ *Already registered* numbers will be *auto-cancelled after 2 minutes*.\n"
            "Non-registered numbers will NOT be auto-cancelled.\n\n"
            "When ready, tap below to start polling for OTPs:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=start_poll_keyboard(),
        )
        asyncio.create_task(auto_cancel_timer(chat_id, ctx.application))
    else:
        for o in s.orders:
            o.status = "polling"
        phones = "\n".join(f"📞 `{o.phone}`" for o in s.orders)
        await update.message.reply_text(
            f"*Numbers ready:*\n{phones}\n\n💰 ₹{s.listed_price} each\n\n⏳ Polling for OTPs…",
            parse_mode=ParseMode.MARKDOWN,
        )
        if s.poll_task and not s.poll_task.done():
            s.poll_task.cancel()
        s.poll_task = asyncio.create_task(poll_all(chat_id, ctx.application))

    return ConversationHandler.END

# ── Auto-cancel timer for REGISTERED numbers only ────────────────────────────

async def auto_cancel_timer(chat_id: int, app):
    await asyncio.sleep(120)
    s = sess(chat_id)
    
    pending = [o for o in s.orders if o.status == "pending" and o.is_registered]
    
    if pending:
        for o in pending:
            set_status(s.api_key, o.order_id, 8)
            o.status = "cancelled"
            await safe_send(
                chat_id,
                app,
                f"⏰ *Auto-cancelled* `{o.phone}` — already registered on Swiggy, 2 minutes passed without action.",
                parse_mode=ParseMode.MARKDOWN,
            )
        await safe_send(
            chat_id,
            app,
            f"⏰ *{len(pending)} registered number(s)* auto-cancelled after 2 minutes.\n"
            f"Send /start to try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        non_reg_pending = [o for o in s.orders if o.status == "pending" and not o.is_registered]
        if non_reg_pending:
            phones = ", ".join(f"`{o.phone}`" for o in non_reg_pending)
            await safe_send(
                chat_id,
                app,
                f"ℹ️ *{len(non_reg_pending)} non-registered number(s)* still pending:\n{phones}\n\n"
                f"They will NOT be auto-cancelled. Use the buttons above to keep or cancel them.",
                parse_mode=ParseMode.MARKDOWN,
            )

# ── Pre-poll buttons ──────────────────────────────────────────────────────────

async def cb_precancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_id  = query.message.chat_id
    order_id = query.data.split(":", 1)[1]
    s        = sess(chat_id)

    order = next((o for o in s.orders if o.order_id == order_id), None)
    if order and order.status == "pending":
        set_status(s.api_key, order_id, 8)
        order.status = "cancelled"
        s.cancelled_numbers.add(order.phone)
        await safe_answer(query, "❌ Cancelled")
        await safe_edit_markup(query, None)
        await query.message.reply_text(
            f"❌ `{order.phone}` has been *cancelled.*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await safe_answer(query, "Already handled.")

async def cb_keepoll(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_id  = query.message.chat_id
    order_id = query.data.split(":", 1)[1]
    s        = sess(chat_id)

    order = next((o for o in s.orders if o.order_id == order_id), None)
    if order and order.status == "pending":
        order.status = "polling"
        await safe_answer(query, "✅ Will poll for OTP")
        await safe_edit_markup(query, None)
        await query.message.reply_text(
            f"✅ `{order.phone}` has been *queued for OTP polling.*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await safe_answer(query, "Already handled.")

async def cb_startpoll(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    s       = sess(chat_id)

    for o in s.orders:
        if o.status == "pending":
            o.status = "polling"

    to_poll = [o for o in s.orders if o.status == "polling"]
    if not to_poll:
        await safe_answer(query, "No numbers left to poll — all cancelled.")
        await safe_edit_markup(query, None)
        return

    await safe_answer(query, "▶ Polling started!")
    await safe_edit_markup(query, None)

    phones = "\n".join(f"• `{o.phone}`" for o in to_poll)
    await query.message.reply_text(
        f"⏳ Polling *{len(to_poll)}* number(s) for OTPs:\n{phones}",
        parse_mode=ParseMode.MARKDOWN,
    )

    if s.poll_task and not s.poll_task.done():
        s.poll_task.cancel()
    s.poll_task = asyncio.create_task(poll_all(chat_id, ctx.application))

# ── Background polling ────────────────────────────────────────────────────────

async def poll_all(chat_id: int, app: Application):
    s          = sess(chat_id)
    start_time = {o.order_id: time.time() for o in s.orders if o.status == "polling"}
    next_check = time.time() + CHECK_EVERY

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        s = sess(chat_id)

        for o in s.orders:
            if o.status != "polling":
                continue

            raw = get_status(s.api_key, o.order_id)

            if raw.startswith("STATUS_OK:"):
                otp = raw.split(":", 1)[1]
                if otp in o.otps:
                    continue
                o.otps.append(otp)
                o.status = "otp_check"
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"🎉 *OTP received!*\n\n"
                        f"📞 Number: `{o.phone}`\n"
                        f"🔑 OTP: `{otp}`\n\n"
                        f"Is another OTP coming on this number?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=otp_keyboard(o.order_id),
                    )
                except Exception:
                    pass

            elif raw in ("STATUS_CANCEL", "NO_ACTIVATION"):
                o.status = "ext_cancel"
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"🚫 Number `{o.phone}` was cancelled externally.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

            elif o.order_id in start_time and time.time() - start_time[o.order_id] >= HARD_TIMEOUT:
                o.status = "timeout"
                try:
                    await app.bot.send_message(
                        chat_id, f"⏰ Number `{o.phone}` timed out (10 min).",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

        if time.time() >= next_check:
            still = [o for o in s.orders if o.status == "polling"]
            if still:
                lines = "\n".join(f"• `{o.phone}`" for o in still)
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"⏰ *2 min passed* — {len(still)} number(s) still waiting:\n{lines}\n\nWhat would you like to do?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=wait_keyboard(still),
                    )
                except Exception:
                    pass
            next_check = time.time() + CHECK_EVERY

        active = [o for o in s.orders if o.status in ("polling", "otp_check")]
        if not active:
            await send_final_summary(chat_id, app)
            break

async def send_final_summary(chat_id: int, app: Application):
    s = sess(chat_id)
    lines = []
    for o in s.orders:
        icon     = {"received": "✅", "cancelled": "❌", "timeout": "⏰", "ext_cancel": "🚫"}.get(o.status, "❓")
        otp_text = " | ".join(o.otps) if o.otps else o.status
        registered_tag = " [REG]" if o.is_registered else ""
        swiggy_tag = f" {o.swiggy_icon}" if o.swiggy_icon else ""
        lines.append(f"{icon} [{o.idx}] `{o.phone}`{registered_tag}{swiggy_tag} — {otp_text}")

    bal      = get_balance(s.api_key)
    bal_text = f"\n\n💰 Remaining balance: ₹{bal}" if bal else ""

    try:
        await app.bot.send_message(
            chat_id,
            "📋 *Final Results*\n\n" + "\n".join(lines) + bal_text +
            "\n\nSend /start to buy more numbers.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

# ── OTP decision callbacks ────────────────────────────────────────────────────

async def cb_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_id  = query.message.chat_id
    order_id = query.data.split(":", 1)[1]
    s        = sess(chat_id)

    order = next((o for o in s.orders if o.order_id == order_id), None)
    if order:
        set_status(s.api_key, order_id, 6)
        order.status = "received"
        await safe_answer(query, "✅ Finalized!")
        await safe_edit_markup(query, None)
        try:
            await query.message.reply_text(
                f"✅ `{order.phone}` finalized. OTP(s): `{'` | `'.join(order.otps)}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    else:
        await safe_answer(query, "Already handled.")

async def cb_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_id  = query.message.chat_id
    order_id = query.data.split(":", 1)[1]
    s        = sess(chat_id)

    order = next((o for o in s.orders if o.order_id == order_id), None)
    if order:
        order.status = "polling"
        await safe_answer(query, "🔄 Keeping alive!")
        await safe_edit_markup(query, None)
        try:
            await query.message.reply_text(
                f"🔄 `{order.phone}` is continuing to poll for the next OTP…",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    else:
        await safe_answer(query, "Already handled.")

# ── Wait-menu callbacks ───────────────────────────────────────────────────────

async def cb_cancel_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    s       = sess(chat_id)

    cancelled = []
    for o in s.orders:
        if o.status == "polling":
            set_status(s.api_key, o.order_id, 8)
            o.status = "cancelled"
            s.cancelled_numbers.add(o.phone)
            cancelled.append(o.phone)

    await safe_answer(query, "❌ Cancelled all")
    await safe_edit_markup(query, None)
    if cancelled:
        phones = ", ".join(f"`{p}`" for p in cancelled)
        try:
            await query.message.reply_text(
                f"❌ Cancelled: {phones}", parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

async def cb_cancel_one(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_id  = query.message.chat_id
    order_id = query.data.split(":", 1)[1]
    s        = sess(chat_id)

    order = next((o for o in s.orders if o.order_id == order_id), None)
    if order and order.status == "polling":
        set_status(s.api_key, order_id, 8)
        order.status = "cancelled"
        s.cancelled_numbers.add(order.phone)
        await safe_answer(query, f"❌ Cancelled {order.phone}")

        still = [o for o in s.orders if o.status == "polling"]
        await safe_edit_markup(query, wait_keyboard(still) if still else None)

        try:
            await query.message.reply_text(
                f"❌ `{order.phone}` has been *cancelled.*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        if still:
            phones = ", ".join(f"`{o.phone}`" for o in still)
            try:
                await query.message.reply_text(
                    f"⏳ {phones} — still polling for OTPs…",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
    else:
        await safe_answer(query, "Already handled.")

async def cb_keep_wait(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    s       = sess(chat_id)

    still = [o for o in s.orders if o.status == "polling"]
    await safe_answer(query, "⏳ Keeping all alive!")
    await safe_edit_markup(query, None)
    if still:
        phones = ", ".join(f"`{o.phone}`" for o in still)
        try:
            await query.message.reply_text(
                f"⏳ {phones} — continuing to poll for OTPs…",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

# ── Global error handler ──────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, (TimedOut, BadRequest)):
        return
    logging.warning("Unhandled exception: %s", ctx.error)

# ── "cancel" text handler ─────────────────────────────────────────────────────

async def msg_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s       = sess(chat_id)

    still = [o for o in s.orders if o.status == "polling"]
    if not still:
        await update.message.reply_text(
            "No numbers are currently being polled.\nSend /start to buy numbers."
        )
        return

    lines = "\n".join(f"• `{o.phone}`" for o in still)
    await update.message.reply_text(
        f"⏰ *Cancel menu* — {len(still)} number(s) currently polling:\n{lines}\n\nWhat would you like to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=wait_keyboard(still),
    )

# ── /cancel ───────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelled. Send /start to begin again.")
    return ConversationHandler.END

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api_key = USER_KEYS.get(user_id) or sess(update.effective_chat.id).api_key
    if not api_key:
        await update.message.reply_text("No API key set. Use /start first.")
        return
    bal = get_balance(api_key)
    await update.message.reply_text(f"💰 Balance: ₹{bal}" if bal else "❌ Could not fetch balance.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            WAIT_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_api_key)],
            WAIT_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_search),
                CallbackQueryHandler(cb_page,         pattern=r"^page:"),
                CallbackQueryHandler(cb_newsearch,    pattern=r"^newsearch$"),
                CallbackQueryHandler(cb_pick_service, pattern=r"^svc:"),
            ],
            WAIT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_count)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_error_handler(error_handler)

    app.add_handler(CallbackQueryHandler(cb_retry_continue, pattern=r"^retry_continue:"))
    app.add_handler(CallbackQueryHandler(cb_retry_stop, pattern=r"^retry_stop:"))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^cancel$"),
        msg_cancel,
    ))

    app.add_handler(CallbackQueryHandler(cb_precancel, pattern=r"^precancel:"))
    app.add_handler(CallbackQueryHandler(cb_keepoll,   pattern=r"^keepoll:"))
    app.add_handler(CallbackQueryHandler(cb_startpoll, pattern=r"^startpoll$"))
    app.add_handler(CallbackQueryHandler(cb_done,      pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(cb_more,      pattern=r"^more:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_all, pattern=r"^cancel_all$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_one, pattern=r"^cancel_one:"))
    app.add_handler(CallbackQueryHandler(cb_keep_wait,  pattern=r"^keep_wait$"))

    print("🤖 Bot started — polling for updates…")

    # ── Railway: Use polling (webhook not required) ──
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
