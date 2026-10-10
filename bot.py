# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║        🇪🇹  FAYDA ID BOT  ✦  ESIGNET EDITION  ✦  RAILWAY READY      ║
║                                                                        ║
║  ✦ Powered by official Fayda eSignet API (auth.fayda.et)             ║
║  ✦ Smart OTP Flow  •  4 Templates  •  Color + Grayscale              ║
║  ✦ Wallet System  •  Referrals  •  A4 Converter                      ║
║  ✦ Admin Dashboard  •  Broadcast  •  Payment Management              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# ── venv bootstrap ───────────────────────────────────────────────────────────
import os as _os, sys as _sys
_venv_lib = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".venv", "lib")
if _os.path.isdir(_venv_lib):
    for _d in _os.listdir(_venv_lib):
        _sp = _os.path.join(_venv_lib, _d, "site-packages")
        if _os.path.isdir(_sp) and _sp not in _sys.path:
            _sys.path.insert(0, _sp)
            break
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageDraw

# ── QR decoder (OpenCV only: no native zbar library needed) ──────────────────
import cv2
import numpy as _np


class _QRResult:
    """Minimal stand-in for pyzbar's result object (only .data is used)."""
    def __init__(self, text: str):
        self.data = text.encode("utf-8")
        self.type = "QRCODE"


def _decode_qr_bgr(bgr) -> str | None:
    """OpenCV decoders in order of increasing power; the first hit wins."""
    det  = cv2.QRCodeDetector()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    for frame in (bgr, gray):
        data, _, _ = det.detectAndDecode(frame)
        if data:
            return data
    try:
        data, _, _ = cv2.QRCodeDetectorAruco().detectAndDecode(bgr)
        if data:
            return data
    except (AttributeError, cv2.error):
        pass
    try:
        texts, _ = cv2.wechat_qrcode_WeChatQRCode().detectAndDecode(bgr)
        if texts and texts[0]:
            return texts[0]
    except (AttributeError, cv2.error):
        pass
    return None


def zbar_decode(pil_image):
    """
    Drop-in replacement for pyzbar.decode(). A downscaled frame is tried first
    (detection is faster, and it works for most photos); full resolution is the
    fallback when that misses.
    """
    try:
        bgr = _np.asarray(pil_image.convert("RGB"))[:, :, ::-1].copy()
        h, w = bgr.shape[:2]
        longest = max(h, w)
        if longest > 1400:
            scale = 1400.0 / longest
            small = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
            text = _decode_qr_bgr(small)
            if text:
                return [_QRResult(text)]
        text = _decode_qr_bgr(bgr)
        return [_QRResult(text)] if text else []
    except Exception:
        return []


from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import (
    TEMPLATES,
    get_template_sample,
    render_cards,
    safe_b64decode,
)
from esignet import EsignetClient, EsignetError

# Blocking work (SQLite, eSignet HTTP, rendering) runs in this pool. The default
# executor only has min(32, cpus + 4) threads, too few once many users are active.
_IO_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="io")

_WHITELIST: set[int] = set()              # mirror of the whitelist table (/adduser, /removeuser keep it in sync)
_BOT_USERNAME = ""                        # set at startup, so invite links need no API call
_SAMPLE_BYTES:    dict[int, bytes] = {}   # template id -> JPEG sample, rendered once
_SAMPLE_FILE_IDS: dict[int, str]   = {}   # template id -> Telegram file_id, uploaded once

# ═══════════════════════════════════════════════════════════════════════
#  ⚙️  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "dhtechs_admin")
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0"))
PAYMENT_PHONE  = os.getenv("PAYMENT_PHONE", "0011111111")

PRICE_OTP      = int(os.getenv("PRICE_OTP", "10"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "10"))
FREE_TRIALS    = int(os.getenv("FREE_TRIALS", "1"))

DB_PATH        = os.getenv("DB_PATH", "fayda_bot.db")
OUTPUT_FOLDER  = "output"
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# A4 print settings
A4_WIDTH        = 2480
A4_HEIGHT       = 3508
CARD_WIDTH      = 2191
CARD_HEIGHT     = 667
MAX_PER_PAGE    = 5
DPI             = 300
CROP_LENGTH     = 40
CROP_WIDTH_PX   = 3
CROP_OFFSET     = 15
CENTER_MARK_LEN = 80

# ═══════════════════════════════════════════════════════════════════════
#  📝  LOGGING
# ═══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fayda_bot")

# ═══════════════════════════════════════════════════════════════════════
#  🔢  CONVERSATION STATES
# ═══════════════════════════════════════════════════════════════════════
(
    WAIT_FAN,           # waiting for 16-digit Fayda Application Number
    WAIT_OTP,           # waiting for OTP code
    WAIT_SCREENSHOT,    # top-up screenshot
    WAIT_EDIT_CREDITS,  # admin edit wallet
    WAIT_BROADCAST,     # admin broadcast
    WAIT_A4_IMAGES,     # A4 converter images
    WAIT_A4_FLIP,       # A4 flip confirmation
) = range(7)

# Extra state (QR-scan entry path — shares WAIT_OTP for OTP input)
WAIT_QR_SCAN = 7  # only used as entry-point marker; OTP step reuses WAIT_OTP


