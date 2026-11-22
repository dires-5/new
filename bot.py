"""
╔══════════════════════════════════════════════════════════╗
║        FAYDA UNIFIED MEGA BOT  ·  bot.py                ║
║   OTP-1 eSignet · OTP-2 Card-Order · OTP-3 TelePrint    ║
║   Public · Ultra-fast · Colourful · Railway-ready       ║
╚══════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import random
import re
import sqlite3
import string
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── optional OpenCV for QR scan ──────────────────────────────────────────────
try:
    import cv2
    import numpy as np
    CV2_OK = True
except ImportError:
    CV2_OK = False

# ── app card-renderer ─────────────────────────────────────────────────────────
from app import (
    TEMPLATES,
    get_template_sample,
    merge_to_template_all_outputs,
    safe_b64decode,
)

# ── eSignet client ────────────────────────────────────────────────────────────
from esignet import EsignetClient

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("faydabot")

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DB_PATH     = os.getenv("DB_PATH", "fayda_bot.db")
REMOVEBG_KEY= os.getenv("REMOVEBG_API_KEY", "")
FREE_TRIALS = int(os.getenv("FREE_TRIALS", "1"))
CARD_COST   = float(os.getenv("CARD_COST", "5.0"))
REFERRAL_BONUS = float(os.getenv("REFERRAL_BONUS", "10.0"))
TOPUP_ACCOUNT  = os.getenv("TOPUP_ACCOUNT", "0912345678 (TeleBirr)")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
BOT_USERNAME     = os.getenv("BOT_USERNAME", "")

# ── Post / Card-Order API ─────────────────────────────────────────────────────
POST_BASE        = "https://card-order.fayda.et"
POST_NEXT_ACTION = "707d17603b46fdbaeb88f738d9882412097f92c7be"

# ── TelePrint API ─────────────────────────────────────────────────────────────
TELE_BASE        = "https://teleprint.fayda.et/home"
TELE_NEXT_ACTION = "70a3ccf5964dca07f7d3a397fa6925fe7b6c2b9c82"

# ── A4 layout constants ───────────────────────────────────────────────────────
A4_W, A4_H = 2480, 3508
CARD_W, CARD_H = 2191, 667
MARGIN = 60
CROP_COLOR = (200, 200, 200)

EXECUTOR = ThreadPoolExecutor(max_workers=8)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONVERSATION STATES
# ═══════════════════════════════════════════════════════════════════════════════
# Each flow gets its own range so handlers never collide inside one application.

# ── eSignet flow  (00–09) ─────────────────────────────────────────────────────
(
    ESIG_WAIT_FAN,
    ESIG_WAIT_OTP,
    ESIG_WAIT_TEMPLATE,
    ESIG_WAIT_QR,
) = range(0, 4)

# ── Card-Order / Post flow  (10–19) ──────────────────────────────────────────
(
    POST_WAIT_ID,
    POST_WAIT_OTP,
    POST_WAIT_TEMPLATE,
) = range(10, 13)

# ── TelePrint flow  (20–29) ───────────────────────────────────────────────────
(
    TELE_WAIT_FAN,
    TELE_WAIT_OTP,
    TELE_WAIT_TEMPLATE,
) = range(20, 23)

# ── Shared / utility flows  (30–49) ──────────────────────────────────────────
(
    WAIT_SCREENSHOT,
    WAIT_EDIT_CREDITS,
    WAIT_BROADCAST,
    WAIT_A4_IMAGES,
    WAIT_A4_FLIP,
    WAIT_PDF_UPLOAD,
    WAIT_PDF_CONFIRM,
) = range(30, 37)

# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            credits    REAL    DEFAULT 0,
            free_left  INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by   INTEGER,
            joined_at  TEXT    DEFAULT (datetime('now')),
            is_banned  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            amount      REAL,
            screenshot  TEXT,
            status      TEXT DEFAULT 'pending',
            reviewed_by INTEGER,
            created_at  TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS generations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            flow       TEXT,
            fan        TEXT,
            template   TEXT,
            cost       REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS global_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)

def ensure_user(user_id: int, username: str = "", full_name: str = "") -> None:
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    with db() as c:
        c.execute("""
            INSERT OR IGNORE INTO users (user_id, username, full_name, free_left, referral_code)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, full_name, FREE_TRIALS, code))
        c.execute("""
            UPDATE users SET username=?, full_name=?
            WHERE user_id=?
        """, (username, full_name, user_id))

def get_user(user_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def add_credits(user_id: int, amount: float) -> None:
    with db() as c:
        c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (amount, user_id))

