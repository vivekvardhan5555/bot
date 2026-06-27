"""
OTPDoctor Telegram Bot
─────────────────────
Flow: /start → (API key once per user) → search service → pick → how many
      → buy in parallel → [Swiggy only] check trickhack.in → cancel/keep
      → poll → OTP decision
"""

import os
import re
import asyncio
import json
import logging
import time
import requests as req
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL      = "https://otpdoctor.in/stubs/handler_api.php"
CHECK_URL     = "https://trickhack.in/Swiggy/"
PAGE_SIZE     = 10
POLL_INTERVAL = 10
HARD_TIMEOUT  = 600
CHECK_EVERY   = 120
RETRY_PROMPT  = 30        # ask user every N NO_NUMBERS attempts

# ── Conversation states ───────────────────────────────────────────────────────

WAIT_API_KEY, WAIT_SEARCH, WAIT_COUNT = range(3)

# ── Persistent stores (survive across /start) ─────────────────────────────────

USER_KEYS:      dict[int, str]       = {}   # user_id → api_key
CHECKED_PHONES: dict[int, set]       = {}   # user_id → set of already-seen phones

# ── Per-slot buy-pause events (chat_id, slot_idx) ────────────────────────────

SLOT_STOP: dict[tuple, asyncio.Event] = {}   # (chat_id, idx) → stop event
SLOT_CONT: dict[tuple, asyncio.Event] = {}   # (chat_id, idx) → continue event

# ── OTPDoctor API helpers ─────────────────────────────────────────────────────

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
        r = req.get(
            BASE_URL,
            params={"action": "getServices", "api_key": api_key, "country": "in"},
            timeout=30,
        )
        text = r.text.strip()
        if text.startswith("{"):
            data = json.loads(text)
            return sorted(
                [
                    {
                        "id": sid,
                        "name": info.get("service_name", "").strip(),
                        "price": info.get("service_price", "?"),
                        "server": info.get("server_name", ""),
                    }
                    for sid, info in data.items()
                ],
                key=lambda x: x["name"].lower(),
            ), None
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

# ── Swiggy web check ──────────────────────────────────────────────────────────

def check_swiggy(phone: str) -> tuple[str, str, bool | None]:
    """
    Returns (icon, text, is_registered):
      is_registered=True  → 'success' class → number IS registered on Swiggy → auto-cancel
      is_registered=False → 'error'   class → number is NOT registered → keep for OTP
      is_registered=None  → unknown / no result yet → caller should retry
    """
    try:
        r = req.post(CHECK_URL, data={"mobile": phone}, timeout=30)
        m = re.search(
            r'<div[^>]*class="result\s+(success|error|info)"[^>]*>(.*?)</div>',
            r.text, re.DOTALL,
        )
        if m:
            cls  = m.group(1)
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if cls == "success":
                return "✅", text, True    # registered  → auto-cancel
            elif cls == "error":
                return "❌", text, False   # NOT registered → keep
            else:                          # info / anything else → unknown
                return "ℹ️", text, None
        return "❓", "No result yet — retrying…", None
    except Exception as e:
        return "⚠️", f"Check failed: {e}", None

# ── Session / Order model ─────────────────────────────────────────────────────

@dataclass
class Order:
    idx:      int
    order_id: str
    phone:    str
    otps:     list = field(default_factory=list)
    # pending | polling | otp_check | received | cancelled | timeout | ext_cancel | stopped
    status:   str  = "pending"

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

# ── Keyboards ─────────────────────────────────────────────────────────────────

def service_keyboard(hits: list, page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = hits[start : start + PAGE_SIZE]
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

def pre_poll_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel",      callback_data=f"precancel:{order_id}"),
        InlineKeyboardButton("✅ Keep & poll", callback_data=f"keepoll:{order_id}"),
    ]])

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

def buy_again_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Buy More Numbers", callback_data="buy_again"),
    ]])