# ═══════════════════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ═══════════════════════════════════════════════════════════════════════
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id             INTEGER PRIMARY KEY,
            username            TEXT    DEFAULT '',
            full_name           TEXT    DEFAULT '',
            wallet              INTEGER DEFAULT 0,
            trials_used         INTEGER DEFAULT 0,
            template_id         INTEGER DEFAULT 4,
            referred_by         INTEGER DEFAULT NULL,
            referral_bonus_paid INTEGER DEFAULT 0,
            joined_at           TEXT    DEFAULT (datetime('now')),
            last_seen           TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            username     TEXT    DEFAULT '',
            full_name    TEXT    DEFAULT '',
            amount       INTEGER NOT NULL,
            screenshot_id TEXT,
            status       TEXT    DEFAULT 'pending',
            created_at   TEXT    DEFAULT (datetime('now')),
            reviewed_at  TEXT,
            reviewed_by  TEXT,
            note         TEXT    DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS generations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            username     TEXT    DEFAULT '',
            template_id  INTEGER DEFAULT 4,
            generated_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS global_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO global_settings (key, value) VALUES ('admin_chat_id', '0');
        CREATE TABLE IF NOT EXISTS whitelist (
            chat_id    INTEGER PRIMARY KEY,
            added_at   TEXT    DEFAULT (datetime('now')),
            added_by   TEXT    DEFAULT ''
        );
    """)
    # Safe migrations
    for migration in [
        "ALTER TABLE users ADD COLUMN template_id         INTEGER DEFAULT 4",
        "ALTER TABLE users ADD COLUMN referred_by         INTEGER DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN referral_bonus_paid INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass
    conn.commit()
    conn.close()
    log.info("✅ Database initialised.")


# ── DB helpers ────────────────────────────────────────────────────────

def _db_upsert_user(chat_id, username, full_name, referred_by=None):
    conn = _get_conn()
    existing = conn.execute(
        "SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET username=?, full_name=?, last_seen=datetime('now') WHERE chat_id=?",
            (username or "", full_name or "", chat_id),
        )
    else:
        conn.execute(
            "INSERT INTO users (chat_id, username, full_name, referred_by) VALUES (?,?,?,?)",
            (chat_id, username or "", full_name or "",
             referred_by if (referred_by and referred_by != chat_id) else None),
        )
    conn.commit()
    conn.close()


def _db_get_user(chat_id) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _db_set_template(chat_id, template_id):
    conn = _get_conn()
    conn.execute("UPDATE users SET template_id=? WHERE chat_id=?", (template_id, chat_id))
    conn.commit()
    conn.close()


def _db_can_generate(chat_id, price=None) -> bool:
    price = price or PRICE_OTP
    u = _db_get_user(chat_id)
    return bool(u and (u["trials_used"] < FREE_TRIALS or u["wallet"] >= price))


def _db_deduct(chat_id, price=None) -> tuple[str, int]:
    price = price or PRICE_OTP
    conn  = _get_conn()
    u     = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if not u:
        conn.close()
        return "none", 0
    tid = u["template_id"] if "template_id" in u.keys() else 3
    if u["trials_used"] < FREE_TRIALS:
        conn.execute("UPDATE users SET trials_used=trials_used+1 WHERE chat_id=?", (chat_id,))
        conn.execute(
            "INSERT INTO generations (chat_id, username, template_id) VALUES (?,?,?)",
            (chat_id, u["username"], tid),
        )
        conn.commit()
        remaining = FREE_TRIALS - u["trials_used"] - 1
        conn.close()
        return "trial", remaining
    new_w = max(0, u["wallet"] - price)
    conn.execute("UPDATE users SET wallet=? WHERE chat_id=?", (new_w, chat_id))
    conn.execute(
        "INSERT INTO generations (chat_id, username, template_id) VALUES (?,?,?)",
        (chat_id, u["username"], tid),
    )
    conn.commit()
    conn.close()
    return "wallet", new_w


def _db_add_payment(chat_id, username, full_name, amount, screenshot_id) -> int:
    conn = _get_conn()
    cur  = conn.execute(
        "INSERT INTO payments (chat_id, username, full_name, amount, screenshot_id) VALUES (?,?,?,?,?)",
        (chat_id, username or "", full_name or "", amount, screenshot_id),
    )
    pid  = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def _db_get_payment(pid) -> dict | None:
    conn = _get_conn()
    row  = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _db_update_payment(pid, status, reviewed_by, note=""):
    conn = _get_conn()
    conn.execute(
        "UPDATE payments SET status=?, reviewed_at=datetime('now'), reviewed_by=?, note=? WHERE id=?",
        (status, reviewed_by, note, pid),
    )
    conn.commit()
    conn.close()


def _db_add_wallet(chat_id, amount):
    conn = _get_conn()
    conn.execute("UPDATE users SET wallet=wallet+? WHERE chat_id=?", (amount, chat_id))
    conn.commit()
    conn.close()


def _db_set_wallet(chat_id, amount):
    conn = _get_conn()
    conn.execute("UPDATE users SET wallet=? WHERE chat_id=?", (amount, chat_id))
    conn.commit()
    conn.close()


def _db_pending_payments() -> list[dict]:
    conn  = _get_conn()
    rows  = conn.execute(
        "SELECT * FROM payments WHERE status='pending' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_has_pending(chat_id) -> bool:
    conn = _get_conn()
    row  = conn.execute(
        "SELECT id FROM payments WHERE chat_id=? AND status='pending'", (chat_id,)
    ).fetchone()
    conn.close()
    return row is not None


def _db_stats() -> tuple:
    conn = _get_conn()
    tu   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    tg   = conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
    pp   = conn.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    tr   = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved'"
    ).fetchone()[0]
    conn.close()
    return tu, tg, pp, tr


def _db_all_users(limit=30) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT chat_id, username, full_name, wallet, trials_used, template_id "
        "FROM users ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _db_get_setting(key) -> str:
    conn = _get_conn()
    row  = conn.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""


def _db_set_setting(key, value):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO global_settings (key,value,updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def _db_get_all_chat_ids() -> list[int]:
    conn = _get_conn()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]


def _db_get_referral_info(chat_id) -> tuple:
    conn    = _get_conn()
    row     = conn.execute(
        "SELECT referred_by, referral_bonus_paid FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()
    invited = conn.execute(
        "SELECT COUNT(*) FROM users WHERE referred_by=?", (chat_id,)
    ).fetchone()[0]
    earned  = conn.execute(
        "SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_bonus_paid=1", (chat_id,)
    ).fetchone()[0]
    conn.close()
    if not row:
        return None, 0, invited, earned * REFERRAL_BONUS
    return row["referred_by"], row["referral_bonus_paid"], invited, earned * REFERRAL_BONUS


def _db_reward_referrer_if_eligible(chat_id) -> int | None:
    conn = _get_conn()
    row  = conn.execute(
        "SELECT referred_by, referral_bonus_paid FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if not row or not row["referred_by"] or row["referral_bonus_paid"]:
        conn.close()
        return None
    referrer_id = row["referred_by"]
    conn.execute("UPDATE users SET wallet=wallet+? WHERE chat_id=?", (REFERRAL_BONUS, referrer_id))
    conn.execute("UPDATE users SET referral_bonus_paid=1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    return referrer_id


# ── Whitelist helpers ─────────────────────────────────────────────────

def _db_is_whitelisted(chat_id: int) -> bool:
    conn = _get_conn()
    row  = conn.execute("SELECT chat_id FROM whitelist WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def _db_whitelist_add(chat_id: int, added_by: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO whitelist (chat_id, added_by) VALUES (?,?)",
        (chat_id, added_by),
    )
    conn.commit()
    conn.close()


def _db_whitelist_remove(chat_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM whitelist WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def _db_whitelist_all() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT chat_id, added_at, added_by FROM whitelist ORDER BY added_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════
#  👑  ADMIN STATE
# ═══════════════════════════════════════════════════════════════════════
_admin_chat_id: int = 0


def get_admin_chat_id() -> int:
    global _admin_chat_id
    if _admin_chat_id:
        return _admin_chat_id
    saved = _db_get_setting("admin_chat_id")
    if saved and saved != "0":
        _admin_chat_id = int(saved)
    elif ADMIN_CHAT_ID:
        _admin_chat_id = ADMIN_CHAT_ID
    return _admin_chat_id


def set_admin_chat_id(cid: int):
    global _admin_chat_id
    _admin_chat_id = cid
    _db_set_setting("admin_chat_id", cid)


def is_admin(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if u.username and u.username.lower() == ADMIN_USERNAME.lower():
        return True
    aid = get_admin_chat_id()
    return aid != 0 and u.id == aid


async def _check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the user is admin or whitelisted. Sends a deny message otherwise."""
    if is_admin(update):
        return True
    u    = update.effective_user
    loop = asyncio.get_running_loop()
    ok   = u.id in _WHITELIST
    if not ok:
        target = update.message or (
            update.callback_query.message if update.callback_query else None
        )
        if target:
            await target.reply_text(
                "🔒 <b>Private Bot</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "This bot is <b>private</b> and accessible\n"
                "by invitation only.\n\n"
                "📩 Contact the admin to request access.",
                parse_mode=ParseMode.HTML,
            )
    return ok


# ═══════════════════════════════════════════════════════════════════════
#  🖨️  A4 CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════
def _draw_crop_marks(draw, x, y):
    c = "black"
    w = CROP_WIDTH_PX
    o = CROP_OFFSET
    L = CROP_LENGTH
    cw, ch = CARD_WIDTH, CARD_HEIGHT
    # Top-left
    draw.line((x - o, y - L, x - o, y - o), fill=c, width=w)
    draw.line((x - L, y - o, x - o, y - o), fill=c, width=w)
    # Top-right
    draw.line((x + cw + o, y - L, x + cw + o, y - o), fill=c, width=w)
    draw.line((x + cw + o, y - o, x + cw + L, y - o), fill=c, width=w)
    # Bottom-left
    draw.line((x - o, y + ch + o, x - o, y + ch + L), fill=c, width=w)
    draw.line((x - L, y + ch + o, x - o, y + ch + o), fill=c, width=w)
    # Bottom-right
    draw.line((x + cw + o, y + ch + o, x + cw + o, y + ch + L), fill=c, width=w)
    draw.line((x + cw + o, y + ch + o, x + cw + L, y + ch + o), fill=c, width=w)


def _draw_center_marks(draw):
    cx, cy = A4_WIDTH // 2, A4_HEIGHT // 2
    w = CROP_WIDTH_PX
    L = CENTER_MARK_LEN
    draw.line((cx, 0, cx, L),             fill="black", width=w)
    draw.line((cx, A4_HEIGHT - L, cx, A4_HEIGHT), fill="black", width=w)
    draw.line((0, cy, L, cy),             fill="black", width=w)
    draw.line((A4_WIDTH - L, cy, A4_WIDTH, cy), fill="black", width=w)


def generate_a4_pages(images, mirror=False) -> tuple[list, bytes | None, list[bytes]]:
    """
    Lay cards out on A4 pages (5 per page, with crop marks). Returns the page images,
    the print-ready PDF as bytes, and one JPEG preview per page. Nothing touches disk.
    """
    processed = []
    for img in images:
        img = img.convert("RGB")
        if img.size == (CARD_WIDTH, CARD_HEIGHT):
            if mirror:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            processed.append(img)
    if not processed:
        return [], None, []

    pages        = []
    left_margin  = (A4_WIDTH - CARD_WIDTH) // 2
    remaining_sp = A4_HEIGHT - MAX_PER_PAGE * CARD_HEIGHT
    vertical_gap = remaining_sp // (MAX_PER_PAGE + 1)
    page         = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
    draw         = ImageDraw.Draw(page)
    count        = 0

    for card in processed:
        if count == MAX_PER_PAGE:
            _draw_center_marks(draw)
            pages.append(page)
            page  = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
            draw  = ImageDraw.Draw(page)
            count = 0
        x = left_margin
        y = vertical_gap + count * (CARD_HEIGHT + vertical_gap)
        page.paste(card, (x, y))
        _draw_crop_marks(draw, x, y)
        count += 1

    if count > 0:
        _draw_center_marks(draw)
        pages.append(page)

    pdf = io.BytesIO()
    pages[0].save(pdf, format="PDF", save_all=True, append_images=pages[1:], resolution=DPI)

    previews = []
    for p in pages:
        buf = io.BytesIO()
        p.save(buf, format="JPEG", quality=92)
        previews.append(buf.getvalue())
    return pages, pdf.getvalue(), previews


# ═══════════════════════════════════════════════════════════════════════
#  ⌨️  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════
def user_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🪪 Send OTP"),        KeyboardButton("🎨 Choose Template")],
            [KeyboardButton("🖨️ A4 Converter"),    KeyboardButton("💳 My Wallet")],
            [KeyboardButton("💰 Top Up"),           KeyboardButton("🎁 Invite & Earn")],
        ],
        resize_keyboard=True,
    )


def admin_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🪪 Send OTP"),         KeyboardButton("🎨 Choose Template")],
            [KeyboardButton("🖨️ A4 Converter"),     KeyboardButton("💳 My Wallet")],
            [KeyboardButton("📋 Pending Payments"), KeyboardButton("📈 Stats")],
            [KeyboardButton("👥 Users"),             KeyboardButton("✏️ Edit Wallet")],
            [KeyboardButton("📣 Broadcast"),         KeyboardButton("💰 Top Up")],
            [KeyboardButton("🔒 Whitelist")],
        ],
        resize_keyboard=True,
    )