def deduct_credits(user_id: int) -> bool:
    """Returns True if deducted (free or paid). False if insufficient."""
    with db() as c:
        u = c.execute("SELECT free_left, credits FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not u:
            return False
        if u["free_left"] > 0:
            c.execute("UPDATE users SET free_left = free_left - 1 WHERE user_id=?", (user_id,))
            return True
        if u["credits"] >= CARD_COST:
            c.execute("UPDATE users SET credits = credits - ? WHERE user_id=?", (CARD_COST, user_id))
            return True
        return False

def log_generation(user_id: int, flow: str, fan: str, template: str, cost: float = 0.0):
    with db() as c:
        c.execute(
            "INSERT INTO generations (user_id, flow, fan, template, cost) VALUES (?,?,?,?,?)",
            (user_id, flow, fan, template, cost),
        )

def get_global(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_global(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?,?)", (key, value))

# ═══════════════════════════════════════════════════════════════════════════════
#  TELEPRINT CLIENT  (from teleprint_bot.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TelePrintClient:
    _RSC_ROW = re.compile(r'^\d+:(\{.*\}|\[.*\])$')
    _TOKEN_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')
    _IDENTITY_RE = re.compile(r'"identity_json"\s*:\s*"([^"]+)"')

    def __init__(self):
        self.session = requests.Session()
        self.fan: str = ""
        self._last_raw: dict = {}

    def _headers(self) -> dict:
        return {
            "Next-Action": TELE_NEXT_ACTION,
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://teleprint.fayda.et",
            "Referer": "https://teleprint.fayda.et/home",
            "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36",
        }

    def _parse_rsc(self, text: str) -> dict:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = self._RSC_ROW.match(line)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    pass
        # fallback: find JSON blocks
        for chunk in re.findall(r'\{[^{}]{10,}\}', text):
            try:
                return json.loads(chunk)
            except Exception:
                pass
        return {}

    def get_token(self, fan: str) -> dict:
        self.fan = fan
        payload = json.dumps([{"request": {"fcn": fan, "requestedFields": ["phone"]}},
                               "/otpService/getToken", True])
        r = self.session.post(TELE_BASE, data=payload, headers=self._headers(), timeout=20)
        r.raise_for_status()
        parsed = self._parse_rsc(r.text)
        self._last_raw = parsed
        return parsed

    def validate_token(self, otp: str) -> dict:
        payload = json.dumps([{"request": {"token": otp, "fcn": self.fan,
                                            "requestedFields": ["fullName","dateOfBirth","gender",
                                                                 "phone","uin","photo"]}},
                               "/otpService/validateToken", True])
        r = self.session.post(TELE_BASE, data=payload, headers=self._headers(), timeout=20)
        r.raise_for_status()
        return self._parse_rsc(r.text)

    @staticmethod
    def parse_identity(raw: dict, fan: str) -> dict:
        """Normalize TelePrint identity to the dict merge_to_template expects."""
        name_raw = raw.get("fullName", raw.get("full_name", ""))
        if isinstance(name_raw, list):
            name_en = next((n.get("value", "") for n in name_raw
                            if n.get("language", "").lower() in ("eng", "en")), "")
        else:
            name_en = str(name_raw)
        return {
            "full_name": name_en or fan,
            "dob": raw.get("dateOfBirth", raw.get("dob", "")),
            "gender": raw.get("gender", ""),
            "phone": raw.get("phone", ""),
            "uin": raw.get("uin", fan),
            "photo": raw.get("photo", ""),
            "fan": fan,
        }

# ═══════════════════════════════════════════════════════════════════════════════
#  POST / CARD-ORDER API  (from post_bot.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _post_headers() -> dict:
    return {
        "Next-Action": POST_NEXT_ACTION,
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": POST_BASE,
        "Referer": POST_BASE + "/",
        "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36",
    }

def _post_api_send_otp(fan: str) -> str:
    """Send OTP via card-order.fayda.et. Returns transactionId."""
    payload = json.dumps([{"request": {"fcn": fan}}, "/otpService/getToken", True])
    r = requests.post(POST_BASE + "/", data=payload, headers=_post_headers(), timeout=20)
    r.raise_for_status()
    # Parse RSC stream
    for line in r.text.splitlines():
        line = line.strip()
        m = re.match(r'^\d+:(\{.*\})$', line)
        if m:
            try:
                obj = json.loads(m.group(1))
                if "transactionId" in obj:
                    return obj["transactionId"]
                if "transaction_id" in obj:
                    return obj["transaction_id"]
            except Exception:
                pass
    # fallback
    m2 = re.search(r'"transactionId"\s*:\s*"([^"]+)"', r.text)
    if m2:
        return m2.group(1)
    raise ValueError("No transactionId in POST OTP response")

def _post_api_validate_otp(otp: str, fan: str, transaction_id: str) -> dict:
    """Validate OTP via card-order.fayda.et. Returns identity dict."""
    payload = json.dumps([{"request": {"token": otp, "fcn": fan, "transactionId": transaction_id,
                                        "requestedFields": ["fullName","dateOfBirth","gender",
                                                             "phone","uin","photo"]}},
                           "/otpService/validateToken", True])
    r = requests.post(POST_BASE + "/", data=payload, headers=_post_headers(), timeout=20)
    r.raise_for_status()
    for line in r.text.splitlines():
        line = line.strip()
        m = re.match(r'^\d+:(\{.*\})$', line)
        if m:
            try:
                obj = json.loads(m.group(1))
                if any(k in obj for k in ("fullName", "full_name", "uin")):
                    return obj
            except Exception:
                pass
    raise ValueError("Could not parse identity from POST validate response")

def _post_parse_identity(raw: dict, fan: str) -> dict:
    name_raw = raw.get("fullName", raw.get("full_name", ""))
    if isinstance(name_raw, list):
        name_en = next((n.get("value","") for n in name_raw
                        if n.get("language","").lower() in ("eng","en")), "")
    else:
        name_en = str(name_raw)
    return {
        "full_name": name_en or fan,
        "dob": raw.get("dateOfBirth", raw.get("dob", "")),
        "gender": raw.get("gender", ""),
        "phone": raw.get("phone", ""),
        "uin": raw.get("uin", fan),
        "photo": raw.get("photo", ""),
        "fan": fan,
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  KEYBOARD HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def main_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        ["🔵 OTP 1 — eSignet",    "🟠 OTP 2 — Card-Order"],
        ["🟣 OTP 3 — TelePrint",  "🎨 Choose Template"],
        ["🖨️ A4 Converter",       "💳 My Wallet"],
        ["💰 Top Up",              "🎁 Invite & Earn"],
        ["📞 Support"],
    ]
    if is_admin:
        rows += [
            ["📋 Pending Payments",  "📈 Statistics"],
            ["👥 Users",             "✏️ Edit Wallet"],
            ["📣 Broadcast",         "⚙️ Settings"],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def template_kb() -> InlineKeyboardMarkup:
    keys = list(TEMPLATES.keys())
    buttons = [
        [InlineKeyboardButton(f"🎴 {k}", callback_data=f"tpl:{k}")]
        for k in keys
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="tpl:cancel")])
    return InlineKeyboardMarkup(buttons)

def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def run_sync(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(EXECUTOR, fn, *args)

def _build_card_bytes(identity: dict, template_key: str) -> bytes:
    outputs = merge_to_template_all_outputs(identity, template_key)
    # Return the first (front) card as JPEG bytes
    img = outputs[0]
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()

def _build_a4_bytes(card_images, flip: bool = False) -> bytes:
    """Arrange cards on A4 canvas with crop marks."""
    try:
        from PIL import Image as PILImage, ImageDraw
    except ImportError:
        raise RuntimeError("Pillow not installed")

    a4 = PILImage.new("RGB", (A4_W, A4_H), (255, 255, 255))
    draw = ImageDraw.Draw(a4)
    y = MARGIN
    for img_bytes in card_images:
        card = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
        card = card.resize((CARD_W, CARD_H), PILImage.LANCZOS)
        if flip:
            card = card.rotate(180)
        x = (A4_W - CARD_W) // 2
        a4.paste(card, (x, y))
        # crop marks
        for cx in [x, x + CARD_W]:
            draw.line([(cx, y - 20), (cx, y + CARD_H + 20)], fill=CROP_COLOR, width=2)
        for cy in [y, y + CARD_H]:
            draw.line([(x - 20, cy), (x + CARD_W + 20, cy)], fill=CROP_COLOR, width=2)
        y += CARD_H + MARGIN
    buf = io.BytesIO()
    a4.save(buf, format="JPEG", quality=92)
    return buf.getvalue()

async def send_card(update: Update, identity: dict, template_key: str, flow: str):
    uid = update.effective_user.id
    if not deduct_credits(uid):
        await update.message.reply_text(
            "❌ Insufficient credits!\n\n"
            f"Each card costs *{CARD_COST} ETB*.\n"
            "Use 💰 *Top Up* to add credits.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    msg = await update.message.reply_text("⚡ Generating your card…")
    try:
        card_bytes = await run_sync(_build_card_bytes, identity, template_key)
        log_generation(uid, flow, identity.get("fan", ""), template_key, CARD_COST)
        await update.message.reply_photo(
            photo=io.BytesIO(card_bytes),
            caption=(
                f"✅ *Fayda ID Card — {template_key}*\n"
                f"👤 {identity.get('full_name','')}\n"
                f"📋 FAN: `{identity.get('fan','')}`\n"
                f"🌊 Flow: {flow}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await msg.delete()
        return True
    except Exception as e:
        await msg.edit_text(f"❌ Card generation failed:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return False

# ═══════════════════════════════════════════════════════════════════════════════
#  /start  &  MAIN MENU
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx) -> None:
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.full_name or "")

    # Referral processing
    args = ctx.args
    if args and args[0].startswith("ref_"):
        ref_code = args[0][4:]
        me = get_user(u.id)
        if me and not me["referred_by"]:
            with db() as c:
                referrer = c.execute(
                    "SELECT user_id FROM users WHERE referral_code=?", (ref_code,)
                ).fetchone()
                if referrer and referrer["user_id"] != u.id:
                    c.execute(
                        "UPDATE users SET referred_by=? WHERE user_id=?",
                        (referrer["user_id"], u.id),
                    )

    is_admin = _is_admin(u.id)
    text = (
        "╔══════════════════════════════╗\n"
        "║  🇪🇹  FAYDA ID CARD BOT  🇪🇹  ║\n"
        "╚══════════════════════════════╝\n\n"
        "Welcome!  Choose an OTP flow to generate your\n"
        "🎴 *Fayda Digital ID Card* instantly.\n\n"
        "🔵 *OTP 1* — eSignet  _(auth.fayda.et)_\n"
        "🟠 *OTP 2* — Card-Order  _(card-order.fayda.et)_\n"
        "🟣 *OTP 3* — TelePrint  _(teleprint.fayda.et)_\n\n"
        f"🆓 Free trials remaining: *{FREE_TRIALS}*\n"
        f"💰 Card cost: *{CARD_COST} ETB* per generation"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(is_admin),
    )

async def cmd_help(update: Update, ctx) -> None:
    await update.message.reply_text(
        "🆘 *Help*\n\n"
        "• Pick an OTP button to start generating.\n"
        "• Enter your *14-digit FAN* (Fayda Account Number).\n"
        "• You'll receive an OTP on your registered phone.\n"
        "• Enter the OTP → pick a template → get your card!\n\n"
        "Need support? Contact @" + SUPPORT_USERNAME,
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  OTP 1  —  eSIGNET FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def esig_start(update: Update, ctx) -> int:
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.full_name or "")
    ctx.user_data["esig_client"] = EsignetClient()
    await update.message.reply_text(
        "🔵 *eSignet OTP Flow*\n\n"
        "Enter your *14-digit Fayda Account Number (FAN)*:\n\n"
        "_Or send a QR code image to scan_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return ESIG_WAIT_FAN

async def esig_receive_fan(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    # QR image?
    if update.message.photo and CV2_OK:
        return await esig_scan_qr(update, ctx)

    fan = re.sub(r"\D", "", text)
    if len(fan) != 14:
        await update.message.reply_text("❌ FAN must be exactly 14 digits. Try again:")
        return ESIG_WAIT_FAN

    client: EsignetClient = ctx.user_data["esig_client"]
    msg = await update.message.reply_text("⏳ Initialising eSignet…")
    try:
        await run_sync(client.initialize, fan)
        await run_sync(client.get_oauth_details)
        await run_sync(client.send_otp)
        ctx.user_data["esig_fan"] = fan
        await msg.edit_text(
            f"✅ OTP sent to your registered phone!\n\n"
            f"FAN: `{fan}`\n\n"
            "🔢 Enter the *6-digit OTP*:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ESIG_WAIT_OTP
    except Exception as e:
        await msg.edit_text(f"❌ eSignet error:\n`{e}`\n\nSend FAN again or /cancel",
                            parse_mode=ParseMode.MARKDOWN)
        return ESIG_WAIT_FAN

async def esig_scan_qr(update: Update, ctx) -> int:
    if not CV2_OK:
        await update.message.reply_text("❌ QR scan not available on this server.")
        return ESIG_WAIT_FAN
    msg = await update.message.reply_text("🔍 Scanning QR code…")
    try:
        file = await update.message.photo[-1].get_file()
        data = await file.download_as_bytearray()
        arr = np.frombuffer(bytes(data), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        detector = cv2.QRCodeDetector()
        val, _, _ = detector.detectAndDecode(img)
        if not val:
            await msg.edit_text("❌ No QR code found. Send FAN as text.")
            return ESIG_WAIT_FAN
        # Try to extract FAN from decoded value
        fan = re.sub(r"\D", "", val)[-14:] if re.sub(r"\D", "", val) else ""
        if len(fan) != 14:
            await msg.edit_text(f"ℹ️ QR decoded: `{val[:60]}`\n\nPlease enter FAN manually:",
                                parse_mode=ParseMode.MARKDOWN)
            return ESIG_WAIT_FAN
        await msg.edit_text(f"✅ FAN extracted: `{fan}`\nInitialising eSignet…",
                            parse_mode=ParseMode.MARKDOWN)
        # Reuse FAN logic
        fake_msg = type("M", (), {"text": fan})()
        update.message.text = fan
        return await esig_receive_fan(update, ctx)
    except Exception as e:
        await msg.edit_text(f"❌ QR scan failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return ESIG_WAIT_FAN

async def esig_receive_otp(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    otp = re.sub(r"\D", "", text)
    if len(otp) not in (6, 8):
        await update.message.reply_text("❌ OTP must be 6 or 8 digits. Try again:")
        return ESIG_WAIT_OTP

    client: EsignetClient = ctx.user_data["esig_client"]
    fan: str = ctx.user_data["esig_fan"]
    msg = await update.message.reply_text("⚡ Verifying OTP…")
    try:
        await run_sync(client.verify_otp, otp)
        code = await run_sync(client.get_auth_code)
        await run_sync(client.exchange_auth_code, code)
        raw = await run_sync(client.get_identity)
        identity = client.parse_identity(raw, fan)
        ctx.user_data["esig_identity"] = identity
        await msg.edit_text(
            f"✅ Identity verified!\n\n"
            f"👤 *{identity['full_name']}*\n"
            f"📋 FAN: `{fan}`\n\n"
            "🎨 Choose a card template:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text("Pick template:", reply_markup=template_kb())
        return ESIG_WAIT_TEMPLATE
    except Exception as e:
        await msg.edit_text(
            f"❌ OTP verification failed:\n`{e}`\n\nSend OTP again or /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ESIG_WAIT_OTP

async def esig_receive_template(update: Update, ctx) -> int:
    query = update.callback_query
    await query.answer()
    tpl = query.data.split(":", 1)[1]
    if tpl == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    identity = ctx.user_data.get("esig_identity", {})
    # Delegate to shared send_card
    # We need a fake message-compatible update — use query.message
    update._effective_message = query.message
    await query.edit_message_text(f"⚡ Generating *{tpl}* card…", parse_mode=ParseMode.MARKDOWN)
    success = await send_card(update, identity, tpl, "eSignet")
    if success:
        await query.message.reply_text(
            "🎉 Card generated!\n\nBack to main menu 👇",
            reply_markup=main_menu_kb(_is_admin(update.effective_user.id)),
        )
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  OTP 2  —  CARD-ORDER (POST) FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def post_start(update: Update, ctx) -> int:
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.full_name or "")
    await update.message.reply_text(
        "🟠 *Card-Order OTP Flow*\n\n"
        "Enter your *14-digit Fayda Account Number (FAN)*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return POST_WAIT_ID

async def post_receive_id(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    fan = re.sub(r"\D", "", text)
    if len(fan) != 14:
        await update.message.reply_text("❌ FAN must be exactly 14 digits. Try again:")
        return POST_WAIT_ID

    msg = await update.message.reply_text("⏳ Sending OTP via Card-Order API…")
    try:
        txn_id = await run_sync(_post_api_send_otp, fan)
        ctx.user_data["post_fan"] = fan
        ctx.user_data["post_txn_id"] = txn_id
        await msg.edit_text(
            f"✅ OTP sent!\n\nFAN: `{fan}`\n\n🔢 Enter the *6-digit OTP*:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return POST_WAIT_OTP
    except Exception as e:
        await msg.edit_text(
            f"❌ API error:\n`{e}`\n\nSend FAN again or /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return POST_WAIT_ID

async def post_receive_otp(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    otp = re.sub(r"\D", "", text)
    if len(otp) < 4:
        await update.message.reply_text("❌ Invalid OTP. Try again:")
        return POST_WAIT_OTP

    fan = ctx.user_data["post_fan"]
    txn_id = ctx.user_data["post_txn_id"]
    msg = await update.message.reply_text("⚡ Validating OTP…")
    try:
        raw = await run_sync(_post_api_validate_otp, otp, fan, txn_id)
        identity = _post_parse_identity(raw, fan)
        ctx.user_data["post_identity"] = identity
        await msg.edit_text(
            f"✅ Verified!\n\n👤 *{identity['full_name']}*\n📋 FAN: `{fan}`\n\n🎨 Choose template:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text("Pick template:", reply_markup=template_kb())
        return POST_WAIT_TEMPLATE
    except Exception as e:
        await msg.edit_text(
            f"❌ OTP validation failed:\n`{e}`\n\nTry again or /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return POST_WAIT_OTP

async def post_receive_template(update: Update, ctx) -> int:
    query = update.callback_query
    await query.answer()
    tpl = query.data.split(":", 1)[1]
    if tpl == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    identity = ctx.user_data.get("post_identity", {})
    update._effective_message = query.message
    await query.edit_message_text(f"⚡ Generating *{tpl}* card…", parse_mode=ParseMode.MARKDOWN)
    success = await send_card(update, identity, tpl, "Card-Order")
    if success:
        await query.message.reply_text(
            "🎉 Card generated!\n\nBack to main menu 👇",
            reply_markup=main_menu_kb(_is_admin(update.effective_user.id)),
        )
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  OTP 3  —  TELEPRINT FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def tele_start(update: Update, ctx) -> int:
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.full_name or "")
    ctx.user_data["tele_client"] = TelePrintClient()
    await update.message.reply_text(
        "🟣 *TelePrint OTP Flow*\n\n"
        "Enter your *14-digit Fayda Account Number (FAN)*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return TELE_WAIT_FAN

async def tele_receive_fan(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    fan = re.sub(r"\D", "", text)
    if len(fan) != 14:
        await update.message.reply_text("❌ FAN must be exactly 14 digits. Try again:")
        return TELE_WAIT_FAN

    client: TelePrintClient = ctx.user_data["tele_client"]
    msg = await update.message.reply_text("⏳ Sending OTP via TelePrint…")
    try:
        await run_sync(client.get_token, fan)
        ctx.user_data["tele_fan"] = fan
        await msg.edit_text(
            f"✅ OTP sent!\n\nFAN: `{fan}`\n\n🔢 Enter the *6-digit OTP*:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return TELE_WAIT_OTP
    except Exception as e:
        await msg.edit_text(
            f"❌ TelePrint error:\n`{e}`\n\nSend FAN again or /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return TELE_WAIT_FAN

async def tele_receive_otp(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    otp = re.sub(r"\D", "", text)
    if len(otp) < 4:
        await update.message.reply_text("❌ Invalid OTP. Try again:")
        return TELE_WAIT_OTP

    client: TelePrintClient = ctx.user_data["tele_client"]
    fan: str = ctx.user_data["tele_fan"]
    msg = await update.message.reply_text("⚡ Validating OTP…")
    try:
        raw = await run_sync(client.validate_token, otp)
        identity = TelePrintClient.parse_identity(raw, fan)
        ctx.user_data["tele_identity"] = identity
        await msg.edit_text(
            f"✅ Verified!\n\n👤 *{identity['full_name']}*\n📋 FAN: `{fan}`\n\n🎨 Choose template:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text("Pick template:", reply_markup=template_kb())
        return TELE_WAIT_TEMPLATE
    except Exception as e:
        await msg.edit_text(
            f"❌ OTP validation failed:\n`{e}`\n\nTry again or /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return TELE_WAIT_OTP

async def tele_receive_template(update: Update, ctx) -> int:
    query = update.callback_query
    await query.answer()
    tpl = query.data.split(":", 1)[1]
    if tpl == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    identity = ctx.user_data.get("tele_identity", {})
    update._effective_message = query.message
    await query.edit_message_text(f"⚡ Generating *{tpl}* card…", parse_mode=ParseMode.MARKDOWN)
    success = await send_card(update, identity, tpl, "TelePrint")
    if success:
        await query.message.reply_text(
            "🎉 Card generated!\n\nBack to main menu 👇",
            reply_markup=main_menu_kb(_is_admin(update.effective_user.id)),
        )
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  TEMPLATES BROWSER
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_templates(update: Update, ctx) -> None:
    keys = list(TEMPLATES.keys())
    await update.message.reply_text(
        "🎨 *Available Templates*\n\n" +
        "\n".join(f"• *{k}*" for k in keys) +
        "\n\nUse any OTP flow, then pick your preferred template.",
        parse_mode=ParseMode.MARKDOWN,
    )
    # Send sample previews
    for k in keys:
        try:
            sample_bytes = await run_sync(lambda key=k: _build_card_bytes(
                {"full_name": "አቤቤ ተስፋዬ", "dob": "1990-01-01", "gender": "Male",
                 "phone": "+251911000000", "uin": "12345678901234",
                 "photo": "", "fan": "12345678901234"}, key
            ))
            await update.message.reply_photo(
                photo=io.BytesIO(sample_bytes),
                caption=f"🎴 *{k}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
#  A4 CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_a4_start(update: Update, ctx) -> int:
    ctx.user_data["a4_images"] = []
    await update.message.reply_text(
        "🖨️ *A4 PVC Converter*\n\n"
        "Send card images one by one (up to 4), then type *done*.\n"
        "I'll arrange them on A4 300 DPI with crop marks.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup([["✅ Done", "❌ Cancel"]], resize_keyboard=True),
    )
    return WAIT_A4_IMAGES

async def handle_a4_image(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)
    if text == "✅ Done":
        images = ctx.user_data.get("a4_images", [])
        if not images:
            await update.message.reply_text("❌ No images received.")
            return ConversationHandler.END
        await update.message.reply_text(
            "🔄 Flip cards 180°?",
            reply_markup=ReplyKeyboardMarkup([["↩️ Yes, flip", "➡️ No, keep normal"]], resize_keyboard=True),
        )
        return WAIT_A4_FLIP

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        data = await file.download_as_bytearray()
        ctx.user_data["a4_images"].append(bytes(data))
        count = len(ctx.user_data["a4_images"])
        await update.message.reply_text(f"✅ Card {count} received. Send more or tap *Done*.",
                                        parse_mode=ParseMode.MARKDOWN)
        if count >= 4:
            await update.message.reply_text(
                "Maximum 4 cards reached.",
                reply_markup=ReplyKeyboardMarkup([["✅ Done", "❌ Cancel"]], resize_keyboard=True),
            )
    return WAIT_A4_IMAGES

async def handle_a4_flip(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    flip = text.startswith("↩️")
    images = ctx.user_data.get("a4_images", [])
    msg = await update.message.reply_text("🖨️ Building A4 layout…")
    try:
        a4_bytes = await run_sync(_build_a4_bytes, images, flip)
        await update.message.reply_photo(
            photo=io.BytesIO(a4_bytes),
            caption="🖨️ *A4 PVC Layout — 300 DPI*\nReady for professional printing!",
            parse_mode=ParseMode.MARKDOWN,
        )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ A4 error: `{e}`", parse_mode=ParseMode.MARKDOWN)
    is_admin = _is_admin(update.effective_user.id)
    await update.message.reply_text("Back to menu 👇", reply_markup=main_menu_kb(is_admin))
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  WALLET
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_wallet(update: Update, ctx) -> None:
    u = update.effective_user
    ensure_user(u.id)
    user = get_user(u.id)
    with db() as c:
        total_gen = c.execute(
            "SELECT COUNT(*) as n FROM generations WHERE user_id=?", (u.id,)
        ).fetchone()["n"]
    await update.message.reply_text(
        f"💳 *My Wallet*\n\n"
        f"💰 Credits: *{user['credits']:.2f} ETB*\n"
        f"🆓 Free uses left: *{user['free_left']}*\n"
        f"🎴 Total cards generated: *{total_gen}*\n\n"
        f"Each card costs *{CARD_COST} ETB*",
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  TOP-UP FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_topup_start(update: Update, ctx) -> int:
    await update.message.reply_text(
        "💰 *Top Up Your Wallet*\n\n"
        f"Send payment to:\n*{TOPUP_ACCOUNT}*\n\n"
        "Then send a *screenshot* of your payment receipt here:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return WAIT_SCREENSHOT

async def handle_topup_screenshot(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)

    if not update.message.photo:
        await update.message.reply_text("❌ Please send a photo/screenshot.")
        return WAIT_SCREENSHOT

    file = await update.message.photo[-1].get_file()
    file_id = update.message.photo[-1].file_id
    uid = update.effective_user.id

    with db() as c:
        c.execute(
            "INSERT INTO payments (user_id, amount, screenshot, status) VALUES (?,?,?,?)",
            (uid, 0, file_id, "pending"),
        )
        payment_id = c.execute("SELECT last_insert_rowid() as id").fetchone()["id"]

    await update.message.reply_text(
        "✅ Screenshot received! Awaiting admin review.\n"
        "You'll be notified once approved.",
        reply_markup=main_menu_kb(_is_admin(uid)),
    )

    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_photo(
                chat_id=admin_id,
                photo=file_id,
                caption=(
                    f"💰 *New Top-Up Request*\n"
                    f"User: `{uid}` (@{update.effective_user.username or '—'})\n"
                    f"Payment ID: `{payment_id}`\n\n"
                    f"Use /approve {payment_id} <amount> to approve."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  REFERRAL
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_referral(update: Update, ctx) -> None:
    u = update.effective_user
    ensure_user(u.id)
    user = get_user(u.id)
    code = user["referral_code"] if user else "N/A"
    bot_name = BOT_USERNAME or (await ctx.bot.get_me()).username
    link = f"https://t.me/{bot_name}?start=ref_{code}"
    with db() as c:
        count = c.execute(
            "SELECT COUNT(*) as n FROM users WHERE referred_by=?", (u.id,)
        ).fetchone()["n"]
    await update.message.reply_text(
        f"🎁 *Invite & Earn*\n\n"
        f"Your referral link:\n`{link}`\n\n"
        f"👥 People referred: *{count}*\n"
        f"🏆 Bonus per referral: *{REFERRAL_BONUS} ETB* (after their first top-up)\n\n"
        "Share your link and earn!",
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_approve(update: Update, ctx) -> None:
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /approve <payment_id> <amount>")
        return
    try:
        pid, amount = int(args[0]), float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return

    with db() as c:
        payment = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not payment:
            await update.message.reply_text("❌ Payment not found.")
            return
        if payment["status"] != "pending":
            await update.message.reply_text(f"⚠️ Already {payment['status']}.")
            return
        c.execute(
            "UPDATE payments SET status='approved', amount=?, reviewed_by=?, reviewed_at=datetime('now') WHERE id=?",
            (amount, update.effective_user.id, pid),
        )
        c.execute(
            "UPDATE users SET credits=credits+? WHERE user_id=?",
            (amount, payment["user_id"]),
        )
        # Referral bonus
        referred_user = c.execute(
            "SELECT referred_by FROM users WHERE user_id=?", (payment["user_id"],)
        ).fetchone()
        referrer_id = None
        if referred_user and referred_user["referred_by"]:
            # Check if this is their first approved payment
            prev = c.execute(
                "SELECT COUNT(*) as n FROM payments WHERE user_id=? AND status='approved' AND id!=?",
                (payment["user_id"], pid),
            ).fetchone()["n"]
            if prev == 0:
                referrer_id = referred_user["referred_by"]
                c.execute(
                    "UPDATE users SET credits=credits+? WHERE user_id=?",
                    (REFERRAL_BONUS, referrer_id),
                )

    await update.message.reply_text(f"✅ Approved {amount} ETB for user {payment['user_id']}.")
    try:
        await ctx.bot.send_message(
            chat_id=payment["user_id"],
            text=f"✅ Your top-up of *{amount} ETB* has been approved!\nEnjoy generating cards 🎴",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
    if referrer_id:
        try:
            await ctx.bot.send_message(
                chat_id=referrer_id,
                text=f"🎁 You earned *{REFERRAL_BONUS} ETB* referral bonus!",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

async def cmd_reject(update: Update, ctx) -> None:
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /reject <payment_id>")
        return
    pid = int(args[0])
    with db() as c:
        payment = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not payment:
            await update.message.reply_text("❌ Not found.")
            return
        c.execute(
            "UPDATE payments SET status='rejected', reviewed_by=?, reviewed_at=datetime('now') WHERE id=?",
            (update.effective_user.id, pid),
        )
    await update.message.reply_text(f"❌ Payment {pid} rejected.")
    try:
        await ctx.bot.send_message(
            chat_id=payment["user_id"],
            text="❌ Your top-up was rejected. Please contact support.",
        )
    except Exception:
        pass

async def handle_pending(update: Update, ctx) -> None:
    if not _is_admin(update.effective_user.id):
        return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await update.message.reply_text("✅ No pending payments.")
        return
    text = "📋 *Pending Payments*\n\n"
    for r in rows:
        text += f"ID `{r['id']}` · User `{r['user_id']}` · {r['created_at'][:16]}\n"
    text += "\nUse /approve <id> <amount> or /reject <id>"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_stats(update: Update, ctx) -> None:
    if not _is_admin(update.effective_user.id):
        return
    with db() as c:
        users   = c.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        gens    = c.execute("SELECT COUNT(*) as n FROM generations").fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) as n FROM payments WHERE status='pending'").fetchone()["n"]
        revenue = c.execute("SELECT COALESCE(SUM(amount),0) as s FROM payments WHERE status='approved'").fetchone()["s"]
        by_flow = c.execute(
            "SELECT flow, COUNT(*) as n FROM generations GROUP BY flow"
        ).fetchall()
    flow_txt = "\n".join(f"  • {r['flow']}: {r['n']}" for r in by_flow)
    await update.message.reply_text(
        f"📈 *Statistics*\n\n"
        f"👥 Users: *{users}*\n"
        f"🎴 Cards generated: *{gens}*\n"
        f"💰 Total revenue: *{revenue:.2f} ETB*\n"
        f"📋 Pending payments: *{pending}*\n\n"
        f"By flow:\n{flow_txt}",
        parse_mode=ParseMode.MARKDOWN,
    )

async def handle_edit_credits_start(update: Update, ctx) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ *Edit Wallet*\n\nSend: `<user_id> <amount>`\n(negative to deduct)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return WAIT_EDIT_CREDITS

async def handle_edit_credits_input(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)
    parts = text.split()
    if len(parts) != 2:
        await update.message.reply_text("❌ Format: `<user_id> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return WAIT_EDIT_CREDITS
    try:
        uid, amt = int(parts[0]), float(parts[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid values.")
        return WAIT_EDIT_CREDITS
    add_credits(uid, amt)
    u = get_user(uid)
    new_bal = u["credits"] if u else "?"
    await update.message.reply_text(
        f"✅ Updated!\nUser `{uid}` → Balance: *{new_bal} ETB*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(True),
    )
    return ConversationHandler.END

async def handle_broadcast_start(update: Update, ctx) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "📣 *Broadcast*\n\nSend the message to broadcast to all users:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return WAIT_BROADCAST

async def handle_broadcast_msg(update: Update, ctx) -> int:
    text = (update.message.text or "").strip()
    if text == "❌ Cancel":
        return await _cancel(update, ctx)
    with db() as c:
        users = c.execute("SELECT user_id FROM users").fetchall()
    ok = fail = 0
    for row in users:
        try:
            await ctx.bot.send_message(chat_id=row["user_id"], text=text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await update.message.reply_text(
        f"📣 Broadcast done!\n✅ Sent: {ok}  ❌ Failed: {fail}",
        reply_markup=main_menu_kb(True),
    )
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════════
#  CANCEL / FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

async def _cancel(update: Update, ctx) -> int:
    ctx.user_data.clear()
    is_admin = _is_admin(update.effective_user.id)
    await update.message.reply_text(
        "❌ Cancelled. Back to main menu.",
        reply_markup=main_menu_kb(is_admin),
    )
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx) -> int:
    return await _cancel(update, ctx)

async def handle_menu_text(update: Update, ctx) -> None:
    """Route plain-text menu button presses to the right action."""
    text = (update.message.text or "").strip()
    if text == "💳 My Wallet":
        await handle_wallet(update, ctx)
    elif text == "🎨 Choose Template":
        await handle_templates(update, ctx)
    elif text == "🎁 Invite & Earn":
        await handle_referral(update, ctx)
    elif text == "📞 Support":
        await update.message.reply_text(f"📞 Contact support: @{SUPPORT_USERNAME}")
    elif text == "📋 Pending Payments":
        await handle_pending(update, ctx)
    elif text == "📈 Statistics":
        await handle_stats(update, ctx)
    elif text == "⚙️ Settings":
        await update.message.reply_text(
            f"⚙️ *Settings*\n\nCard cost: {CARD_COST} ETB\nFree trials: {FREE_TRIALS}",
            parse_mode=ParseMode.MARKDOWN,
        )

async def handle_unknown(update: Update, ctx) -> None:
    await update.message.reply_text(
        "❓ Unknown command. Use the buttons below.",
        reply_markup=main_menu_kb(_is_admin(update.effective_user.id)),
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  APPLICATION BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # ── eSignet conversation ──────────────────────────────────────────────────
    esig_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🔵 OTP 1"), esig_start)],
        states={
            ESIG_WAIT_FAN: [
                MessageHandler(filters.PHOTO, esig_receive_fan),
                MessageHandler(filters.TEXT & ~filters.COMMAND, esig_receive_fan),
            ],
            ESIG_WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, esig_receive_otp)],
            ESIG_WAIT_TEMPLATE: [CallbackQueryHandler(esig_receive_template, pattern=r"^tpl:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="esig",
    )

    # ── Post / Card-Order conversation ────────────────────────────────────────
    post_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🟠 OTP 2"), post_start)],
        states={
            POST_WAIT_ID:  [MessageHandler(filters.TEXT & ~filters.COMMAND, post_receive_id)],
            POST_WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, post_receive_otp)],
            POST_WAIT_TEMPLATE: [CallbackQueryHandler(post_receive_template, pattern=r"^tpl:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="post",
    )

    # ── TelePrint conversation ────────────────────────────────────────────────
    tele_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🟣 OTP 3"), tele_start)],
        states={
            TELE_WAIT_FAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, tele_receive_fan)],
            TELE_WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, tele_receive_otp)],
            TELE_WAIT_TEMPLATE: [CallbackQueryHandler(tele_receive_template, pattern=r"^tpl:")],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="tele",
    )

    # ── A4 Converter conversation ─────────────────────────────────────────────
    a4_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🖨️"), handle_a4_start)],
        states={
            WAIT_A4_IMAGES: [
                MessageHandler(filters.PHOTO | filters.TEXT, handle_a4_image),
            ],
            WAIT_A4_FLIP: [MessageHandler(filters.TEXT, handle_a4_flip)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="a4",
    )

    # ── Top-Up conversation ───────────────────────────────────────────────────
    topup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^💰 Top Up"), handle_topup_start)],
        states={
            WAIT_SCREENSHOT: [
                MessageHandler(filters.PHOTO | filters.TEXT, handle_topup_screenshot),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="topup",
    )

    # ── Admin: edit credits conversation ─────────────────────────────────────
    edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^✏️ Edit Wallet"), handle_edit_credits_start)],
        states={
            WAIT_EDIT_CREDITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_credits_input)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="edit_credits",
    )

    # ── Admin: broadcast conversation ─────────────────────────────────────────
    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^📣 Broadcast"), handle_broadcast_start)],
        states={
            WAIT_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast_msg)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel"), cmd_cancel),
        ],
        allow_reentry=True,
        name="broadcast",
    )

    # ── Register all handlers ─────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))

    for conv in [esig_conv, post_conv, tele_conv, a4_conv, topup_conv, edit_conv, broadcast_conv]:
        app.add_handler(conv)

    # Menu buttons that don't start conversations
    app.add_handler(MessageHandler(
        filters.Regex(r"^(💳|🎨|🎁|📞|📋|📈|👥|⚙️)"),
        handle_menu_text,
    ))

    # Fallback
    app.add_handler(MessageHandler(filters.ALL, handle_unknown))

    return app


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN not set. Add it to .env or Railway Variables.")
    init_db()
    log.info("✅ DB initialised at %s", DB_PATH)
    log.info("🤖 Starting Fayda Unified Mega Bot…")
    app = build_app()
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