def retry_keyboard(chat_id: int, idx: int) -> InlineKeyboardMarkup:
    """Shown after every 30 NO_NUMBERS retries for a slot."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Continue",    callback_data=f"buycon:{chat_id}:{idx}"),
        InlineKeyboardButton("⏹ Stop this",  callback_data=f"buystop:{chat_id}:{idx}"),
    ]])

# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in USER_KEYS:
        api_key = USER_KEYS[user_id]
        SESSIONS[chat_id] = Session(api_key=api_key)
        s   = sess(chat_id)
        msg = await update.message.reply_text("⏳ Loading India services…")
        services, err = fetch_services(api_key)
        if err or not services:
            await msg.edit_text(
                f"❌ Could not load services: {err or 'empty'}. Try /start again."
            )
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

# ── "Buy Again" callback — re-enters the search flow without asking for key ───

async def cb_buy_again(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query   = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    await safe_answer(query)

    if user_id not in USER_KEYS:
        await query.message.reply_text(
            "No API key on record. Send /start to set up."
        )
        return ConversationHandler.END

    api_key = USER_KEYS[user_id]
    SESSIONS[chat_id] = Session(api_key=api_key)
    s   = sess(chat_id)
    msg = await query.message.reply_text("⏳ Loading India services…")
    services, err = fetch_services(api_key)
    if err or not services:
        await msg.edit_text(
            f"❌ Could not load services: {err or 'empty'}. Try /start again."
        )
        return ConversationHandler.END
    s.services = services
    bal = get_balance(api_key)
    await msg.edit_text(
        f"✅ Balance: ₹{bal}  |  {len(services)} services available\n\n"
        "🔍 *Search a service* — type a name (e.g. `swiggy`, `telegram`, `zomato`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_SEARCH

# ── Step 1 — API key (only needed once per user) ──────────────────────────────

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
        await msg.edit_text(
            f"❌ Could not load services: {err or 'empty'}. Try /start again."
        )
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
            parse_mode=ParseMode.MARKDOWN,
        )
        return WAIT_SEARCH

    s.search_hits = hits
    s.search_page = 0
    text = (
        f"Found *{len(hits)}* service(s) for `{query}` — pick one:"
        if query
        else f"All *{len(hits)}* services — pick one:"
    )
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

# ── Step 3 — count → parallel purchase → site check ──────────────────────────

async def _buy_one(
    idx: int,
    api_key: str,
    service_id: str,
    status_map: dict,
    chat_id: int,
    app: Application,
) -> Order:
    """Retry until a number is obtained.
    Every RETRY_PROMPT consecutive NO_NUMBERS, ask the user to continue or stop.
    """
    attempt      = 0
    no_num_count = 0

    while True:
        attempt      += 1
        no_num_count += 1
        status_map[idx] = f"⟳ [{idx}] attempt {attempt}…"
        order_id, phone, err = get_number(api_key, service_id)

        if order_id:
            status_map[idx] = f"✅ [{idx}] `{phone}`"
            return Order(idx=idx, order_id=order_id, phone=phone)

        elif err == "NO_NUMBERS":
            status_map[idx] = f"⟳ [{idx}] no numbers — attempt {attempt}, retrying…"

            # ── Every RETRY_PROMPT attempts: ask the user ─────────────────────
            if no_num_count % RETRY_PROMPT == 0:
                key = (chat_id, idx)
                stop_evt = asyncio.Event()
                cont_evt = asyncio.Event()
                SLOT_STOP[key] = stop_evt
                SLOT_CONT[key] = cont_evt

                try:
                    await app.bot.send_message(
                        chat_id,
                        f"⚠️ *Slot [{idx}]* has tried *{attempt} times* with no numbers "
                        f"available yet.\n\nContinue retrying or stop this slot?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=retry_keyboard(chat_id, idx),
                    )
                except Exception:
                    pass

                # Wait (polling every second) until user taps a button
                while not stop_evt.is_set() and not cont_evt.is_set():
                    await asyncio.sleep(1)

                # Clean up
                SLOT_STOP.pop(key, None)
                SLOT_CONT.pop(key, None)

                if stop_evt.is_set():
                    status_map[idx] = f"⏹ [{idx}] stopped by user after {attempt} attempts"
                    o = Order(idx=idx, order_id="", phone="")
                    o.status = "stopped"
                    return o
                # else: continue — reset streak counter so next prompt is 30 more
                no_num_count = 0

            await asyncio.sleep(2)

        else:
            # Hard error (NO_BALANCE, BAD_SERVICE, etc.)
            status_map[idx] = f"❌ [{idx}] failed: {err}"
            o = Order(idx=idx, order_id="", phone="")
            o.status = "failed"
            o._err = err   # type: ignore[attr-defined]
            return o

# ── Buy-pause callbacks (Continue / Stop buttons) ─────────────────────────────

async def cb_buy_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")          # buycon:<chat_id>:<idx>
    key   = (int(parts[1]), int(parts[2]))
    if key in SLOT_CONT:
        SLOT_CONT[key].set()
    await safe_answer(query, "▶ Continuing…")
    await safe_edit_markup(query, None)

async def cb_buy_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")          # buystop:<chat_id>:<idx>
    key   = (int(parts[1]), int(parts[2]))
    if key in SLOT_STOP:
        SLOT_STOP[key].set()
    await safe_answer(query, "⏹ Stopping…")
    await safe_edit_markup(query, None)

# ── Live status updater ───────────────────────────────────────────────────────

async def _delayed_cancel(api_key: str, order: Order, chat_id: int, app: Application):
    """Wait 2 minutes (site policy) then auto-cancel a registered number."""
    await asyncio.sleep(120)
    if order.status not in ("cancelled", "received", "ext_cancel"):
        set_status(api_key, order.order_id, 8)
        order.status = "cancelled"
        try:
            await app.bot.send_message(
                chat_id,
                f"🚫 `{order.phone}` auto-cancelled after 2 min (already registered on Swiggy).",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

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

# ── recv_count — thin launcher, returns END immediately to free the conv lock ──

async def recv_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
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

    # Launch as background task — this releases the ConversationHandler lock
    # immediately so that Continue/Stop button callbacks can be processed.
    if s.poll_task and not s.poll_task.done():
        s.poll_task.cancel()
    s.poll_task = asyncio.create_task(
        _buy_all_task(chat_id, user_id, ctx.application, count, status_msg)
    )

    return ConversationHandler.END


# ── _buy_all_task — runs in background, uses app.bot.send_message ─────────────

async def _buy_all_task(
    chat_id:    int,
    user_id:    int,
    app:        Application,
    count:      int,
    status_msg,
):
    s      = sess(chat_id)
    header = f"🛒 Purchasing *{count}* number(s) for *{s.service_name}*…"

    status_map: dict[int, str] = {i: f"⏳ [{i}] waiting…" for i in range(1, count + 1)}
    done_evt = asyncio.Event()

    updater_task = asyncio.create_task(
        _live_status_updater(status_msg, header, status_map, count, done_evt)
    )

    results: list[Order] = await asyncio.gather(
        *[
            _buy_one(i, s.api_key, s.service_id, status_map, chat_id, app)
            for i in range(1, count + 1)
        ]
    )

    done_evt.set()
    await asyncio.sleep(1.2)
    updater_task.cancel()

    # ── Build result summary ───────────────────────────────────────────────────
    err_map = {
        "NO_BALANCE":  "Insufficient balance",
        "BAD_SERVICE": "Invalid service ID",
        "TRY_AGAIN":   "Temporary error",
    }
    seen  = CHECKED_PHONES.setdefault(user_id, set())
    lines = []
    for o in results:
        if o.status == "failed":
            err = getattr(o, "_err", "Unknown error")
            lines.append(f"❌ [{o.idx}] Failed: {err_map.get(err, err)}")
        elif o.status == "stopped":
            lines.append(f"⏹ [{o.idx}] Stopped by user")
        else:
            if o.phone in seen:
                lines.append(f"⚠️ [{o.idx}] `{o.phone}` *(already checked before — duplicate)*")
            else:
                lines.append(f"✅ [{o.idx}] `{o.phone}`")
            s.orders.append(o)

    try:
        await status_msg.edit_text(
            f"🛒 *{s.service_name}* — {len(s.orders)}/{count} bought:\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        try:
            await app.bot.send_message(
                chat_id,
                f"🛒 *{s.service_name}* — {len(s.orders)}/{count} bought:\n\n" + "\n".join(lines),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    if not s.orders:
        try:
            await app.bot.send_message(
                chat_id,
                "❌ Could not buy any numbers.",
                reply_markup=buy_again_keyboard(),
            )
        except Exception:
            pass
        return

    # ── Duplicate-phone warnings ───────────────────────────────────────────────
    for o in s.orders:
        if o.phone in seen:
            try:
                await app.bot.send_message(
                    chat_id,
                    f"⚠️ `{o.phone}` was *already checked in a previous session*. "
                    f"This number may already be registered.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass

    # Record all bought phones as seen from this point on
    for o in s.orders:
        seen.add(o.phone)

    # ── Swiggy-only web check ─────────────────────────────────────────────────
    is_swiggy = "swiggy" in s.service_name.lower()

    if is_swiggy:
        try:
            await app.bot.send_message(chat_id, "🔍 Checking each number on Swiggy…")
        except Exception:
            pass

        for o in s.orders:
            # Retry until a definitive answer arrives
            icon, result_text, is_registered = "❓", "No result yet.", None
            for attempt in range(1, 11):
                icon, result_text, is_registered = check_swiggy(o.phone)
                if is_registered is not None:
                    break
                try:
                    if attempt == 1:
                        await app.bot.send_message(
                            chat_id,
                            f"📞 `{o.phone}`\n🌐 {icon} {result_text} — retrying…",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                except Exception:
                    pass
                await asyncio.sleep(4)

            if is_registered is True:
                asyncio.create_task(_delayed_cancel(s.api_key, o, chat_id, app))
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"📞 `{o.phone}`\n🌐 Swiggy: {icon} {result_text}\n\n"
                        f"⚠️ *Already registered* — will auto-cancel in 2 minutes.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

            elif is_registered is False:
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"📞 `{o.phone}`\n🌐 Swiggy: {icon} {result_text}\n\n"
                        f"What do you want to do with this number?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=pre_poll_keyboard(o.order_id),
                    )
                except Exception:
                    pass

            else:
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"📞 `{o.phone}`\n🌐 Swiggy: ❓ Could not determine status.\n\n"
                        f"What do you want to do with this number?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=pre_poll_keyboard(o.order_id),
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.3)

        remaining = [o for o in s.orders if o.status == "pending"]
        if not remaining:
            try:
                await app.bot.send_message(
                    chat_id,
                    "⚡ All numbers already registered — auto-cancel scheduled in 2 min.",
                    reply_markup=buy_again_keyboard(),
                )
            except Exception:
                pass
            return

        try:
            await app.bot.send_message(
                chat_id,
                "👆 Review numbers above — cancel any you don't need.\n\n"
                "When ready, tap below to start polling for OTPs:",
                reply_markup=start_poll_keyboard(),
            )
        except Exception:
            pass

    else:
        for o in s.orders:
            o.status = "polling"
        phones = "\n".join(f"📞 `{o.phone}`" for o in s.orders)
        try:
            await app.bot.send_message(
                chat_id,
                f"*Numbers ready:*\n{phones}\n\n💰 ₹{s.listed_price} each\n\n⏳ Polling for OTPs…",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        if s.poll_task and not s.poll_task.done():
            s.poll_task.cancel()
        s.poll_task = asyncio.create_task(poll_all(chat_id, app))

# ── Pre-poll buttons (Swiggy check results) ───────────────────────────────────

async def cb_precancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    chat_id  = query.message.chat_id
    order_id = query.data.split(":", 1)[1]
    s        = sess(chat_id)

    order = next((o for o in s.orders if o.order_id == order_id), None)
    if order and order.status == "pending":
        set_status(s.api_key, order_id, 8)
        order.status = "cancelled"
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
                        chat_id,
                        f"⏰ Number `{o.phone}` timed out (10 min).",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

        # 2-min nudge
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
    s     = sess(chat_id)
    lines = []
    icons = {
        "received":   "✅",
        "cancelled":  "❌",
        "timeout":    "⏰",
        "ext_cancel": "🚫",
        "stopped":    "⏹",
    }
    for o in s.orders:
        icon     = icons.get(o.status, "❓")
        otp_text = " | ".join(o.otps) if o.otps else o.status
        lines.append(f"{icon} [{o.idx}] `{o.phone}` — {otp_text}")

    bal      = get_balance(s.api_key)
    bal_text = f"\n\n💰 Remaining balance: ₹{bal}" if bal else ""

    try:
        await app.bot.send_message(
            chat_id,
            "📋 *Final Results*\n\n" + "\n".join(lines) + bal_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=buy_again_keyboard(),
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

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, (TimedOut, BadRequest)):
        return
    logging.warning("Unhandled exception: %s", ctx.error)

# ── "cancel" text handler (works any time, including during polling) ──────────

async def msg_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s       = sess(chat_id)

    still = [o for o in s.orders if o.status == "polling"]
    if not still:
        await update.message.reply_text(
            "No numbers are currently being polled.",
            reply_markup=buy_again_keyboard(),
        )
        return

    lines = "\n".join(f"• `{o.phone}`" for o in still)
    await update.message.reply_text(
        f"⏰ *Cancel menu* — {len(still)} number(s) currently polling:\n{lines}\n\nWhat would you like to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=wait_keyboard(still),
    )

# ── /cancel (inside conversation flow) ───────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Operation stopped.",
        reply_markup=buy_again_keyboard(),
    )
    return ConversationHandler.END

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api_key = USER_KEYS.get(user_id) or sess(update.effective_chat.id).api_key
    if not api_key:
        await update.message.reply_text("No API key set. Use /start first.")
        return
    bal = get_balance(api_key)
    await update.message.reply_text(
        f"💰 Balance: ₹{bal}" if bal else "❌ Could not fetch balance."
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cb_buy_again, pattern=r"^buy_again$"),
        ],
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

    # Plain-text "cancel" during polling
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^cancel$"),
        msg_cancel,
    ))

    # Buy-pause (30-attempt prompt) callbacks
    app.add_handler(CallbackQueryHandler(cb_buy_continue, pattern=r"^buycon:"))
    app.add_handler(CallbackQueryHandler(cb_buy_stop,     pattern=r"^buystop:"))

    # Swiggy pre-poll & start-poll
    app.add_handler(CallbackQueryHandler(cb_precancel,  pattern=r"^precancel:"))
    app.add_handler(CallbackQueryHandler(cb_keepoll,    pattern=r"^keepoll:"))
    app.add_handler(CallbackQueryHandler(cb_startpoll,  pattern=r"^startpoll$"))

    # OTP decision
    app.add_handler(CallbackQueryHandler(cb_done,       pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(cb_more,       pattern=r"^more:"))

    # Wait-menu
    app.add_handler(CallbackQueryHandler(cb_cancel_all, pattern=r"^cancel_all$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_one, pattern=r"^cancel_one:"))
    app.add_handler(CallbackQueryHandler(cb_keep_wait,  pattern=r"^keep_wait$"))

    print("🤖 Bot started — polling for updates…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