def cancel_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Cancel")]],
        resize_keyboard=True,
    )


def cancel_inline_kb():
    """Inline cancel button — use with edit_text() instead of cancel_kb().
    Telegram only allows InlineKeyboardMarkup on edited messages."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]]
    )


def a4_collect_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("✅ Done — Generate A4")],
            [KeyboardButton("❌ Cancel")],
        ],
        resize_keyboard=True,
    )


def template_inline_keyboard(current_tid: int) -> InlineKeyboardMarkup:
    rows  = []
    items = list(TEMPLATES.items())
    for i in range(0, len(items), 2):
        row = []
        for tid, cfg in items[i:i + 2]:
            check = "✅ " if tid == current_tid else ""
            row.append(InlineKeyboardButton(
                f"{check}{tid}. {cfg['name'][:16]}",
                callback_data=f"tpl_select:{tid}",
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🖼️ Preview All Templates", callback_data="tpl_preview_menu")
    ])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════
#  📡  SAFE SEND HELPERS
# ═══════════════════════════════════════════════════════════════════════
async def safe_send(bot, chat_id, text, **kw):
    try:
        await bot.send_message(chat_id, text, **kw)
    except TelegramError as e:
        log.warning(f"safe_send {chat_id}: {e}")


async def safe_send_photo(bot, chat_id, photo, caption=None, **kw):
    try:
        await bot.send_photo(chat_id, photo, caption=caption, **kw)
    except TelegramError as e:
        log.warning(f"safe_send_photo {chat_id}: {e}")


# ═══════════════════════════════════════════════════════════════════════
#  🏠  /start
# ═══════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    loop = asyncio.get_running_loop()

    referred_by = None
    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("ref_") and arg[4:].isdigit():
            referred_by = int(arg[4:])

    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name, referred_by)
    user = await loop.run_in_executor(None, _db_get_user, u.id)

    # Register admin
    if u.username and u.username.lower() == ADMIN_USERNAME.lower():
        if get_admin_chat_id() != u.id:
            set_admin_chat_id(u.id)
            log.info(f"👑 Admin registered: {u.id}")

    if is_admin(update):
        await update.message.reply_text(
            "👑 <b>ADMIN PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🙋 Welcome back, <b>{u.first_name}</b>!\n\n"
            "📊 Manage users, payments, wallet & broadcasts.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <b>Fayda eSignet OTP flow active</b>",
            reply_markup=admin_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _check_access(update, context):
        return

    tid         = user.get("template_id", 3) or 3
    tname       = TEMPLATES.get(tid, TEMPLATES[3])["name"]
    trials_left = FREE_TRIALS - user["trials_used"]

    if trials_left > 0:
        status_line = (
            f"🎁 <b>Free Trial Ready!</b>\n"
            f"   You have <b>{trials_left}</b> free generation(s)!"
        )
        status_icon = "🟢"
    else:
        birr        = user["wallet"]
        otp_gens    = birr // PRICE_OTP
        status_icon = "🟡" if birr > 0 else "🔴"
        status_line = (
            f"💳 <b>Wallet:</b> {birr} ETB\n"
            f"   🪪 OTP cards: <b>{otp_gens}</b>"
        )

    await update.message.reply_text(
        f"🇪🇹 <b>ሰላም፣ {u.first_name}!</b>  Welcome to Fayda ID Bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🪪 Generate your <b>official Fayda Digital ID</b> card\n"
        "   via the real <b>Fayda </b> — no third-party!\n\n"
        f"{status_icon} <b>Your Status:</b>\n"
        f"{status_line}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 <b>Template:</b> #{tid} — {tname}\n"
        f"   (Color + Grayscale output)\n\n"
        f"💵 <b>Price:</b> {PRICE_OTP} ETB per generation\n"
        f"📱 <b>TeleBirr:</b> <code>{PAYMENT_PHONE}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 <b>Choose an option to get started!</b>",
        reply_markup=user_menu(),
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════════════════
#  🎨  TEMPLATE SELECTION
# ═══════════════════════════════════════════════════════════════════════
async def show_template_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update, context):
        return
    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    user  = await loop.run_in_executor(None, _db_get_user, u.id)
    tid   = user.get("template_id", 3) or 3
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]

    await update.message.reply_text(
        "🎨 <b>Choose Your ID Card Template</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Current:</b> Template #{tid} — {tname}\n\n"
        "🖼️ Each template outputs:\n"
        "   • 🌈 <b>Color</b> version\n"
        "   • ⚫ <b>Grayscale</b> version (PVC-optimised)\n\n"
        "Tap <b>🖼️ Preview All Templates</b> to see them!\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=template_inline_keyboard(tid),
    )


async def cb_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u   = query.from_user
    tid = int(query.data.split(":")[1])
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_set_template, u.id, tid)
    cfg = TEMPLATES[tid]
    await query.edit_message_text(
        f"✅ <b>Template #{tid} Selected!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 <b>{cfg['name']}</b>\n\n"
        f"📤 Outputs: {', '.join(o.capitalize() for o in cfg['outputs'])}\n\n"
        f"Tap <b>🪪 Send OTP</b> to generate your card!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
    )


async def _ensure_sample_bytes(loop) -> list[int]:
    """Render any missing template samples (in parallel) and return the usable ids."""
    tids    = sorted(TEMPLATES.keys())
    missing = [t for t in tids if t not in _SAMPLE_BYTES]
    if missing:
        blobs = await asyncio.gather(
            *(loop.run_in_executor(None, get_template_sample, t) for t in missing)
        )
        for t, blob in zip(missing, blobs):
            if blob:
                _SAMPLE_BYTES[t] = blob
    return [t for t in tids if t in _SAMPLE_BYTES]


async def cb_template_preview_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Loading templates…")
    loop    = asyncio.get_running_loop()
    chat_id = query.message.chat_id
    usable  = await _ensure_sample_bytes(loop)

    if not usable:
        await query.edit_message_text(
            "⚠️ Sample images not found.\n"
            "Upload sample1.png … sample4.png to static/template/ to enable previews.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data="tpl_back")]]
            ),
        )
        return

    def _caption(tid: int) -> str:
        return (f"🖼️ <b>Template #{tid} — {TEMPLATES[tid]['name']}</b>\n"
                f"Outputs: {', '.join(o.capitalize() for o in TEMPLATES[tid]['outputs'])}")

    def _source(tid: int):
        # Telegram file_id after the first upload (nothing re-sent), bytes before that.
        return _SAMPLE_FILE_IDS.get(tid) or io.BytesIO(_SAMPLE_BYTES[tid])

    try:
        media = [InputMediaPhoto(media=_source(t), caption=_caption(t),
                                 parse_mode=ParseMode.HTML) for t in usable]
        sent = await context.bot.send_media_group(chat_id, media=media)
        for t, msg in zip(usable, sent):
            _SAMPLE_FILE_IDS[t] = msg.photo[-1].file_id
    except Exception as e:
        log.warning(f"send_media_group failed: {e}")
        for t in usable:
            msg = await context.bot.send_photo(
                chat_id, photo=io.BytesIO(_SAMPLE_BYTES[t]),
                caption=_caption(t), parse_mode=ParseMode.HTML,
            )
            _SAMPLE_FILE_IDS[t] = msg.photo[-1].file_id

    user    = await loop.run_in_executor(None, _db_get_user, query.from_user.id)
    cur_tid = user.get("template_id", 3) or 3 if user else 3
    await context.bot.send_message(
        chat_id,
        "👆 <b>Tap a template to select it:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=template_inline_keyboard(cur_tid),
    )


async def cb_template_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    loop    = asyncio.get_running_loop()
    user    = await loop.run_in_executor(None, _db_get_user, query.from_user.id)
    cur_tid = user.get("template_id", 3) or 3 if user else 3
    await query.edit_message_text(
        "🎨 <b>Choose Your ID Card Template</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap 🖼️ Preview to see each template.\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=template_inline_keyboard(cur_tid),
    )


async def cb_cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the inline ❌ Cancel button (cancel_flow callback)."""
    query = update.callback_query
    await query.answer("Cancelled.")
    context.user_data.clear()
    await query.edit_message_text(
        "❌ <b>Cancelled.</b>\n\nUse the menu below to start again.",
        parse_mode=ParseMode.HTML,
    )
    try:
        await query.message.reply_text(
            "👇 Main menu:", reply_markup=user_menu()
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  📷  QR SCAN FLOW — user sends QR image → FAN auto-extracted → OTP sent
# ═══════════════════════════════════════════════════════════════════════

def _decode_fayda_qr_payload(qr_text: str) -> dict | None:
    """Decode Base45 → zlib → CBOR Fayda QR payload. Returns dict or None."""
    try:
        import base45, cbor2
        raw      = base45.b45decode(qr_text.strip())
        inflated = zlib.decompress(raw)
        return cbor2.loads(inflated)
    except Exception:
        return None


def _extract_fan_from_qr_text(qr_text: str) -> str | None:
    """
    Extract a 16-digit FAN from a Fayda QR payload.

    Supports two formats:
      1. New — Base45 → zlib → CBOR with 'individual_id' field
      2. Legacy plain-text — :A:{16 digits}:
    Falls back to any standalone 16-digit number in the raw text.
    """
    # Format 1: decode CBOR payload
    payload = _decode_fayda_qr_payload(qr_text)
    if payload:
        fan = str(payload.get("individual_id", "")).strip()
        if re.fullmatch(r'\d{16}', fan):
            return fan

    # Format 2: legacy :A:{FAN}: pattern
    m = re.search(r':A:(\d{16})(?::|$)', qr_text)
    if m:
        return m.group(1)

    # Fallback: bare 16-digit number
    m = re.search(r'(?<!\d)(\d{16})(?!\d)', qr_text)
    if m:
        return m.group(1)

    return None


def _decode_qr_image_bytes(data: bytes):
    """Runs in a worker thread: decoding a phone photo can take a second or more."""
    return zbar_decode(Image.open(io.BytesIO(data)))


async def _prepare_quietly(client: EsignetClient) -> None:
    """Background CSRF fetch. A failure is harmless: initialize() retries inline."""
    try:
        await asyncio.get_running_loop().run_in_executor(None, client.prepare)
    except Exception as e:
        log.debug(f"eSignet prefetch failed (will retry inline): {e}")


async def recv_qr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point: user sends a QR image directly to the bot.

    Flow:
      1. Decode QR with OpenCV — fast, zero native deps.
      2. Extract 16-digit FAN from payload.
      3. Reply with decoded FAN.
      4. Initialise eSignet client + send OTP.
      5. Store original QR image bytes so template merge uses them.
      6. Transition to WAIT_OTP — same handler as the manual flow.
    """
    if not await _check_access(update, context):
        return ConversationHandler.END

    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    # ── Balance check ─────────────────────────────────────────────────
    can = await loop.run_in_executor(None, _db_can_generate, u.id, PRICE_OTP)
    if not can:
        has_pending = await loop.run_in_executor(None, _db_has_pending, u.id)
        if has_pending:
            await update.message.reply_text(
                "⏳ <b>Payment Under Review</b>\n\n"
                "🔍 Your payment is being reviewed.\n"
                "   You'll get a notification once approved!",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "❌ <b>Insufficient Balance</b>\n\n"
                f"💰 You need at least <b>{PRICE_OTP} ETB</b> to generate an ID card.\n\n"
                "📱 Tap <b>💰 Top Up</b> to add funds.",
                parse_mode=ParseMode.HTML,
            )
        return ConversationHandler.END

    msg = await update.message.reply_text("🔍 <b>Scanning QR code…</b>", parse_mode=ParseMode.HTML)

    # ── Download photo ────────────────────────────────────────────────
    photo    = update.message.photo[-1]          # largest resolution
    file_obj = await context.bot.get_file(photo.file_id)
    buf      = io.BytesIO()
    await file_obj.download_to_memory(buf)
    buf.seek(0)
    # Start the eSignet handshake now; it overlaps the QR decode below.
    client = EsignetClient()
    prep   = asyncio.create_task(_prepare_quietly(client))
    qr_image_bytes = buf.getvalue()              # keep original for template

    # ── Decode QR — same logic as qr_extractor.py (pyzbar / zbar) ──
    try:
        codes = await loop.run_in_executor(None, _decode_qr_image_bytes, qr_image_bytes)
        if not codes:
            await msg.edit_text(
                "❌ <b>No QR code detected</b>\n\n"
                "Please send a <b>clear, well-lit</b> photo of the QR code.\n"
                "Make sure the QR fills most of the image and is not blurry.",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END

        qr_text = codes[0].data.decode("utf-8", errors="replace")
        log.info(f"QR decoded for {u.id}: {qr_text[:80]}…")

    except Exception as e:
        log.warning(f"QR decode error: {e}")
        await msg.edit_text(
            f"❌ <b>QR Decode Failed</b>\n\n<code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    # ── Extract FAN ───────────────────────────────────────────────────
    fan = _extract_fan_from_qr_text(qr_text)
    if not fan:
        await msg.edit_text(
            "❌ <b>FAN Not Found in QR</b>\n\n"
            "Could not locate a 16-digit Fayda Application Number.\n\n"
            f"QR preview: <code>{qr_text[:120]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    # ── Prepare session ───────────────────────────────────────────────
    context.user_data.clear()
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid  = (user.get("template_id", 3) or 3) if user else 3
    context.user_data["template_id"]    = tid
    context.user_data["fan"]            = fan
    # Store as base64 — will be set as api_data["qr_crop"] before merge
    context.user_data["user_qr_b64"]   = base64.b64encode(qr_image_bytes).decode()

    await msg.edit_text(
        "✅ <b>QR Decoded!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 <b>FAN:</b> <code>{fan}</code>\n\n"
        "📡 Connecting to Fayda server…  🔄",
        parse_mode=ParseMode.HTML,
    )

    # ── eSignet: initialize + send OTP ───────────────────────────────
    await asyncio.wait([prep])   # usually finished already

    def _do_send_otp():
        client.initialize(fan)
        client.get_oauth_details()
        return client.send_otp()

    try:
        otp_info = await loop.run_in_executor(None, _do_send_otp)
        context.user_data["esignet_client"] = client

        masked_email  = otp_info.get("maskedEmail",  "")
        masked_mobile = otp_info.get("maskedMobile", "")
        delivery_note = ""
        if masked_mobile:
            delivery_note += f"📱 Phone: <code>{masked_mobile}</code>\n"
        if masked_email:
            delivery_note += f"✉️ Email: <code>{masked_email}</code>\n"

        await msg.edit_text(
            "📲 <b>OTP Sent!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 FAN: <code>{fan}</code>\n"
            f"{delivery_note or '📬 OTP dispatched to your registered contact.'}\n"
            f"🎨 Template: <b>#{tid} — {TEMPLATES[tid]['name']}</b>\n\n"
            "📋 <b>Enter the OTP you received:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_inline_kb(),
        )
        return WAIT_OTP

    except EsignetError as e:
        log.warning(f"eSignet QR-flow send_otp error: {e}")
        await msg.edit_text(
            f"❌ <b>Fayda API Error</b>\n\n<code>{e}</code>\n\n"
            "Please try again or enter your FAN manually via <b>🪪 Send OTP</b>.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    except Exception as e:
        log.exception("recv_qr_photo: unexpected error")
        await msg.edit_text(
            f"❌ <b>Connection Error</b>\n\n{str(e)[:200]}\n\nPlease try again.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════
#  🪪  OTP FLOW — Powered by Fayda eSignet (auth.fayda.et)
# ═══════════════════════════════════════════════════════════════════════
async def start_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: user taps '🪪 Send OTP'."""
    if not await _check_access(update, context):
        return ConversationHandler.END

    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    can = await loop.run_in_executor(None, _db_can_generate, u.id, PRICE_OTP)
    if not can:
        has_pending = await loop.run_in_executor(None, _db_has_pending, u.id)
        if has_pending:
            await update.message.reply_text(
                "⏳ <b>Payment Under Review</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔍 Your payment is being reviewed.\n"
                "   You'll get a notification once approved!\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "❌ <b>Insufficient Balance</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 You need at least <b>{PRICE_OTP} ETB</b>\n"
                "   to generate an ID card.\n\n"
                "📱 Tap <b>💰 Top Up</b> to add funds\n"
                f"   via TeleBirr: <code>{PAYMENT_PHONE}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML,
            )
        return ConversationHandler.END

    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid  = user.get("template_id", 3) or 3
    context.user_data.clear()
    context.user_data["template_id"] = tid

    # Warm up the eSignet session while the user types their FAN (hides a round trip).
    client = EsignetClient()
    context.user_data["esignet_client"] = client
    context.user_data["esignet_prep"]   = asyncio.create_task(_prepare_quietly(client))

    await update.message.reply_text(
        "🪪 <b>Fayda ID Card </b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 <b>Powered by official page</b>\n\n"
        f"🎨 Template: <b>#{tid} — {TEMPLATES[tid]['name']}</b>\n\n"
        "📋 <b>Step 1 of 2 — Enter your FAN</b>\n\n"
        "🔢 Type your <b>16-digit Fayda Application\n"
        "   Number (FAN)</b>:\n\n"
        "   Example: <code>1234567890123456</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_FAN


async def recv_fan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: receive FAN, initialize eSignet, send OTP."""
    text = update.message.text.strip()
    if text == "❌ Cancel":
        return await _cancel(update, context)

    digits = re.sub(r"[\s\-]", "", text)
    if not digits.isdigit() or len(digits) != 16:
        await update.message.reply_text(
            "⚠️ <b>Invalid FAN</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "❗ Your FAN must be <b>exactly 16 digits</b>.\n\n"
            "✏️ Try again:\n"
            "   Example: <code>1234567890123456</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_FAN

    context.user_data["fan"] = digits

    msg = await update.message.reply_text(
        "⏳ <b>Connecting to Fayda Server…</b>\n\n"
        "📡 Send  →  check  →  Sending OTP\n"
        "   Please wait…  🔄"
    )

    loop   = asyncio.get_running_loop()
    client = context.user_data.get("esignet_client") or EsignetClient()
    prep   = context.user_data.pop("esignet_prep", None)
    if prep is not None:
        await asyncio.wait([prep])   # never raises; usually done while the user typed the FAN

    def _do_send_otp():
        client.initialize(digits)
        client.get_oauth_details()
        return client.send_otp()   # returns {"maskedEmail":..., "maskedMobile":...}

    try:
        otp_info = await loop.run_in_executor(None, _do_send_otp)
        context.user_data["esignet_client"] = client

        masked_email  = otp_info.get("maskedEmail",  "")
        masked_mobile = otp_info.get("maskedMobile", "")
        delivery_note = ""
        if masked_mobile:
            delivery_note += f"📱 Phone: <code>{masked_mobile}</code>\n"
        if masked_email:
            delivery_note += f"✉️ Email: <code>{masked_email}</code>\n"

        await msg.edit_text(
            "📲 <b>OTP Sent Successfully!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{delivery_note or '📬 OTP dispatched to your registered contact.'}\n"
            "📋 <b>Step 2 of 2 — Enter OTP</b>\n\n"
            "🔢 Type the OTP you received:\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_OTP

    except EsignetError as e:
        log.warning(f"eSignet error on send_otp: {e}")
        await msg.edit_text(
            f"❌ <b>Fayda API Error</b>\n\n"
            f"<code>{e}</code>\n\n"
            "Please check your FAN and try again.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_FAN

    except Exception as e:
        log.exception("send_otp unexpected error")
        await msg.edit_text(
            f"❌ <b>Connection Error</b>\n\n"
            f"{str(e)[:200]}\n\nPlease try again.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_FAN


_BG_TASKS: set = set()   # strong references so background tasks can't be garbage-collected mid-run


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def _release_session(context, client) -> None:
    """Once the flow ends, drop the eSignet session and the stored QR image."""
    context.user_data.pop("esignet_client", None)
    context.user_data.pop("esignet_prep", None)
    context.user_data.pop("user_qr_b64", None)
    if client is not None:
        client.close()


async def _send_card_album(bot, chat_id, reply_to_id, rendered: dict, caption: str) -> dict:
    """Both cards as one album: a single upload request. Returns {mode: file_id}."""
    modes  = [m for m in ("gray", "color") if rendered.get(m)]
    labels = {"gray": "⚫ Grayscale", "color": "🌈 Color"}
    if len(modes) == 1:
        m   = modes[0]
        msg = await bot.send_photo(chat_id, photo=io.BytesIO(rendered[m]), caption=caption,
                                   parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_id)
        return {m: msg.photo[-1].file_id}
    media = [
        InputMediaPhoto(
            media=io.BytesIO(rendered[m]),
            caption=caption if i == 0 else f"✅ <b>{labels[m]}</b>",
            parse_mode=ParseMode.HTML,
        )
        for i, m in enumerate(modes)
    ]
    msgs = await bot.send_media_group(chat_id, media=media, reply_to_message_id=reply_to_id)
    return {m: msgs[i].photo[-1].file_id for i, m in enumerate(modes)}


async def _notify_admin_card(bot, admin_id, *, header: str, raw_fid, card_fids: dict) -> None:
    """Background: re-sends by file_id, so the admin gets everything without new uploads."""
    try:
        if raw_fid:
            await bot.send_photo(admin_id, photo=raw_fid,
                                 caption=f"{header}\n📷 <b>Raw Photo</b>",
                                 parse_mode=ParseMode.HTML)
        modes = [m for m in ("gray", "color") if m in card_fids]
        captions = {m: f"<b>{'🌈 Color' if m == 'color' else '⚫ Grayscale'} ID</b>" for m in modes}
        if len(modes) == 1:
            m = modes[0]
            await bot.send_photo(admin_id, photo=card_fids[m], caption=captions[m],
                                 parse_mode=ParseMode.HTML)
        elif modes:
            media = [InputMediaPhoto(media=card_fids[m], caption=captions[m],
                                     parse_mode=ParseMode.HTML) for m in modes]
            await bot.send_media_group(admin_id, media=media)
    except Exception as e:
        log.warning(f"admin notify failed: {e}")


async def recv_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: receive OTP, complete eSignet flow, generate ID card."""
    text = update.message.text.strip()
    if text == "❌ Cancel":
        return await _cancel(update, context)

    client: EsignetClient | None = context.user_data.get("esignet_client")
    fan    = context.user_data.get("fan", "")
    tid    = context.user_data.get("template_id", 3) or 3

    if not client or not fan:
        await update.message.reply_text(
            "⚠️ Session expired. Please tap <b>🪪 Send OTP</b> to start again.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    cfg = TEMPLATES.get(tid, TEMPLATES[3])

    msg = await update.message.reply_text(
        "⚙️ <b>Generating your Fayda ID Card…</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "  🔐 Verifying OTP…\n"
        "  📜 Fetching info…\n"
        "  🔑 Look for…\n"
        "  👤 Loading  …\n"
        f"  🖼️ Rendering Template #{tid} ({cfg['name']})…\n\n"
        "⏱️ Please wait…\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    loop = asyncio.get_running_loop()

    def _complete_flow():
        client.verify_otp(text)
        auth_code = client.get_auth_code()
        client.exchange_auth_code(auth_code)
        raw = client.get_identity()
        return EsignetClient.parse_identity(raw, fan)

    try:
        api_data = await loop.run_in_executor(None, _complete_flow)
        # Attach resident JWT so the QR encodes a real signed payload
        api_data["qr_jwt"] = client.resident_jwt

        # If the user started with a QR photo, that original image goes on the card.
        user_qr_b64 = context.user_data.get("user_qr_b64", "")
        if user_qr_b64:
            api_data["qr_crop"] = user_qr_b64
            log.info("recv_otp: using user-supplied QR image for template merge")

        # Photo cutout, QR and text layer are built in parallel inside render_cards().
        rendered = await loop.run_in_executor(None, render_cards, api_data, fan, tid)

        deduct_type, remaining = await loop.run_in_executor(
            None, _db_deduct, update.effective_user.id, PRICE_OTP
        )
        if deduct_type == "trial":
            bal_note = (
                f"🎁 Free trial used! <b>{remaining}</b> remaining."
                if remaining > 0
                else "🎁 Last free trial used. Tap 💰 Top Up to continue!"
            )
        else:
            gens_left = remaining // PRICE_OTP
            bal_note  = (
                f"💳 Wallet: <b>{remaining} ETB</b>  "
                f"({gens_left} generation(s) left)"
            )

        await msg.delete()

        raw_photo_b64   = api_data.get("photo", "")
        raw_photo_bytes = None
        if raw_photo_b64:
            try:
                raw_photo_bytes = safe_b64decode(raw_photo_b64)
            except Exception:
                pass

        name_eng = api_data.get("fullName", {}).get("eng", "")
        labels   = [("⚫ Grayscale" if m == "gray" else "🌈 Color")
                    for m in ("gray", "color") if m in rendered]
        card_caption = (
            f"✅ <b>Fayda ID Card — {' & '.join(labels)}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 {name_eng}\n"
            f"🎨 Template #{tid}: <b>{cfg['name']}</b>\n"
            f"{bal_note}\n\n"
            "🖨️ Want print-ready? Tap <b>🖨️ A4 Converter</b>!\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        async def _send_raw():
            if not raw_photo_bytes:
                return None
            raw_msg = await update.message.reply_photo(
                photo=io.BytesIO(raw_photo_bytes),
                caption="📷 <b>Your Fayda Photo</b>",
                parse_mode=ParseMode.HTML,
            )
            return raw_msg.photo[-1].file_id

        # The raw photo and the card album are uploaded at the same time.
        raw_fid, card_fids = await asyncio.gather(
            _send_raw(),
            _send_card_album(context.bot, update.message.chat_id,
                             update.message.message_id, rendered, card_caption),
        )

        # Admin copy runs in the background; the user doesn't wait for it.
        admin_id = get_admin_chat_id()
        if admin_id:
            u      = update.effective_user
            uin    = api_data.get("uin", "N/A")
            header = (
                f"🔔 <b>New ID Generated</b>\n"
                f"👤 {name_eng}  |  UIN: <code>{uin}</code>\n"
                f"📱 {u.full_name} (@{u.username or 'N/A'})\n"
                f"🆔 Chat: <code>{u.id}</code>\n"
                f"🎨 Template #{tid}: {cfg['name']}"
            )
            _spawn(_notify_admin_card(context.bot, admin_id, header=header,
                                      raw_fid=raw_fid, card_fids=card_fids))

        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text(
            "👇 What would you like to do next?", reply_markup=kb
        )
        return ConversationHandler.END

    except EsignetError as e:
        log.warning(f"eSignet error completing flow: {e}")
        await msg.edit_text(
            f"❌ <b>Fayda API Error</b>\n\n"
            f"<code>{e}</code>\n\n"
            "Please check your OTP and try again.\n"
            "OTPs expire after 3 minutes.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    except Exception as e:
        log.exception("recv_otp: unexpected error")
        await msg.edit_text(
            f"❌ <b>Error generating ID card</b>\n\n"
            f"{str(e)[:300]}\n\nPlease try again.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    finally:
        _release_session(context, client)


# ═══════════════════════════════════════════════════════════════════════
#  💰  TOP UP FLOW
# ═══════════════════════════════════════════════════════════════════════
async def start_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update, context):
        return ConversationHandler.END

    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    has_pending = await loop.run_in_executor(None, _db_has_pending, u.id)
    if has_pending:
        await update.message.reply_text(
            "⏳ <b>Payment Still Pending</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 You already have a payment under review.\n"
            "   You'll be notified once approved!\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "💰 <b>Top Up Your Wallet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Step 1</b> — Send ETB to TeleBirr:\n"
        f"   📱 <code>{PAYMENT_PHONE}</code>\n\n"
        f"   💵 OTP Generation: <b>{PRICE_OTP} ETB</b> each\n\n"
        "<b>Step 2</b> — Screenshot the confirmation\n\n"
        "<b>Step 3</b> — Send screenshot here with the\n"
        "   amount as the caption 👇\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Approval usually within minutes!",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_SCREENSHOT


async def recv_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip() == "❌ Cancel":
        return await _cancel(update, context)

    if not update.message.photo:
        await update.message.reply_text(
            "📸 <b>Screenshot Required</b>\n\n"
            "Please send a <b>photo screenshot</b> of your\n"
            "TeleBirr payment confirmation.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_SCREENSHOT

    u       = update.effective_user
    photo   = update.message.photo[-1]
    file_id = photo.file_id
    loop    = asyncio.get_running_loop()

    amount  = PRICE_OTP
    caption = (update.message.caption or "").strip()
    if caption.isdigit() and int(caption) > 0:
        amount = int(caption)

    pid      = await loop.run_in_executor(
        None, _db_add_payment, u.id, u.username, u.full_name, amount, file_id
    )
    admin_id = get_admin_chat_id()
    if admin_id:
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{pid}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no:{pid}"),
            ]]
        )
        try:
            await context.bot.send_photo(
                admin_id,
                photo=file_id,
                caption=(
                    f"🔔 <b>New Payment Request #{pid}</b>\n\n"
                    f"👤 {u.full_name} (@{u.username or 'N/A'})\n"
                    f"🆔 <code>{u.id}</code>\n"
                    f"💵 Amount: <b>{amount} ETB</b>\n"
                    f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            log.warning(f"Admin notify failed: {e}")

    await update.message.reply_text(
        "✅ <b>Screenshot Received!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Reference: <b>#{pid}</b>\n"
        f"💵 Amount:   <b>{amount} ETB</b>\n\n"
        "⏳ Waiting for admin approval…\n"
        "   🔔 You'll be notified automatically!\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=user_menu(),
    )
    return ConversationHandler.END


async def cb_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves / rejects a payment."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    action, pid_str = query.data.split(":")
    pid    = int(pid_str)
    loop   = asyncio.get_running_loop()
    pay    = await loop.run_in_executor(None, _db_get_payment, pid)

    if not pay:
        await query.edit_message_caption("❌ Payment not found.")
        return
    if pay["status"] != "pending":
        await query.answer(
            f"Already {pay['status']}.", show_alert=True
        )
        return

    reviewer = query.from_user.username or str(query.from_user.id)

    if action == "pay_ok":
        await loop.run_in_executor(
            None, _db_update_payment, pid, "approved", reviewer
        )
        await loop.run_in_executor(
            None, _db_add_wallet, pay["chat_id"], pay["amount"]
        )
        # Referral bonus
        referrer_id = await loop.run_in_executor(
            None, _db_reward_referrer_if_eligible, pay["chat_id"]
        )
        user = await loop.run_in_executor(None, _db_get_user, pay["chat_id"])
        bal  = user["wallet"] if user else pay["amount"]
        gens = bal // PRICE_OTP

        await query.edit_message_caption(
            f"✅ <b>Payment #{pid} Approved!</b>\n"
            f"👤 {pay['full_name']}\n"
            f"💵 +{pay['amount']} ETB\n"
            f"💳 New balance: {bal} ETB",
            parse_mode=ParseMode.HTML,
        )
        await safe_send(
            context.bot, pay["chat_id"],
            f"✅ <b>Payment Approved!</b>\n\n"
            f"💵 +<b>{pay['amount']} ETB</b> added to your wallet!\n"
            f"💳 Balance: <b>{bal} ETB</b> ({gens} generation(s))\n\n"
            f"🪪 Tap <b>🪪 Send OTP</b> to generate your ID!",
            parse_mode=ParseMode.HTML,
        )
        if referrer_id:
            await safe_send(
                context.bot, referrer_id,
                f"🎁 <b>Referral Bonus!</b>\n\n"
                f"+<b>{REFERRAL_BONUS} ETB</b> added to your wallet!\n"
                f"Someone you invited just topped up.",
                parse_mode=ParseMode.HTML,
            )
    else:  # pay_no
        await loop.run_in_executor(
            None, _db_update_payment, pid, "rejected", reviewer
        )
        await query.edit_message_caption(
            f"❌ <b>Payment #{pid} Rejected</b>\n"
            f"👤 {pay['full_name']}\n"
            f"💵 {pay['amount']} ETB",
            parse_mode=ParseMode.HTML,
        )
        await safe_send(
            context.bot, pay["chat_id"],
            "❌ <b>Payment Rejected</b>\n\n"
            "Your payment screenshot was not approved.\n"
            "Please try again or contact support.",
            parse_mode=ParseMode.HTML,
        )


# ═══════════════════════════════════════════════════════════════════════
#  🖨️  A4 CONVERTER FLOW
# ═══════════════════════════════════════════════════════════════════════
async def start_a4_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update, context):
        return ConversationHandler.END

    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    context.user_data["a4_images"] = []

    await update.message.reply_text(
        "🖨️ <b>A4 PVC Card Converter</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📐 Creates print-ready A4 layout:\n"
        "   ✂️ Crop marks for precise cutting\n"
        "   ⊕ Center alignment guides\n"
        "   🗂️ Up to 5 cards per A4 page\n"
        "   📄 Output: print-ready PDF + page previews\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>Upload only ID card images</b>\n"
        f"   from this bot ({CARD_WIDTH}×{CARD_HEIGHT} px).\n\n"
        "📤 Send card image(s) now:",
        parse_mode=ParseMode.HTML,
        reply_markup=a4_collect_kb(),
    )
    return WAIT_A4_IMAGES


async def recv_a4_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        context.user_data.pop("a4_images", None)
        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("❌ Cancelled.", reply_markup=kb)
        return ConversationHandler.END

    if text == "✅ Done — Generate A4":
        images_data = context.user_data.get("a4_images", [])
        if not images_data:
            await update.message.reply_text(
                "⚠️ No images yet! Send at least one ID card image.",
                parse_mode=ParseMode.HTML,
            )
            return WAIT_A4_IMAGES
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("↔️ Flip Horizontally",  callback_data="a4_flip_yes"),
                InlineKeyboardButton("➡️ Keep Normal",        callback_data="a4_flip_no"),
            ]]
        )
        await update.message.reply_text(
            "↔️ <b>Horizontal Flip?</b>\n\n"
            f"📦 {len(images_data)} image(s) ready.\n\n"
            "Flip cards for double-sided PVC printing?\n",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return WAIT_A4_FLIP

    # Collect image
    photo    = None
    loop     = asyncio.get_running_loop()
    file_obj = None

    if update.message.photo:
        photo    = update.message.photo[-1]
        file_obj = await context.bot.get_file(photo.file_id)
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith("image/"):
            file_obj = await context.bot.get_file(doc.file_id)

    if not file_obj:
        await update.message.reply_text(
            "📎 Please send an image file (photo or document)."
        )
        return WAIT_A4_IMAGES

    buf   = io.BytesIO()
    await file_obj.download_to_memory(buf)
    buf.seek(0)
    try:
        img = Image.open(buf).convert("RGB")
    except Exception:
        await update.message.reply_text("⚠️ Could not open image. Please try again.")
        return WAIT_A4_IMAGES

    if img.size != (CARD_WIDTH, CARD_HEIGHT):
        await update.message.reply_text(
            f"⚠️ <b>Wrong size!</b>\n\n"
            f"Expected {CARD_WIDTH}×{CARD_HEIGHT} px,\n"
            f"got {img.size[0]}×{img.size[1]} px.\n\n"
            "Please upload a card exported from this bot.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_A4_IMAGES

    context.user_data.setdefault("a4_images", []).append(img)
    count = len(context.user_data["a4_images"])
    await update.message.reply_text(
        f"✅ <b>Image {count} added!</b>\n\n"
        f"📤 Send more cards or tap <b>✅ Done</b>.",
        parse_mode=ParseMode.HTML,
    )
    return WAIT_A4_IMAGES


async def cb_a4_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    mirror = query.data == "a4_flip_yes"
    images = context.user_data.get("a4_images", [])

    if not images:
        await query.edit_message_text("⚠️ No images found. Please start again.")
        return ConversationHandler.END

    await query.edit_message_text(
        "⚙️ <b>Building A4 layout…</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 Arranging {len(images)} card(s)…\n"
        f"{'↔️ Mirror enabled' if mirror else '➡️ Normal orientation'}\n"
        "⏱️ Please wait…\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
    )

    loop    = asyncio.get_running_loop()
    chat_id = query.message.chat_id
    try:
        pages, pdf_bytes, previews = await loop.run_in_executor(
            None, generate_a4_pages, images, mirror
        )
        if not pages:
            await query.edit_message_text(
                "⚠️ No valid card images were found.\n"
                "Make sure images are exactly 2191×667 px."
            )
            return ConversationHandler.END

        preview_caption = (
            f"🖨️ <b>A4 Layout — {len(previews)} page(s)</b>\n"
            f"📐 {len(images)} cards  •  300 DPI"
        )

        async def _send_previews():
            if len(previews) == 1:
                await context.bot.send_photo(
                    chat_id, photo=io.BytesIO(previews[0]),
                    caption=preview_caption, parse_mode=ParseMode.HTML,
                )
            else:
                media = [
                    InputMediaPhoto(media=io.BytesIO(blob),
                                    caption=preview_caption if i == 0 else None,
                                    parse_mode=ParseMode.HTML)
                    for i, blob in enumerate(previews)
                ]
                await context.bot.send_media_group(chat_id, media=media)

        async def _send_pdf():
            await context.bot.send_document(
                chat_id,
                document=io.BytesIO(pdf_bytes),
                filename="Fayda_A4_PVC_Ready.pdf",
                caption=(
                    "📄 <b>A4 PDF — Print Ready!</b>\n\n"
                    f"📐 {len(images)} ID card(s) per page  •  300 DPI\n"
                    "✂️ Crop marks included"
                ),
                parse_mode=ParseMode.HTML,
            )

        # Previews and the PDF go out at the same time.
        await asyncio.gather(_send_previews(), _send_pdf())

        kb = admin_menu() if is_admin(update) else user_menu()
        await context.bot.send_message(
            chat_id,
            "✅ <b>A4 Layout Ready!</b>\n\nSend to printer for PVC card printing.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    except Exception as e:
        log.exception("A4 generation failed")
        await query.edit_message_text(
            f"❌ <b>A4 generation failed</b>\n\n{str(e)[:200]}"
        )

    context.user_data.pop("a4_images", None)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════
#  📋  ADMIN HANDLERS
# ═══════════════════════════════════════════════════════════════════════
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    loop    = asyncio.get_running_loop()
    pending = await loop.run_in_executor(None, _db_pending_payments)
    if not pending:
        await update.message.reply_text(
            "📋 <b>Pending Payments</b>\n\n✅ None! All clear.",
            parse_mode=ParseMode.HTML,
        )
        return
    for pay in pending[:10]:
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{pay['id']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no:{pay['id']}"),
            ]]
        )
        await update.message.reply_text(
            f"🔔 <b>Payment #{pay['id']}</b>\n\n"
            f"👤 {pay['full_name']} (@{pay['username'] or 'N/A'})\n"
            f"🆔 <code>{pay['chat_id']}</code>\n"
            f"💵 Amount: <b>{pay['amount']} ETB</b>\n"
            f"📅 {pay['created_at']}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    loop       = asyncio.get_running_loop()
    tu, tg, pp, tr = await loop.run_in_executor(None, _db_stats)
    await update.message.reply_text(
        "📈 <b>Bot Statistics</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users:      <b>{tu}</b>\n"
        f"🪪 Total Generations: <b>{tg}</b>\n"
        f"⏳ Pending Payments: <b>{pp}</b>\n"
        f"💰 Total Revenue:    <b>{tr} ETB</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
    )


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    loop  = asyncio.get_running_loop()
    users = await loop.run_in_executor(None, _db_all_users, 20)
    if not users:
        await update.message.reply_text("👥 No users found.")
        return
    lines = [f"👥 <b>Recent Users (last 20)</b>\n\n━━━━━━━━━━━━━━━━━━━━━━"]
    for u in users:
        trial_badge = "🎁" if u["trials_used"] < FREE_TRIALS else "💳"
        lines.append(
            f"{trial_badge} <b>{u['full_name'] or 'N/A'}</b>\n"
            f"   @{u['username'] or 'N/A'}  |  ID: <code>{u['chat_id']}</code>\n"
            f"   💰 {u['wallet']} ETB  |  🎨 Tmpl #{u.get('template_id',3)}\n"
        )
    await update.message.reply_text(
        "\n".join(lines[:25]),
        parse_mode=ParseMode.HTML,
    )


async def start_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ <b>Edit User Wallet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>CHAT_ID AMOUNT</code>\n\n"
        "Examples:\n"
        "   <code>123456789 +50</code>  → add 50 ETB\n"
        "   <code>123456789 -20</code>  → subtract 20 ETB\n"
        "   <code>123456789 100</code>  → set to 100 ETB\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_EDIT_CREDITS


async def recv_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "❌ Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return ConversationHandler.END
    try:
        parts    = text.split()
        if len(parts) < 2:
            raise ValueError()
        chat_id   = int(parts[0])
        amt_str   = parts[1]
        loop      = asyncio.get_running_loop()
        user      = await loop.run_in_executor(None, _db_get_user, chat_id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return WAIT_EDIT_CREDITS
        if amt_str.startswith("+"):
            amt    = int(amt_str[1:])
            new_bal = user["wallet"] + amt
            action  = f"➕ Added {amt} ETB"
        elif amt_str.startswith("-"):
            amt    = int(amt_str[1:])
            new_bal = max(0, user["wallet"] - amt)
            action  = f"➖ Subtracted {amt} ETB"
        else:
            new_bal = int(amt_str)
            action  = f"📌 Set to {new_bal} ETB"
        await loop.run_in_executor(None, _db_set_wallet, chat_id, new_bal)
        gens = new_bal // PRICE_OTP
        await update.message.reply_text(
            f"✅ <b>Wallet Updated!</b>\n\n"
            f"👤 {user['full_name']} (@{user['username'] or 'N/A'})\n"
            f"🔧 {action}\n"
            f"💳 New Balance: <b>{new_bal} ETB</b> ({gens} gen(s))\n",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu(),
        )
        await safe_send(
            context.bot, chat_id,
            f"💳 <b>Wallet Updated!</b>\n\nNew balance: <b>{new_bal} ETB</b>\n"
            f"🪪 Generations: <b>{gens}</b>",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text(
            "⚠️ Invalid format.\nUse: <code>CHAT_ID AMOUNT</code>",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_EDIT_CREDITS


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "📣 <b>Broadcast Message</b>\n\n"
        "Type the message to send to all users:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_BROADCAST


class _Pacer:
    """Spaces sends out so the bot stays under Telegram's broadcast limit (about 30/s)."""
    def __init__(self, per_second: float):
        self._gap  = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            now  = loop.time()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = loop.time()
            self._next = now + self._gap


def _seconds(value) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


async def recv_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "❌ Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return ConversationHandler.END

    loop     = asyncio.get_running_loop()
    chat_ids = await loop.run_in_executor(None, _db_get_all_chat_ids)
    status   = await update.message.reply_text(
        f"📣 Broadcasting to <b>{len(chat_ids)}</b> users…",
        parse_mode=ParseMode.HTML,
    )
    msg_text = (
        f"📢 <b>Message from Fayda ID Bot</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━"
    )
    pacer = _Pacer(per_second=25)
    slots = asyncio.Semaphore(50)

    async def _send_one(cid) -> bool:
        async with slots:
            for _ in range(3):
                await pacer.wait()
                try:
                    await context.bot.send_message(cid, msg_text, parse_mode=ParseMode.HTML)
                    return True
                except RetryAfter as e:
                    await asyncio.sleep(_seconds(e.retry_after) + 0.5)
                except Exception:
                    return False
            return False

    results = await asyncio.gather(*(_send_one(cid) for cid in chat_ids))
    sent    = sum(results)
    failed  = len(results) - sent

    try:
        await status.edit_text(
            f"📣 <b>Broadcast Done!</b>\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu(),
        )
    except Exception:
        pass
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════
#  💳  WALLET & SUPPORT & INVITE
# ═══════════════════════════════════════════════════════════════════════
async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update, context):
        return
    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    user        = await loop.run_in_executor(None, _db_get_user, u.id)
    trials_left = max(0, FREE_TRIALS - user["trials_used"])
    otp_gens    = user["wallet"] // PRICE_OTP
    tid         = user.get("template_id", 3) or 3
    tname       = TEMPLATES.get(tid, TEMPLATES[3])["name"]
    status      = "🟢 Ready to generate!" if (trials_left > 0 or user["wallet"] > 0) else "🔴 Top up to generate"

    await update.message.reply_text(
        "💳 <b>My Wallet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {u.full_name}\n\n"
        f"🎁 Free Trials:  <b>{trials_left}</b> remaining\n"
        f"💰 Balance:      <b>{user['wallet']} ETB</b>\n"
        f"🪪 Generations:  <b>{otp_gens}</b> (at {PRICE_OTP} ETB)\n\n"
        f"🎨 Template:     <b>#{tid} — {tname}</b>\n\n"
        f"{status}\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
    )



async def show_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update, context):
        return
    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    bot_info            = context.bot
    bot_uname           = _BOT_USERNAME or (await bot_info.get_me()).username
    link                = f"https://t.me/{bot_uname}?start=ref_{u.id}"
    _, _, invited, earned = await loop.run_in_executor(None, _db_get_referral_info, u.id)

    await update.message.reply_text(
        "🎁 <b>Invite & Earn</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Earn <b>{REFERRAL_BONUS} ETB</b> for every friend\n"
        "   who joins and tops up their wallet!\n\n"
        "🔗 <b>Your Link:</b>\n"
        f"<code>{link}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Friends Invited: <b>{invited}</b>\n"
        f"💰 Total Earned:    <b>{earned} ETB</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════════════════
#  🔒  WHITELIST ADMIN HANDLERS
# ═══════════════════════════════════════════════════════════════════════
async def admin_whitelist_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show whitelist and usage instructions — admin only."""
    if not is_admin(update):
        return
    loop  = asyncio.get_running_loop()
    users = await loop.run_in_executor(None, _db_whitelist_all)
    if not users:
        await update.message.reply_text(
            "🔒 <b>Whitelist</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 No users added yet.\n\n"
            "➕ <b>Add user:</b>    /adduser CHAT_ID\n"
            "➖ <b>Remove user:</b> /removeuser CHAT_ID\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = [f"🔒 <b>Whitelist — {len(users)} user(s)</b>\n\n━━━━━━━━━━━━━━━━━━━━━━"]
    for row in users:
        lines.append(f"🆔 <code>{row['chat_id']}</code>  📅 {row['added_at'][:10]}")
    lines.append(
        "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "➕ /adduser CHAT_ID\n"
        "➖ /removeuser CHAT_ID"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /adduser CHAT_ID — grant a user access."""
    if not is_admin(update):
        return
    try:
        chat_id   = int(context.args[0])
        added_by  = update.effective_user.username or str(update.effective_user.id)
        loop      = asyncio.get_running_loop()
        await loop.run_in_executor(None, _db_whitelist_add, chat_id, added_by)
        _WHITELIST.add(chat_id)
        await update.message.reply_text(
            f"✅ <b>Access Granted</b>\n\n"
            f"🆔 <code>{chat_id}</code> has been added to the whitelist.",
            parse_mode=ParseMode.HTML,
        )
        await safe_send(
            context.bot, chat_id,
            "✅ <b>Access Granted!</b>\n\n"
            "You now have access to Fayda ID Bot.\n"
            "Tap /start to begin! 🇪🇹",
            parse_mode=ParseMode.HTML,
        )
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /adduser CHAT_ID")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /removeuser CHAT_ID — revoke a user's access."""
    if not is_admin(update):
        return
    try:
        chat_id = int(context.args[0])
        loop    = asyncio.get_running_loop()
        await loop.run_in_executor(None, _db_whitelist_remove, chat_id)
        _WHITELIST.discard(chat_id)
        await update.message.reply_text(
            f"🚫 <b>Access Revoked</b>\n\n"
            f"🆔 <code>{chat_id}</code> has been removed from the whitelist.",
            parse_mode=ParseMode.HTML,
        )
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /removeuser CHAT_ID")


# ═══════════════════════════════════════════════════════════════════════
#  🔧  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════
async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        chat_id = int(context.args[0])
        amount  = int(context.args[1])
        loop    = asyncio.get_running_loop()
        user    = await loop.run_in_executor(None, _db_get_user, chat_id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return
        await loop.run_in_executor(None, _db_add_wallet, chat_id, amount)
        new_bal = user["wallet"] + amount
        await update.message.reply_text(
            f"✅ Added {amount} ETB → {user['full_name']}\nBalance: {new_bal} ETB"
        )
        await safe_send(
            context.bot, chat_id,
            f"💳 Admin credited <b>{amount} ETB</b>!\nNew balance: <b>{new_bal} ETB</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await update.message.reply_text("Usage: /addwallet CHAT_ID AMOUNT")


# ═══════════════════════════════════════════════════════════════════════
#  ❌  CANCEL
# ═══════════════════════════════════════════════════════════════════════
async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=kb)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _cancel(update, context)


# ═══════════════════════════════════════════════════════════════════════
#  🔀  MESSAGE ROUTER
# ═══════════════════════════════════════════════════════════════════════
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not await _check_access(update, context):
        return
    u    = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    text = update.message.text.strip()

    routes = {
        "💳 My Wallet":          show_wallet,
        "🎁 Invite & Earn":      show_invite,
        "🎨 Choose Template":    show_template_menu,
        "📋 Pending Payments":   admin_pending,
        "👥 Users":              admin_users,
        "📈 Stats":              admin_stats,
        "🔒 Whitelist":          admin_whitelist_view,
    }

    handler = routes.get(text)
    if handler:
        if text in ("📋 Pending Payments", "👥 Users", "📈 Stats", "🔒 Whitelist") and not is_admin(update):
            pass
        else:
            await handler(update, context)
            return

    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text(
        "👇 Please use the menu buttons below.", reply_markup=kb
    )


# ═══════════════════════════════════════════════════════════════════════
#  🛡️  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, TelegramError):
        msg = str(err).lower()
        if any(s in msg for s in (
            "forbidden", "bot was blocked", "chat not found",
            "user is deactivated", "message is not modified",
            "query is too old", "message to edit not found",
        )):
            log.debug(f"Ignored TelegramError: {err}")
            return
    log.error("Unhandled exception:", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ <b>Something went wrong.</b>\n\nPlease try again or use /start.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  🚀  BUILD & RUN
# ═══════════════════════════════════════════════════════════════════════
def build_app():
    init_db()
    _WHITELIST.clear()
    _WHITELIST.update(r["chat_id"] for r in _db_whitelist_all())

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    async def _post_init(app):
        global _BOT_USERNAME
        asyncio.get_running_loop().set_default_executor(_IO_POOL)
        await app.bot.delete_webhook(drop_pending_updates=True)
        _BOT_USERNAME = (await app.bot.get_me()).username or ""
        log.info("  ✅ Webhook cleared — starting polling…")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .concurrent_updates(True)
        .connection_pool_size(64)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    # OTP generation flow (eSignet)
    gen_conv = ConversationHandler(
        entry_points=[
            # Manual FAN entry (existing flow)
            MessageHandler(filters.Regex(r"^🪪 Send OTP$"), start_generate),
            # QR-scan flow: user sends a photo → FAN auto-extracted → OTP sent
            MessageHandler(filters.PHOTO & ~filters.COMMAND, recv_qr_photo),
        ],
        states={
            WAIT_FAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_fan)],
            WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
        conversation_timeout=300,
    )

    # Top-up payment flow
    topup_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^💰 Top Up$"), start_topup)
        ],
        states={
            WAIT_SCREENSHOT: [
                MessageHandler(filters.PHOTO, recv_screenshot),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_screenshot),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
        conversation_timeout=180,
    )

    # Admin wallet editor
    edit_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^✏️ Edit Wallet$"), start_edit_wallet)
        ],
        states={
            WAIT_EDIT_CREDITS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_edit_wallet)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
    )

    # Admin broadcast
    broadcast_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^📣 Broadcast$"), start_broadcast)
        ],
        states={
            WAIT_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_broadcast)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
    )

    # A4 layout converter
    a4_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🖨️ A4 Converter$"), start_a4_convert)
        ],
        states={
            WAIT_A4_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, recv_a4_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_a4_image),
            ],
            WAIT_A4_FLIP: [],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
        conversation_timeout=600,
    )

    application.add_handler(CommandHandler("start",      cmd_start))
    application.add_handler(CommandHandler("cancel",     cmd_cancel))
    application.add_handler(CommandHandler("addwallet",  cmd_addwallet))
    application.add_handler(CommandHandler("adduser",    cmd_adduser))
    application.add_handler(CommandHandler("removeuser", cmd_removeuser))

    application.add_handler(gen_conv)
    application.add_handler(topup_conv)
    application.add_handler(edit_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(a4_conv)

    application.add_handler(
        CallbackQueryHandler(cb_template_select,      pattern=r"^tpl_select:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_template_preview_menu, pattern=r"^tpl_preview_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_template_back,         pattern=r"^tpl_back$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_payment,               pattern=r"^pay_(ok|no):\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_a4_flip,               pattern=r"^a4_flip_(yes|no)$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_cancel_flow,           pattern=r"^cancel_flow$")
    )

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, route_message)
    )
    application.add_error_handler(error_handler)

    return application


def _enable_fast_loop():
    """uvloop is a drop-in asyncio loop, typically 2-4x faster for network I/O."""
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass


def main():
    _enable_fast_loop()
    log.info("╔═══════════════════════════════════════════╗")
    log.info("║  🇪🇹  Fayda ID Bot — eSignet Edition      ║")
    log.info("╚═══════════════════════════════════════════╝")
    log.info(f"  👑 Admin:    @{ADMIN_USERNAME}")
    log.info(f"  📱 Payment:  {PAYMENT_PHONE}")
    log.info(f"  💵 Price:    {PRICE_OTP} ETB / OTP  |  Trials: {FREE_TRIALS}")
    log.info(f"  🔐 OTP via:  auth.fayda.et (official eSignet API)")
    log.info(f"  🎨 Templates: {len(TEMPLATES)} loaded")

    from telegram.error import Conflict as _Conflict

    for attempt in range(1, 6):
        try:
            app = build_app()
            # PTB 21.x: run_polling manages its own event loop internally.
            # delete_webhook is handled via post_init hook below.
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            break
        except _Conflict as e:
            wait = attempt * 5
            log.warning(f"  ⚠️ Conflict attempt {attempt}/5 — retry in {wait}s: {e}")
            time.sleep(wait)
    else:
        log.error("  ❌ Could not start after 5 attempts.")


if __name__ == "__main__":
    main()
