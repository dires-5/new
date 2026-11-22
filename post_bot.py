# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║          🇪🇹  FAYDA ID BOT  — ULTRA PREMIUM EDITION         ║
║                                                              ║
║  ✨ Smart • Colorful • Beautiful • Production-Ready          ║
║  🪪 Fayda Digital ID Generation via Official API            ║
║  🖨️  A4 PVC Card Layout (All Users)                         ║
║  💳 Wallet System | 📸 Screenshot Payments                  ║
║  🎨 4 Templates × Color + Grayscale                         ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import io
import re
import logging
import sqlite3
import asyncio
import base64
import json
import binascii
from datetime import datetime

import requests
from PIL import Image, ImageDraw
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app import (
    merge_to_template, merge_to_template_all_outputs,
    get_template_sample, TEMPLATES
)
from pdf_extractor import extract_from_fayda_pdf

# ══════════════════════════════════════════════════════════════
#  ⚙️  CONFIG
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8698651344:AAHhPzcTFBzdnFENBjECdCN4gZ9y5zJuzag")
ADMIN_USERNAME = "dhtechs_admin"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
PAYMENT_PHONE = "0919545335"
PRICE_PER_GEN = 50      # legacy default (OTP flow)
PRICE_OTP = 50          # OTP flow price
PRICE_PDF = 10          # PDF upload flow price
REFERRAL_BONUS = 10     # ETB credited to referrer on referee's first approved top-up
FREE_TRIALS = 1
DB_PATH = "fayda_bot.db"

BASE_URL = "https://card-order.fayda.et"
PORTAL = "https://card-order.fayda.et"

# Next.js action header
NEXT_ACTION = "707d17603b46fdbaeb88f738d9882412097f92c7be"

# ══════════════════════════════════════════════════════════════
#  🖨️  A4 SETTINGS
# ══════════════════════════════════════════════════════════════
A4_WIDTH = 2480
A4_HEIGHT = 3508
CARD_WIDTH = 2191
CARD_HEIGHT = 667
MAX_PER_PAGE = 5
DPI = 300
OUTPUT_FOLDER = "output"
CROP_LENGTH = 40
CROP_WIDTH_PX = 3
CROP_OFFSET = 15
CENTER_MARK_LEN = 80
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ══════════════════════════════════════════════════════════════
#  📝  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
)
log = logging.getLogger("fayda_bot")

# ══════════════════════════════════════════════════════════════
#  🔢  STATES
# ══════════════════════════════════════════════════════════════
(
    WAIT_ID, WAIT_OTP,
    WAIT_SCREENSHOT,
    WAIT_EDIT_CREDITS, WAIT_BROADCAST,
    WAIT_A4_IMAGES, WAIT_A4_FLIP,
    WAIT_TEMPLATE_CHOICE,
    WAIT_PDF_UPLOAD,
    WAIT_PDF_CONFIRM,
) = range(10)

# ══════════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ══════════════════════════════════════════════════════════════
def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id     INTEGER PRIMARY KEY,
            username    TEXT    DEFAULT '',
            full_name   TEXT    DEFAULT '',
            wallet      INTEGER DEFAULT 0,
            trials_used INTEGER DEFAULT 0,
            template_id INTEGER DEFAULT 3,
            referred_by INTEGER DEFAULT NULL,
            referral_bonus_paid INTEGER DEFAULT 0,
            joined_at   TEXT    DEFAULT (datetime('now')),
            last_seen   TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER NOT NULL,
            username      TEXT    DEFAULT '',
            full_name     TEXT    DEFAULT '',
            amount        INTEGER NOT NULL,
            screenshot_id TEXT,
            status        TEXT    DEFAULT 'pending',
            created_at    TEXT    DEFAULT (datetime('now')),
            reviewed_at   TEXT,
            reviewed_by   TEXT,
            note          TEXT    DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS generations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            username     TEXT    DEFAULT '',
            template_id  INTEGER DEFAULT 3,
            generated_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS global_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO global_settings (key,value) VALUES ('admin_chat_id','0');
        INSERT OR IGNORE INTO global_settings (key,value) VALUES ('dd_token','');
    """)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN template_id INTEGER DEFAULT 3")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN referral_bonus_paid INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    conn.commit()
    conn.close()
    log.info("✅ Database initialised.")

def _db_upsert_user(chat_id, username, full_name, referred_by=None):
    conn = _get_conn()
    existing = conn.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if existing:
        conn.execute("""
            UPDATE users SET username=?, full_name=?, last_seen=datetime('now')
            WHERE chat_id=?
        """, (username or "", full_name or "", chat_id))
    else:
        conn.execute("""
            INSERT INTO users (chat_id,username,full_name,referred_by)
            VALUES (?,?,?,?)
        """, (chat_id, username or "", full_name or "",
              referred_by if (referred_by and referred_by != chat_id) else None))
    conn.commit()
    conn.close()

def _db_get_user(chat_id):
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def _db_set_template(chat_id, template_id):
    conn = _get_conn()
    conn.execute("UPDATE users SET template_id=? WHERE chat_id=?", (template_id, chat_id))
    conn.commit()
    conn.close()

def _db_can_generate(chat_id, price=PRICE_PER_GEN):
    u = _db_get_user(chat_id)
    return u and (u["trials_used"] < FREE_TRIALS or u["wallet"] >= price)

def _db_deduct(chat_id, price=PRICE_PER_GEN):
    conn = _get_conn()
    u = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if not u:
        conn.close()
        return None, 0
    tid = u["template_id"] if "template_id" in u.keys() else 3
    if u["trials_used"] < FREE_TRIALS:
        conn.execute("UPDATE users SET trials_used=trials_used+1 WHERE chat_id=?", (chat_id,))
        conn.execute("INSERT INTO generations (chat_id,username,template_id) VALUES (?,?,?)",
                     (chat_id, u["username"], tid))
        conn.commit()
        remaining = FREE_TRIALS - u["trials_used"] - 1
        conn.close()
        return "trial", remaining
    else:
        new_w = max(0, u["wallet"] - price)
        conn.execute("UPDATE users SET wallet=? WHERE chat_id=?", (new_w, chat_id))
        conn.execute("INSERT INTO generations (chat_id,username,template_id) VALUES (?,?,?)",
                     (chat_id, u["username"], tid))
        conn.commit()
        conn.close()
        return "wallet", new_w

def _db_add_payment(chat_id, username, full_name, amount, screenshot_id):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO payments (chat_id,username,full_name,amount,screenshot_id) VALUES (?,?,?,?,?)",
        (chat_id, username or "", full_name or "", amount, screenshot_id))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def _db_get_payment(pid):
    conn = _get_conn()
    row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def _db_update_payment(pid, status, reviewed_by, note=""):
    conn = _get_conn()
    conn.execute("UPDATE payments SET status=?,reviewed_at=datetime('now'),reviewed_by=?,note=? WHERE id=?",
                 (status, reviewed_by, note, pid))
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

def _db_get_referral_info(chat_id):
    """Return (referred_by, referral_bonus_paid, invited_count, total_earned)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT referred_by, referral_bonus_paid FROM users WHERE chat_id=?",
        (chat_id,)).fetchone()
    invited = conn.execute(
        "SELECT COUNT(*) FROM users WHERE referred_by=?", (chat_id,)).fetchone()[0]
    earned = conn.execute(
        "SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_bonus_paid=1",
        (chat_id,)).fetchone()[0]
    conn.close()
    if not row:
        return None, 0, invited, earned * REFERRAL_BONUS
    return row["referred_by"], row["referral_bonus_paid"], invited, earned * REFERRAL_BONUS

def _db_reward_referrer_if_eligible(chat_id):
    """
    Called when a user's top-up payment is approved.
    If this user was referred and the referrer bonus hasn't been paid yet,
    credit REFERRAL_BONUS ETB to the referrer and mark as paid.
    Returns referrer_chat_id if a bonus was paid, else None.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT referred_by, referral_bonus_paid FROM users WHERE chat_id=?",
        (chat_id,)).fetchone()
    if not row or not row["referred_by"] or row["referral_bonus_paid"]:
        conn.close()
        return None
    referrer_id = row["referred_by"]
    conn.execute("UPDATE users SET wallet=wallet+? WHERE chat_id=?", (REFERRAL_BONUS, referrer_id))
    conn.execute("UPDATE users SET referral_bonus_paid=1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    return referrer_id

def _db_pending_payments():
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def _db_has_pending(chat_id):
    conn = _get_conn()
    row = conn.execute("SELECT id FROM payments WHERE chat_id=? AND status='pending'", (chat_id,)).fetchone()
    conn.close()
    return row is not None

def _db_stats():
    conn = _get_conn()
    tu = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    tg = conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
    pp = conn.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    tr = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved'").fetchone()[0]
    conn.close()
    return tu, tg, pp, tr

def _db_all_users(limit=30):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT chat_id,username,full_name,wallet,trials_used,template_id FROM users ORDER BY last_seen DESC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def _db_get_setting(key):
    conn = _get_conn()
    row = conn.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""

def _db_set_setting(key, value):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO global_settings (key,value,updated_at) VALUES (?,?,datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
    """, (key, str(value)))
    conn.commit()
    conn.close()

def _db_get_all_chat_ids():
    conn = _get_conn()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]

# ══════════════════════════════════════════════════════════════
#  👑  ADMIN HELPERS
# ══════════════════════════════════════════════════════════════
_admin_chat_id = 0

def get_admin_chat_id():
    global _admin_chat_id
    if _admin_chat_id:
        return _admin_chat_id
    saved = _db_get_setting("admin_chat_id")
    if saved and saved != "0":
        _admin_chat_id = int(saved)
    return _admin_chat_id

def set_admin_chat_id(cid):
    global _admin_chat_id
    _admin_chat_id = cid
    _db_set_setting("admin_chat_id", cid)

def is_admin(update):
    u = update.effective_user
    if not u:
        return False
    if u.username and u.username.lower() == ADMIN_USERNAME.lower():
        return True
    aid = get_admin_chat_id()
    return aid != 0 and u.id == aid

# ══════════════════════════════════════════════════════════════
#  🖨️  A4 CORE FUNCTIONS
# ══════════════════════════════════════════════════════════════
def draw_crop_marks(draw, x, y):
    draw.line((x - CROP_OFFSET, y - CROP_LENGTH, x - CROP_OFFSET, y - CROP_OFFSET), fill="black", width=CROP_WIDTH_PX)
    draw.line((x - CROP_LENGTH, y - CROP_OFFSET, x - CROP_OFFSET, y - CROP_OFFSET), fill="black", width=CROP_WIDTH_PX)
    draw.line((x + CARD_WIDTH + CROP_OFFSET, y - CROP_LENGTH, x + CARD_WIDTH + CROP_OFFSET, y - CROP_OFFSET), fill="black", width=CROP_WIDTH_PX)
    draw.line((x + CARD_WIDTH + CROP_OFFSET, y - CROP_OFFSET, x + CARD_WIDTH + CROP_LENGTH, y - CROP_OFFSET), fill="black", width=CROP_WIDTH_PX)
    draw.line((x - CROP_OFFSET, y + CARD_HEIGHT + CROP_OFFSET, x - CROP_OFFSET, y + CARD_HEIGHT + CROP_LENGTH), fill="black", width=CROP_WIDTH_PX)
    draw.line((x - CROP_LENGTH, y + CARD_HEIGHT + CROP_OFFSET, x - CROP_OFFSET, y + CARD_HEIGHT + CROP_OFFSET), fill="black", width=CROP_WIDTH_PX)
    draw.line((x + CARD_WIDTH + CROP_OFFSET, y + CARD_HEIGHT + CROP_OFFSET, x + CARD_WIDTH + CROP_OFFSET, y + CARD_HEIGHT + CROP_LENGTH), fill="black", width=CROP_WIDTH_PX)
    draw.line((x + CARD_WIDTH + CROP_OFFSET, y + CARD_HEIGHT + CROP_OFFSET, x + CARD_WIDTH + CROP_LENGTH, y + CARD_HEIGHT + CROP_OFFSET), fill="black", width=CROP_WIDTH_PX)

def draw_center_marks(draw):
    cx, cy = A4_WIDTH // 2, A4_HEIGHT // 2
    draw.line((cx, 0, cx, CENTER_MARK_LEN), fill="black", width=CROP_WIDTH_PX)
    draw.line((cx, A4_HEIGHT - CENTER_MARK_LEN, cx, A4_HEIGHT), fill="black", width=CROP_WIDTH_PX)
    draw.line((0, cy, CENTER_MARK_LEN, cy), fill="black", width=CROP_WIDTH_PX)
    draw.line((A4_WIDTH - CENTER_MARK_LEN, cy, A4_WIDTH, cy), fill="black", width=CROP_WIDTH_PX)

def generate_a4_pages(images, mirror=False):
    processed = []
    for img in images:
        img = img.convert("RGB")
        if img.size == (CARD_WIDTH, CARD_HEIGHT):
            if mirror:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            processed.append(img)
    if not processed:
        return [], None, []
    pages, png_paths = [], []
    left_margin = (A4_WIDTH - CARD_WIDTH) // 2
    remaining_sp = A4_HEIGHT - MAX_PER_PAGE * CARD_HEIGHT
    vertical_gap = remaining_sp // (MAX_PER_PAGE + 1)
    page_num = 1
    page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
    draw = ImageDraw.Draw(page)
    count = 0
    for card in processed:
        if count == MAX_PER_PAGE:
            draw_center_marks(draw)
            p = os.path.join(OUTPUT_FOLDER, f"A4_page_{page_num}.png")
            page.save(p, dpi=(DPI, DPI))
            png_paths.append(p)
            pages.append(page)
            page_num += 1
            page = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
            draw = ImageDraw.Draw(page)
            count = 0
        x = left_margin
        y = vertical_gap + count * (CARD_HEIGHT + vertical_gap)
        page.paste(card, (x, y))
        draw_crop_marks(draw, x, y)
        count += 1
    if count > 0:
        draw_center_marks(draw)
        p = os.path.join(OUTPUT_FOLDER, f"A4_page_{page_num}.png")
        page.save(p, dpi=(DPI, DPI))
        png_paths.append(p)
        pages.append(page)
    pdf_path = os.path.join(OUTPUT_FOLDER, "A4_ID_Cards_PVC_READY.pdf")
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:], format="PDF", resolution=DPI)
    return pages, pdf_path, png_paths

# ══════════════════════════════════════════════════════════════
#  🔌  API FUNCTIONS
# ══════════════════════════════════════════════════════════════

session = requests.Session()
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

def _init_session():
    """Initialize session with required cookies from the portal"""
    global session
    session = requests.Session()
    try:
        r = session.get(PORTAL, headers=_BROWSER_HEADERS, timeout=30)
        if r.status_code == 200:
            log.info("Session initialized successfully")
        return True
    except Exception as e:
        log.warning(f"Session init failed: {e}")
        return False

def _api_verify(id_number: str) -> str:
    """Send FCN to card-order API using Next.js server action format"""
    if not session.cookies:
        _init_session()

    payload = [
        {
            "id": "",
            "version": "1.0.0",
            "requesttime": None,
            "metadata": {},
            "request": {
                "fcn": id_number,
                "promoCode": "",
                "isOrderEdit": False,
                "isReprint": False
            }
        },
        "/otpService/getToken",
        True
    ]

    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "text/x-component",
        "Origin": PORTAL,
        "Referer": PORTAL + "/",
        "User-Agent": _BROWSER_HEADERS["User-Agent"],
        "next-action": NEXT_ACTION,
    }

    try:
        log.info(f"Sending OTP request for FCN: {id_number[:4]}...{id_number[-4:]}")
        r = session.post(f"{BASE_URL}/", json=payload, headers=headers, timeout=90)
        r.raise_for_status()

        response_text = r.text
        log.debug(f"Response: {response_text[:500]}")

        match = re.search(r'"transactionId":"([^"]+)"', response_text)
        if match:
            transaction_id = match.group(1)
            log.info(f"OTP sent successfully, transactionId: {transaction_id}")
            return transaction_id

        raise Exception("Could not extract transactionId from response")

    except Exception as e:
        log.error(f"API verify error: {e}")
        raise

def _api_validate_otp(otp: str, fcn: str, transaction_id: str) -> dict:
    """Validate OTP using Fayda streamed Next.js response"""

    requesttime = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + "Z"

    payload = [
        {
            "id": "",
            "version": "1.0.0",
            "requesttime": requesttime,
            "request": {
                "token": otp,
                "fcn": fcn,
                "transactionId": transaction_id,
                "isOrderEdit": False
            }
        },
        "/otpService/validateToken",
        True
    ]

    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "text/x-component",
        "Origin": PORTAL,
        "Referer": PORTAL + "/",
        "User-Agent": _BROWSER_HEADERS["User-Agent"],
        "next-action": NEXT_ACTION,
    }

    try:

        log.info(f"Validating OTP for FCN: {fcn[:4]}...{fcn[-4:]}")

        r = session.post(
            f"{BASE_URL}/",
            json=payload,
            headers=headers,
            timeout=90,
        )

        r.raise_for_status()

        response_text = r.text

        log.info(f"Response length: {len(response_text)}")

        # =====================================================
        # FIND PAYLOAD — PERMANENT LABEL-INDEPENDENT EXTRACTION
        # =====================================================
        def _extract_payload(text: str) -> str:
            m = re.search(r'(eyJ[A-Za-z0-9\-_]{50,})', text)
            if m:
                return m.group(1).strip()
            m = re.search(r',[,\s]*([A-Za-z0-9\-_]{200,})', text)
            if m:
                return m.group(1).strip()
            candidates = re.findall(r'[A-Za-z0-9\-_]{200,}', text)
            if candidates:
                return max(candidates, key=len).strip()
            raise Exception("Payload not found in response")

        try:
            encoded_raw = _extract_payload(response_text)
        except Exception:
            raise Exception("Payload not found in response")

        encoded = encoded_raw
        log.info(f"Encoded length: {len(encoded)}")

        # =====================================================
        # ROBUST BASE64URL → BYTES
        # =====================================================
        try:
            _enc = encoded.strip()
            _enc = _enc.replace('-', '+').replace('_', '/')
            _enc = re.sub(r'[^A-Za-z0-9+/=]', '', _enc)
            _enc = _enc.rstrip('=')
            _mod = len(_enc) % 4
            if _mod == 1:
                log.warning(
                    f"base64 length mod-4 == 1 ({len(_enc)+1} raw) — "
                    "dropping stray trailing byte (Fayda API quirk)"
                )
                _enc = _enc[:-1]
            elif _mod == 2:
                _enc += '=='
            elif _mod == 3:
                _enc += '='

            log.info(f"Padded length: {len(_enc)}")
            decoded_bytes = base64.b64decode(_enc, validate=False)

        except Exception as e:
            with open("bad_base64.txt", "w", encoding="utf-8") as f:
                f.write(encoded)
            raise Exception(f"Failed to decode base64: {e}")

        decoded_text = decoded_bytes.decode("utf-8", errors="ignore")
        log.info(decoded_text[:5000])

        # =====================================================
        # PARSE JSON
        # =====================================================
        try:
            identity_json = json.loads(decoded_text)
        except Exception:
            json_match = re.search(r'(\{.*\})', decoded_text, re.DOTALL)
            if not json_match:
                with open("decoded_dump.txt", "w", encoding="utf-8") as f:
                    f.write(decoded_text)
                raise Exception("JSON object not found")
            clean_json = json_match.group(1)
            clean_json = re.sub(r'[\x00-\x1F\x7F]', '', clean_json)
            identity_json = json.loads(clean_json)

        # =====================================================
        # EXTRACT IDENTITY
        # =====================================================
        identity_data = None
        if (
            isinstance(identity_json, dict)
            and "response" in identity_json
            and "identity" in identity_json["response"]
        ):
            identity_data = identity_json["response"]["identity"]
        elif (
            isinstance(identity_json, dict)
            and "identity" in identity_json
        ):
            identity_data = identity_json["identity"]

        if not identity_data:
            raise Exception("Identity data not found")

        # =====================================================
        # EXTRACT PHOTO
        # =====================================================
        def _safe_photo_pad(s):
            s = s.rstrip("=")
            mod = len(s) % 4
            if mod == 1:
                s = s[:-1]
            elif mod == 2:
                s += "=="
            elif mod == 3:
                s += "="
            return s

        photo_data = ""

        _photo_json = (
            identity_data.get("photo", "")
            or identity_data.get("Photo", "")
            or identity_data.get("photograph", "")
            or identity_data.get("face", "")
        )
        if _photo_json and isinstance(_photo_json, str) and len(_photo_json) > 100:
            _p = _photo_json.replace("-", "+").replace("_", "/")
            photo_data = _safe_photo_pad(_p)
            log.info(f"Photo extracted from JSON identity_data (len={len(photo_data)})")

        if not photo_data:
            _img_patterns = [
                r'(/9j/[A-Za-z0-9+/]{200,})',
                r'(-9j-[A-Za-z0-9\-_]{200,})',
                r'(iVBORw0KGgo[A-Za-z0-9+/\-_]{100,})',
            ]
            for _pat in _img_patterns:
                _m = re.search(_pat, response_text)
                if _m:
                    _p = _m.group(1).replace("-", "+").replace("_", "/")
                    photo_data = _safe_photo_pad(_p)
                    log.info(f"Photo extracted from raw response via pattern (len={len(photo_data)})")
                    break

        if not photo_data:
            log.warning("No photo found — card will render without photo")

        # =====================================================
        # LANGUAGE HELPER
        # =====================================================
        def get_lang(field, lang="eng"):
            value = identity_data.get(field, [])
            if isinstance(value, list):
                for item in value:
                    if item.get("language") == lang:
                        return item.get("value", "")
            elif isinstance(value, dict):
                return value.get(lang, "")
            elif isinstance(value, str):
                return value
            return ""

        # =====================================================
        # FINAL RESULT
        # =====================================================
        result = {
            "fullName": {
                "eng": get_lang("fullName", "eng"),
                "amh": get_lang("fullName", "amh"),
            },
            "dateOfBirth": identity_data.get("dateOfBirth", ""),
            "gender": {
                "eng": get_lang("gender", "eng"),
                "amh": get_lang("gender", "amh"),
            },
            "phone": identity_data.get("phone", ""),
            "uin": (
                identity_data.get("uin")
                or identity_data.get("UIN")
                or ""
            ),
            "fin": (
                identity_data.get("uin")
                or identity_data.get("UIN")
                or ""
            ),
            "uniqueId": fcn,
            "region": {
                "eng": get_lang("region", "eng"),
                "amh": get_lang("region", "amh"),
            },
            "zone": {
                "eng": get_lang("zone", "eng"),
                "amh": get_lang("zone", "amh"),
            },
            "woreda": {
                "eng": get_lang("woreda", "eng"),
                "amh": get_lang("woreda", "amh"),
            },
            "residenceStatus": {
                "eng": get_lang("residenceStatus", "eng") or "Ethiopian",
                "amh": get_lang("residenceStatus", "amh") or "ኢትዮጵያዊ",
            },
            "email": identity_data.get("email", ""),
            "photo": photo_data,
        }

        log.info(f"OTP validated successfully: {result['fullName']['eng']}")
        return result

    except requests.exceptions.Timeout:
        raise Exception("Connection timeout. Please try again.")
    except requests.exceptions.ConnectionError:
        raise Exception("Cannot connect to Fayda server.")
    except Exception as e:
        log.exception("OTP validation failed")
        raise Exception(str(e))

# Initialize session on module load
_init_session()

# ══════════════════════════════════════════════════════════════
#  ⌨️  KEYBOARDS
# ══════════════════════════════════════════════════════════════
def user_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🪪 Send Otp"), KeyboardButton("📄 Upload PDF")],
        [KeyboardButton("🎨 Choose Template"), KeyboardButton("🖨️ A4 Converter")],
        [KeyboardButton("💳 My Wallet"), KeyboardButton("💰 Top Up")],
        [KeyboardButton("🎁 Invite & Earn"), KeyboardButton("📞 Support")],
    ], resize_keyboard=True)

def admin_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🪪 Send Otp"), KeyboardButton("📄 Upload PDF")],
        [KeyboardButton("🎨 Choose Template"), KeyboardButton("🖨️ A4 Converter")],
        [KeyboardButton("📋 Pending Payments"), KeyboardButton("📈 Stats")],
        [KeyboardButton("👥 Users"), KeyboardButton("✏️ Edit Wallet")],
        [KeyboardButton("📣 Broadcast"), KeyboardButton("💳 My Wallet")],
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)

def a4_collect_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("✅ Done — Generate A4")],
        [KeyboardButton("❌ Cancel")]
    ], resize_keyboard=True)

def template_inline_keyboard(current_tid: int) -> InlineKeyboardMarkup:
    rows = []
    items = list(TEMPLATES.items())
    for i in range(0, len(items), 2):
        row = []
        for tid, cfg in items[i:i+2]:
            check = "✅ " if tid == current_tid else ""
            row.append(InlineKeyboardButton(
                f"{check}{tid}. {cfg['name'][:16]}",
                callback_data=f"tpl_select:{tid}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🖼️ Preview All 4 Templates", callback_data="tpl_preview_menu")])
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════════════════════
#  📡  SAFE SEND
# ══════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════
#  🏠  /start
# ══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()

    referred_by = None
    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("ref_"):
            ref_part = arg[4:]
            if ref_part.isdigit():
                referred_by = int(ref_part)

    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name, referred_by)
    user = await loop.run_in_executor(None, _db_get_user, u.id)

    if u.username and u.username.lower() == ADMIN_USERNAME.lower():
        if get_admin_chat_id() != u.id:
            set_admin_chat_id(u.id)
            log.info(f"👑 Admin registered: {u.id}")

    if is_admin(update):
        await update.message.reply_text(
            "👑 <b>ADMIN PANEL</b> — Fayda ID Bot\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🙋 Welcome back, <b>{u.first_name}</b>!\n\n"
            "📊 Use the menu below to manage users,\n"
            "   payments, wallet & broadcasts.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ All systems operational",
            reply_markup=admin_menu(), parse_mode=ParseMode.HTML)
        return

    tid = user.get("template_id", 3) or 3
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]
    trials_left = FREE_TRIALS - user["trials_used"]
    if trials_left > 0:
        status_line = f"🎁 <b>Free Trial Available!</b>\n   You have <b>{trials_left}</b> free generation(s) ready to use!"
        status_icon = "🟢"
    else:
        birr = user["wallet"]
        otp_gens = birr // PRICE_OTP
        pdf_gens = birr // PRICE_PDF
        status_icon = "🟡" if birr > 0 else "🔴"
        status_line = (f"💳 <b>Wallet Balance:</b> {birr} ETB\n"
                       f"   🪪 OTP cards: <b>{otp_gens}</b> | 📄 PDF cards: <b>{pdf_gens}</b>")

    await update.message.reply_text(
        f"🇪🇹 <b>ሰላም፣ {u.first_name}!</b>  Welcome to Fayda ID Bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🪪 Generate your <b>official Fayda Digital ID</b> card\n"
        "   in seconds using the Fayda API.\n\n"
        f"{status_icon} <b>Your Status:</b>\n"
        f"{status_line}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 <b>Current Template:</b> #{tid} — {tname}\n"
        f"   (Each template gives Color + Grayscale output)\n\n"
        f"💵 <b>Price:</b> OTP flow {PRICE_OTP} ETB | PDF flow {PRICE_PDF} ETB\n"
        f"📱 <b>TeleBirr:</b> <code>{PAYMENT_PHONE}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 <b>Choose an option below to get started!</b>",
        reply_markup=user_menu(), parse_mode=ParseMode.HTML)

# ══════════════════════════════════════════════════════════════
#  🎨  TEMPLATE SELECTION
# ══════════════════════════════════════════════════════════════
async def show_template_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    user = await loop.run_in_executor(None, _db_get_user, u.id)

    tid = user.get("template_id", 3) or 3
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]

    await update.message.reply_text(
        "🎨 <b>Choose Your ID Card Template</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Current:</b> Template #{tid} — {tname}\n\n"
        "🖼️ Each template generates:\n"
        "   • 🌈 <b>Color</b> version\n"
        "   • ⚫ <b>Grayscale</b> version (dark, PVC-optimised)\n\n"
        "Tap 🖼️ <b>Preview All 4 Templates</b> to see\n"
        "all sample images at once!\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=template_inline_keyboard(tid))

async def cb_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    tid = int(query.data.split(":")[1])
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_set_template, u.id, tid)
    cfg = TEMPLATES[tid]
    await query.edit_message_text(
        f"✅ <b>Template #{tid} Selected!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 <b>{cfg['name']}</b>\n\n"
        f"📤 Outputs: {', '.join(o.capitalize() for o in cfg['outputs'])}\n\n"
        f"Your next ID generation will use this template.\n"
        f"Tap <b>🪪 Send Otp</b> to proceed!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML)

async def cb_template_preview_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Loading all samples…")
    loop = asyncio.get_running_loop()

    samples = []
    for tid in sorted(TEMPLATES.keys()):
        sb = await loop.run_in_executor(None, get_template_sample, tid)
        if sb:
            samples.append((tid, sb))

    if not samples:
        await query.edit_message_text(
            "⚠️ Sample images not found.\n"
            "Upload sample1.png … sample4.png to static/template/ to enable previews.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("« Back", callback_data="tpl_back")
            ]]))
        return

    from telegram import InputMediaPhoto
    media = []
    for i, (tid, sb) in enumerate(samples):
        cfg = TEMPLATES[tid]
        caption = f"🖼️ <b>Template #{tid} — {cfg['name']}</b>\nOutputs: {', '.join(o.capitalize() for o in cfg['outputs'])}"
        media.append(InputMediaPhoto(media=io.BytesIO(sb), caption=caption, parse_mode=ParseMode.HTML))

    try:
        await context.bot.send_media_group(query.message.chat_id, media=media)
    except Exception as e:
        log.warning(f"send_media_group failed: {e}")
        for tid, sb in samples:
            cfg = TEMPLATES[tid]
            await context.bot.send_photo(query.message.chat_id, photo=io.BytesIO(sb),
                caption=f"🖼️ <b>Template #{tid} — {cfg['name']}</b>", parse_mode=ParseMode.HTML)

    user = await loop.run_in_executor(None, _db_get_user, query.from_user.id)
    cur_tid = user.get("template_id", 3) or 3 if user else 3
    await context.bot.send_message(query.message.chat_id,
        "👆 <b>Tap a template number to select it:</b>",
        parse_mode=ParseMode.HTML, reply_markup=template_inline_keyboard(cur_tid))

async def cb_template_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid = user.get("template_id", 3) or 3 if user else 3
    await query.edit_message_text(
        "🎨 <b>Choose Your ID Card Template</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap 👁 Preview to see each template\nbefore selecting.\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=template_inline_keyboard(tid))

# ══════════════════════════════════════════════════════════════
#  🪪  GENERATE ID FLOW  (OTP)
# ══════════════════════════════════════════════════════════════
async def start_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    can = await loop.run_in_executor(None, _db_can_generate, u.id, PRICE_OTP)
    if not can:
        has_pending = await loop.run_in_executor(None, _db_has_pending, u.id)
        if has_pending:
            await update.message.reply_text(
                "⏳ <b>Payment Under Review</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔍 Your payment screenshot is being\n"
                "   reviewed by our admin team.\n\n"
                "⏱️ Approval is usually within minutes.\n"
                "   You'll get a notification once approved!\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(
                "❌ <b>Insufficient Balance</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 You need at least <b>{PRICE_OTP} ETB</b>\n"
                "   to generate an ID card.\n\n"
                "📱 Tap <b>💰 Top Up</b> to add funds\n"
                f"   via TeleBirr: <code>{PAYMENT_PHONE}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid = user.get("template_id", 3) or 3
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]

    context.user_data.clear()
    context.user_data["template_id"] = tid

    await update.message.reply_text(
        "🪪 <b>Generate Fayda ID Card</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 Template: <b>#{tid} — {tname}</b>\n"
        f"   (Color + Grayscale output)\n\n"
        "📋 <b>Step 1 of 2</b> — Enter your FCN\n\n"
        "🔢 Please type your <b>16-digit</b>\n"
        "   Fayda ID Number (FCN):\n\n"
        "   Example: <code>1234567890123456</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    return WAIT_ID

async def recv_id_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Cancel":
        return await _cancel(update, context)
    digits = re.sub(r"[\s\-]", "", text)
    if not digits.isdigit() or len(digits) != 16:
        await update.message.reply_text(
            "⚠️ <b>Invalid FCN Number</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "❗ Your FCN must be <b>exactly 16 digits</b>.\n\n"
            "✏️ Please try again:\n"
            "   Example: <code>1234567890123456</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML)
        return WAIT_ID

    context.user_data["id_number"] = digits
    context.user_data["unique_id"] = digits

    msg = await update.message.reply_text(
        "⏳ <b>Sending OTP...</b>\n\n"
        "📡 Connecting to Fayda and sending OTP\n"
        "   to your registered phone. Please wait... 🔄")

    loop = asyncio.get_running_loop()
    try:
        transaction_id = await loop.run_in_executor(None, _api_verify, digits)
        context.user_data["transaction_id"] = transaction_id
        await msg.edit_text(
            "📱 <b>OTP Sent Successfully!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>Step 2 of 2</b> — Enter OTP\n\n"
            "✉️ Check your phone for the OTP\n"
            "   from Fayda and enter it below:\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML)
        return WAIT_OTP
    except Exception as e:
        error_msg = str(e)
        log.exception("FCN/OTP send error")
        await msg.edit_text(f"❌ Error: {error_msg[:200]}\n\nPlease try again later.")
        return WAIT_ID

async def recv_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Cancel":
        return await _cancel(update, context)

    transaction_id = context.user_data.get("transaction_id", "")
    fcn = context.user_data.get("id_number", "")
    unique_id = context.user_data.get("unique_id", "")
    tid = context.user_data.get("template_id", 3) or 3

    if not transaction_id or not fcn:
        await update.message.reply_text("⚠️ Session expired. Please start over with /start.")
        return await _cancel(update, context)

    cfg = TEMPLATES.get(tid, TEMPLATES[3])
    modes = cfg["outputs"]

    msg = await update.message.reply_text(
        "⚙️ <b>Generating your ID Card...</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 Validating OTP...\n"
        "📥 Fetching your Fayda data...\n"
        "🎨 Removing photo background...\n"
        f"🖼️ Rendering Template #{tid} ({cfg['name']})...\n"
        f"   Output: {' + '.join(m.capitalize() for m in modes)}\n\n"
        "⏱️ This may take 30–60 seconds\n"
        "━━━━━━━━━━━━━━━━━━━━━━")

    loop = asyncio.get_running_loop()
    try:
        api_data = await loop.run_in_executor(None, _api_validate_otp, text, fcn, transaction_id)
        api_data["uniqueId"] = unique_id
        if not api_data.get("qr_jwt"):
            api_data["qr_jwt"] = transaction_id

        rendered = await loop.run_in_executor(
            None, merge_to_template_all_outputs, api_data, unique_id, tid)

        deduct_type, remaining = await loop.run_in_executor(None, _db_deduct, update.effective_user.id, PRICE_OTP)
        if deduct_type == "trial":
            bal_note = f"🎁 Free trial used! <b>{remaining}</b> trial(s) remaining." if remaining > 0 else "🎁 Last free trial used.\nTap 💰 Top Up to continue!"
        else:
            gens_left = remaining // PRICE_OTP
            bal_note = f"💳 Wallet: <b>{remaining} ETB</b>  ({gens_left} OTP generation(s) left)"

        await msg.delete()

        # ── Raw photo bytes ───────────────────────────────────────────────────
        raw_photo_b64 = api_data.get("photo", "")
        raw_photo_bytes = None
        if raw_photo_b64:
            try:
                from app import safe_b64decode as _safe_b64
                raw_photo_bytes = _safe_b64(raw_photo_b64)
            except Exception:
                raw_photo_bytes = None

        # ── Send to USER ──────────────────────────────────────────────────────
        if raw_photo_bytes:
            await update.message.reply_photo(
                photo=io.BytesIO(raw_photo_bytes),
                caption="📷 <b>Raw Photo from Fayda</b>",
                parse_mode=ParseMode.HTML)

        for mode in ["gray", "color"]:
            png_bytes = rendered.get(mode)
            if png_bytes is None:
                continue
            mode_label = "🌈 Color" if mode == "color" else "⚫ Grayscale"
            await update.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=(
                    f"✅ <b>Fayda ID Card — {mode_label}</b>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎨 Template #{tid}: <b>{cfg['name']}</b>\n"
                    f"{bal_note}\n\n"
                    "🖨️ Want a print-ready version?\n"
                    "   Tap <b>🖨️ A4 Converter</b> and upload this image!\n\n"
                    f"📞 Support: @{ADMIN_USERNAME}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                parse_mode=ParseMode.HTML)

        # ── Send to ADMIN ─────────────────────────────────────────────────────
        admin_id = get_admin_chat_id()
        if admin_id:
            u = update.effective_user
            full_name = api_data.get("fullName", {}).get("eng", "Unknown")
            uin = api_data.get("uin", "N/A")
            header = (
                f"🔔 <b>New ID Generated</b>\n"
                f"👤 {full_name}  |  UIN: <code>{uin}</code>\n"
                f"👤 User: {u.full_name} (@{u.username or 'N/A'})\n"
                f"🆔 Chat: <code>{u.id}</code>\n"
                f"🎨 Template #{tid}: {cfg['name']}"
            )
            if raw_photo_bytes:
                await safe_send_photo(context.bot, admin_id,
                    photo=io.BytesIO(raw_photo_bytes),
                    caption=f"{header}\n📷 <b>Raw Photo</b>",
                    parse_mode=ParseMode.HTML)
            for mode in ["gray", "color"]:
                png_bytes = rendered.get(mode)
                if png_bytes is None:
                    continue
                mode_label = "🌈 Color" if mode == "color" else "⚫ Grayscale"
                await safe_send_photo(context.bot, admin_id,
                    photo=io.BytesIO(png_bytes),
                    caption=f"<b>{mode_label} ID</b>",
                    parse_mode=ParseMode.HTML)

        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("👇 What would you like to do next?", reply_markup=kb)
        return ConversationHandler.END

    except Exception as e:
        log.exception("OTP/ID generation error")
        await msg.edit_text(f"❌ Error generating ID: {str(e)[:300]}\n\nPlease try again.")
        return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  💰  TOP UP FLOW
# ══════════════════════════════════════════════════════════════
async def start_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    has_pending = await loop.run_in_executor(None, _db_has_pending, u.id)
    if has_pending:
        await update.message.reply_text(
            "⏳ <b>Payment Still Pending</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 You already have a payment under review.\n\n"
            "⏱️ Please wait for admin approval.\n"
            "   You'll be notified once it's done!\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    await update.message.reply_text(
        "💰 <b>Top Up Your Wallet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Step 1:</b> Send any amount of ETB to:\n"
        f"   📱 TeleBirr: <code>{PAYMENT_PHONE}</code>\n\n"
        f"   💵 OTP ID Generation: <b>{PRICE_OTP} ETB</b>\n"
        f"   📄 PDF ID Generation: <b>{PRICE_PDF} ETB</b>\n\n"
        f"📌 <b>Step 2:</b> Screenshot the confirmation\n\n"
        f"📌 <b>Step 3:</b> Send the screenshot here, with the\n"
        f"   amount you sent as the caption 👇\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Approval is usually within minutes!",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    return WAIT_SCREENSHOT

async def recv_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip() == "❌ Cancel":
        return await _cancel(update, context)
    if not update.message.photo:
        await update.message.reply_text(
            "📸 <b>Screenshot Required</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send a <b>photo/screenshot</b> of your\n"
            "TeleBirr payment confirmation.\n\n"
            "📱 Take a screenshot → Send it here\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML)
        return WAIT_SCREENSHOT
    u = update.effective_user
    photo = update.message.photo[-1]
    file_id = photo.file_id
    loop = asyncio.get_running_loop()
    amount = PRICE_OTP
    caption = (update.message.caption or "").strip()
    if caption.isdigit() and int(caption) > 0:
        amount = int(caption)
    pid = await loop.run_in_executor(None, _db_add_payment, u.id, u.username, u.full_name, amount, file_id)
    admin_id = get_admin_chat_id()
    if admin_id:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{pid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"pay_no:{pid}"),
        ]])
        try:
            await context.bot.send_photo(admin_id, photo=file_id,
                caption=(
                    f"🔔 <b>New Payment Request #{pid}</b>\n\n"
                    f"👤 {u.full_name} (@{u.username or 'N/A'})\n"
                    f"🆔 Chat: <code>{u.id}</code>\n"
                    f"💵 Amount: <b>{amount} ETB</b>\n"
                    f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                ),
                reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            log.warning(f"Admin notify failed: {e}")
    await update.message.reply_text(
        "✅ <b>Payment Screenshot Received!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Reference #: <b>{pid}</b>\n"
        f"💵 Amount: <b>{amount} ETB</b>\n\n"
        "⏳ Waiting for admin approval...\n"
        "   🔔 You'll be notified automatically!\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML, reply_markup=user_menu())
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  🖨️  A4 CONVERTER FLOW
# ══════════════════════════════════════════════════════════════
async def start_a4_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    context.user_data["a4_images"] = []
    context.user_data["a4_flip"] = False
    await update.message.reply_text(
        "🖨️ <b>A4 PVC Card Converter</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📐 Creates print-ready A4 layout with:\n"
        "   ✂️ Crop marks for precise cutting\n"
        "   ⊕ Center alignment guides\n"
        "   🗂️ Up to 5 cards per A4 page\n"
        "   📄 Output: PDF + PNG (300 DPI)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>IMPORTANT:</b>\n"
        "   📲 Upload only ID card images\n"
        "   <b>downloaded from this bot</b>.\n"
        "   Other images won't fit the template\n"
        f"   (must be exactly {CARD_WIDTH}×{CARD_HEIGHT} px).\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📤 Send your card image(s) now:",
        parse_mode=ParseMode.HTML, reply_markup=a4_collect_kb())
    return WAIT_A4_IMAGES

async def recv_a4_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip() if update.message.text else ""
    if text == "❌ Cancel":
        context.user_data.pop("a4_images", None)
        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("❌ Cancelled.", reply_markup=kb)
        return ConversationHandler.END
    if text == "✅ Done — Generate A4":
        images_data = context.user_data.get("a4_images", [])
        if not images_data:
            await update.message.reply_text("⚠️ <b>No Images Yet!</b>\n\nPlease send at least one ID card image first.", parse_mode=ParseMode.HTML)
            return WAIT_A4_IMAGES
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("↔️ Yes, Flip Horizontally", callback_data="a4_flip_yes"),
            InlineKeyboardButton("➡️ No, Keep Normal", callback_data="a4_flip_no"),
        ]])
        await update.message.reply_text(
            "↔️ <b>Horizontal Flip Option</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 {len(images_data)} image(s) ready to process.\n\n"
            "🔄 Do you want to <b>flip the cards\n"
            "   horizontally</b> before printing?\n\n"
            "   <i>(Useful for double-sided PVC printing)</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return WAIT_A4_FLIP
    if update.message.photo or update.message.document:
        if update.message.photo:
            file_obj = await context.bot.get_file(update.message.photo[-1].file_id)
        else:
            file_obj = await context.bot.get_file(update.message.document.file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        buf.seek(0)
        try:
            img = Image.open(buf)
            w, h = img.size
            if (w, h) != (CARD_WIDTH, CARD_HEIGHT):
                await update.message.reply_text(
                    "⚠️ <b>Wrong Image Size — Skipped!</b>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"❌ Your image: <b>{w}×{h} px</b>\n"
                    f"✅ Required:   <b>{CARD_WIDTH}×{CARD_HEIGHT} px</b>\n\n"
                    "🔔 <b>Only use ID card images downloaded\n"
                    "   directly from this bot.</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode=ParseMode.HTML)
                return WAIT_A4_IMAGES
            buf.seek(0)
            context.user_data.setdefault("a4_images", []).append(buf.read())
            count = len(context.user_data["a4_images"])
            pages_needed = -(-count // MAX_PER_PAGE)
            await update.message.reply_text(
                f"✅ <b>Image {count} Added!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Cards collected: <b>{count}</b>\n"
                f"📄 A4 pages needed: <b>{pages_needed}</b>\n\n"
                "Send more images or tap ✅ Done!\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Could not read image: {str(e)[:100]}")
        return WAIT_A4_IMAGES
    await update.message.reply_text("📤 Please <b>send a photo</b> or document,\nor tap <b>✅ Done</b> to generate the A4 layout.", parse_mode=ParseMode.HTML)
    return WAIT_A4_IMAGES

async def cb_a4_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mirror = query.data == "a4_flip_yes"
    images_data = context.user_data.get("a4_images", [])
    flip_label = "↔️ Horizontal flip: ON" if mirror else "➡️ Horizontal flip: OFF"
    await query.edit_message_text(
        f"⚙️ <b>Generating A4 Layout...</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖼️ Processing <b>{len(images_data)}</b> card(s)...\n"
        f"{flip_label}\n\n"
        f"📐 Arranging layout...\n"
        f"✂️ Adding crop marks...\n"
        f"📄 Exporting PDF + PNG...\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    def _build():
        imgs = [Image.open(io.BytesIO(b)) for b in images_data]
        return generate_a4_pages(imgs, mirror=mirror)
    try:
        pages, pdf_path, png_paths = await loop.run_in_executor(None, _build)
        if not pages:
            await context.bot.send_message(update.effective_chat.id, "❌ No valid images found. Please check sizes.", parse_mode=ParseMode.HTML)
            context.user_data.pop("a4_images", None)
            kb = admin_menu() if is_admin(update) else user_menu()
            await context.bot.send_message(update.effective_chat.id, "Back to menu.", reply_markup=kb)
            return ConversationHandler.END
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(update.effective_chat.id, document=f,
                filename="Fayda_A4_PVC_READY.pdf",
                caption=(
                    "✅ <b>A4 Print-Ready PDF!</b>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📄 Pages: <b>{len(pages)}</b>\n"
                    f"🪪 Cards: <b>{len(images_data)}</b>\n"
                    f"{flip_label}\n"
                    "🖨️ Resolution: <b>300 DPI</b>\n\n"
                    "📋 Send to your PVC card printer!\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ), parse_mode=ParseMode.HTML)
        for i, png_path in enumerate(png_paths, 1):
            with open(png_path, "rb") as f:
                await context.bot.send_document(update.effective_chat.id, document=f,
                    filename=f"A4_Page_{i}.png",
                    caption=f"🖼️ <b>Page {i} of {len(png_paths)}</b> — PNG (300 DPI)",
                    parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("A4 generation error")
        await context.bot.send_message(update.effective_chat.id, f"❌ <b>Error generating A4:</b>\n<i>{str(e)[:300]}</i>", parse_mode=ParseMode.HTML)
    context.user_data.pop("a4_images", None)
    kb = admin_menu() if is_admin(update) else user_menu()
    await context.bot.send_message(update.effective_chat.id, "🎉 <b>Done!</b> Your print files are ready above 👆", parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  💳  PAYMENT CALLBACKS
# ══════════════════════════════════════════════════════════════
async def cb_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.answer("⛔ Admin only!", show_alert=True)
        return
    action, pid_str = query.data.split(":", 1)
    pid = int(pid_str)
    loop = asyncio.get_running_loop()
    payment = await loop.run_in_executor(None, _db_get_payment, pid)
    if not payment:
        await query.edit_message_caption("❓ Payment not found.")
        return
    if payment["status"] != "pending":
        await query.edit_message_caption(f"ℹ️ Payment #{pid} already {payment['status']}.")
        return
    admin_name = update.effective_user.username or str(update.effective_user.id)
    if action == "pay_ok":
        amount = payment["amount"]
        await loop.run_in_executor(None, _db_update_payment, pid, "approved", admin_name)
        await loop.run_in_executor(None, _db_add_wallet, payment["chat_id"], amount)
        otp_gens = amount // PRICE_OTP
        pdf_gens = amount // PRICE_PDF
        await safe_send(context.bot, payment["chat_id"],
            f"🎉 <b>Payment Approved!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>{amount} ETB</b> added to your wallet!\n"
            f"🪪 OTP ID cards possible: <b>{otp_gens}</b> ({PRICE_OTP} ETB each)\n"
            f"📄 PDF ID cards possible: <b>{pdf_gens}</b> ({PRICE_PDF} ETB each)\n\n"
            f"👇 Tap <b>🪪 Send Otp</b> or <b>📄 Upload PDF</b> to start!\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML)

        referrer_id = await loop.run_in_executor(None, _db_reward_referrer_if_eligible, payment["chat_id"])
        if referrer_id:
            await safe_send(context.bot, referrer_id,
                f"🎁 <b>Referral Bonus!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ <b>{REFERRAL_BONUS} ETB</b> added to your wallet!\n"
                f"👤 A friend you invited just topped up.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML)

        await query.edit_message_caption(
            f"✅ <b>APPROVED — Payment #{pid}</b>\n\n"
            f"👤 {payment['full_name']} (@{payment['username']})\n"
            f"💵 {amount} ETB"
            + (f" → +{REFERRAL_BONUS} ETB referral bonus sent to referrer" if referrer_id else "")
            + f"\n👑 By: @{admin_name}")
    elif action == "pay_no":
        await loop.run_in_executor(None, _db_update_payment, pid, "rejected", admin_name)
        await safe_send(context.bot, payment["chat_id"],
            f"❌ <b>Payment Rejected</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Reference #: {pid}\n\n"
            f"If you think this is a mistake,\n"
            f"please contact @{ADMIN_USERNAME} directly.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML)
        await query.edit_message_caption(
            f"❌ <b>REJECTED — Payment #{pid}</b>\n\n"
            f"👤 {payment['full_name']} (@{payment['username']})\n"
            f"💵 {payment['amount']} ETB\n"
            f"👑 By: @{admin_name}")

# ══════════════════════════════════════════════════════════════
#  👑  ADMIN HANDLERS
# ══════════════════════════════════════════════════════════════
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    loop = asyncio.get_running_loop()
    payments = await loop.run_in_executor(None, _db_pending_payments)
    if not payments:
        await update.message.reply_text("✅ <b>All Clear!</b>\n\nNo pending payments at this time. 🎉", parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(f"📋 <b>{len(payments)} Pending Payment(s)</b>", parse_mode=ParseMode.HTML)
    for p in payments:
        text = (
            f"🔔 <b>Payment #{p['id']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 {p['full_name']} (@{p['username'] or 'N/A'})\n"
            f"🆔 Chat: <code>{p['chat_id']}</code>\n"
            f"💵 Amount: <b>{p['amount']} ETB</b>\n"
            f"📅 {p['created_at']}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{p['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"pay_no:{p['id']}"),
        ]])
        if p["screenshot_id"]:
            try:
                await context.bot.send_photo(update.effective_chat.id, photo=p["screenshot_id"], caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except:
                await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    loop = asyncio.get_running_loop()
    tu, tg, pp, tr = await loop.run_in_executor(None, _db_stats)
    await update.message.reply_text(
        "📈 <b>Bot Statistics</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users:       <b>{tu}</b>\n"
        f"🪪 Total Generations: <b>{tg}</b>\n"
        f"⏳ Pending Payments:  <b>{pp}</b>\n"
        f"💰 Total Revenue:     <b>{tr} ETB</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 Avg Revenue/User: <b>{round(tr/tu) if tu else 0} ETB</b>",
        parse_mode=ParseMode.HTML)

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    loop = asyncio.get_running_loop()
    users = await loop.run_in_executor(None, _db_all_users, 25)
    if not users:
        await update.message.reply_text("No users yet.")
        return
    lines = ["👥 <b>Recent Users (Top 25)</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"]
    for u in users:
        trials_left = max(0, FREE_TRIALS - u["trials_used"])
        otp_g = u["wallet"] // PRICE_OTP
        pdf_g = u["wallet"] // PRICE_PDF
        tid = u.get("template_id", 3) or 3
        lines.append(f"• <b>{u['full_name']}</b> (@{u['username'] or 'N/A'})\n  🆔 <code>{u['chat_id']}</code> | 💳 {u['wallet']} ETB (OTP:{otp_g}/PDF:{pdf_g}) | 🎁 {trials_left} trial | 🎨 T#{tid}")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        for i in range(0, len(lines), 10):
            await update.message.reply_text("\n".join(lines[i:i+10]), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def start_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ <b>Edit User Wallet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>CHAT_ID AMOUNT</code>\n\n"
        "Examples:\n"
        "• <code>123456789 200</code>  → set to 200 ETB\n"
        "• <code>123456789 +100</code> → add 100 ETB\n"
        "• <code>123456789 -50</code>  → subtract 50 ETB\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    return WAIT_EDIT_CREDITS

async def recv_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "❌ Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return ConversationHandler.END
    try:
        parts = text.split()
        if len(parts) < 2:
            raise ValueError()
        chat_id = int(parts[0])
        amount_str = parts[1]
        loop = asyncio.get_running_loop()
        user = await loop.run_in_executor(None, _db_get_user, chat_id)
        if not user:
            await update.message.reply_text("❌ User not found. Check Chat ID.")
            return WAIT_EDIT_CREDITS
        if amount_str.startswith("+"):
            amt = int(amount_str[1:])
            new_bal = user["wallet"] + amt
            action = f"➕ Added {amt} ETB"
        elif amount_str.startswith("-"):
            amt = int(amount_str[1:])
            new_bal = max(0, user["wallet"] - amt)
            action = f"➖ Subtracted {amt} ETB"
        else:
            new_bal = int(amount_str)
            action = f"📌 Set to {new_bal} ETB"
        await loop.run_in_executor(None, _db_set_wallet, chat_id, new_bal)
        otp_g = new_bal // PRICE_OTP
        pdf_g = new_bal // PRICE_PDF
        await update.message.reply_text(
            f"✅ <b>Wallet Updated!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 {user['full_name']} (@{user['username'] or 'N/A'})\n"
            f"🔧 {action}\n"
            f"💳 New Balance: <b>{new_bal} ETB</b> (OTP:{otp_g}/PDF:{pdf_g})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML, reply_markup=admin_menu())
        gens = new_bal // PRICE_OTP
        await safe_send(context.bot, chat_id,
            f"💳 <b>Wallet Updated!</b>\n\nNew balance: <b>{new_bal} ETB</b>\n🪪 Can generate: <b>{gens}</b> OTP ID card(s)",
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except:
        await update.message.reply_text("⚠️ Invalid format. Use:\n<code>CHAT_ID AMOUNT</code>", parse_mode=ParseMode.HTML)
        return WAIT_EDIT_CREDITS

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "📣 <b>Broadcast Message</b>\n\n"
        "Type your message to send to all users:",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    return WAIT_BROADCAST

async def recv_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "❌ Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return ConversationHandler.END

    loop = asyncio.get_running_loop()
    chat_ids = await loop.run_in_executor(None, _db_get_all_chat_ids)
    sent = 0
    failed = 0

    status_msg = await update.message.reply_text(f"📣 Broadcasting to <b>{len(chat_ids)}</b> users...", parse_mode=ParseMode.HTML)

    broadcast_text = f"📢 <b>Message from Fayda ID Bot</b>\n\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━"

    for cid in chat_ids:
        try:
            await context.bot.send_message(cid, broadcast_text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramError as e:
            log.debug(f"Broadcast skip {cid}: {e}")
            failed += 1
        except Exception as e:
            log.debug(f"Broadcast skip {cid}: {e}")
            failed += 1
        await asyncio.sleep(0.07)

    try:
        await status_msg.edit_text(
            f"📣 <b>Broadcast Complete!</b>\n\n✅ Delivered: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    except Exception:
        pass
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════
#  💳  WALLET & SUPPORT
# ══════════════════════════════════════════════════════════════
async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    trials_left = max(0, FREE_TRIALS - user["trials_used"])
    otp_gens = user["wallet"] // PRICE_OTP
    pdf_gens = user["wallet"] // PRICE_PDF
    tid = user.get("template_id", 3) or 3
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]
    status = "🟢 Ready to generate!" if (trials_left > 0 or user["wallet"] > 0) else "🔴 Add funds to generate"
    await update.message.reply_text(
        f"💳 <b>Your Wallet</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {u.full_name}\n\n"
        f"🎁 Free Trials:   <b>{trials_left}</b> remaining\n"
        f"💰 Balance:       <b>{user['wallet']} ETB</b>\n"
        f"🪪 OTP Cards:     <b>{otp_gens}</b> (at {PRICE_OTP} ETB each)\n"
        f"📄 PDF Cards:     <b>{pdf_gens}</b> (at {PRICE_PDF} ETB each)\n\n"
        f"🎨 Active Template: <b>#{tid} — {tname}</b>\n\n"
        f"{status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 OTP Flow: {PRICE_OTP} ETB | PDF Flow: {PRICE_PDF} ETB",
        parse_mode=ParseMode.HTML)

async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 <b>Support Center</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 Admin: @{ADMIN_USERNAME}\n\n"
        "Common issues:\n"
        f"• 💳 Payment not approved → Send screenshot to admin\n"
        f"• 🪪 ID generation failed → Try /start again\n"
        f"• 💰 Wallet issue → Contact @{ADMIN_USERNAME}\n"
        f"• 🎨 Template issue → Use 🎨 Choose Template\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏱️ Response time: Usually within minutes",
        parse_mode=ParseMode.HTML)

async def show_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{u.id}"

    _, _, invited, earned = await loop.run_in_executor(None, _db_get_referral_info, u.id)

    await update.message.reply_text(
        "🎁 <b>Invite & Earn</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Earn <b>{REFERRAL_BONUS} ETB</b> for every friend\n"
        "   who joins using your link and tops up\n"
        "   their wallet for the first time!\n\n"
        "🔗 <b>Your Invite Link:</b>\n"
        f"<code>{link}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Friends Invited: <b>{invited}</b>\n"
        f"💰 Total Earned:    <b>{earned} ETB</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📤 Share this link with friends to start earning!",
        parse_mode=ParseMode.HTML)

# ══════════════════════════════════════════════════════════════
#  🔧  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════
async def cmd_set_ddtoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_ddtoken YOUR_TOKEN")
        return
    _db_set_setting("dd_token", " ".join(context.args).strip())
    await update.message.reply_text("✅ DD Token updated successfully!")

async def cmd_get_ddtoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    t = _db_get_setting("dd_token")
    await update.message.reply_text(f"🔑 Current DD Token:\n<code>{t or 'Not set'}</code>", parse_mode=ParseMode.HTML)

async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        chat_id = int(context.args[0])
        amount = int(context.args[1])
        loop = asyncio.get_running_loop()
        user = await loop.run_in_executor(None, _db_get_user, chat_id)
        if not user:
            await update.message.reply_text("❌ User not found.")
            return
        await loop.run_in_executor(None, _db_add_wallet, chat_id, amount)
        new_bal = user["wallet"] + amount
        await update.message.reply_text(f"✅ Added {amount} ETB to {user['full_name']}.\nNew balance: {new_bal} ETB")
        await safe_send(context.bot, chat_id,
            f"💳 Admin added <b>{amount} ETB</b> to your wallet!\nNew balance: <b>{new_bal} ETB</b>",
            parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("Usage: /addwallet CHAT_ID AMOUNT")

# ══════════════════════════════════════════════════════════════
#  ❌  CANCEL
# ══════════════════════════════════════════════════════════════
async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=kb)
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _cancel(update, context)

# ══════════════════════════════════════════════════════════════
#  🔀  MESSAGE ROUTER
# ══════════════════════════════════════════════════════════════
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    text = update.message.text.strip()
    if text == "💳 My Wallet":
        await show_wallet(update, context)
    elif text == "🎁 Invite & Earn":
        await show_invite(update, context)
    elif text == "📞 Support":
        await show_support(update, context)
    elif text == "🎨 Choose Template":
        await show_template_menu(update, context)
    elif text == "📄 Upload PDF":
        await start_pdf_upload(update, context)
    elif text == "📋 Pending Payments" and is_admin(update):
        await admin_pending(update, context)
    elif text == "👥 Users" and is_admin(update):
        await admin_users(update, context)
    elif text == "📈 Stats" and is_admin(update):
        await admin_stats(update, context)
    else:
        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("👇 Please use the menu buttons below.", reply_markup=kb)


# ══════════════════════════════════════════════════════════════
#  📄  PDF UPLOAD FLOW
# ══════════════════════════════════════════════════════════════
async def start_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    can = await loop.run_in_executor(None, _db_can_generate, u.id, PRICE_PDF)
    if not can:
        has_pending = await loop.run_in_executor(None, _db_has_pending, u.id)
        if has_pending:
            await update.message.reply_text(
                "⏳ <b>Payment Under Review</b>\n\n"
                "Your payment is still being reviewed. Wait for admin approval.",
                parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(
                "❌ <b>Insufficient Balance</b>\n\n"
                f"💰 You need at least <b>{PRICE_PDF} ETB</b> to generate a card from PDF.\n"
                "📱 Tap <b>💰 Top Up</b> to add funds.",
                parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid = user.get("template_id", 3) or 3
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]
    context.user_data.clear()
    context.user_data["template_id"] = tid

    await update.message.reply_text(
        "📄 <b>Fayda PDF → ID Card Generator</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 Template: <b>#{tid} — {tname}</b>\n\n"
        "📋 <b>How it works:</b>\n"
        "   1️⃣ Upload your Fayda PDF file\n"
        "   2️⃣ Bot extracts name, DOB, photo, QR\n"
        "   3️⃣ Merged into your chosen template\n"
        "   4️⃣ Color + Grayscale outputs delivered\n\n"
        "📤 <b>Please send your Fayda PDF file now:</b>\n"
        "   (The official PDF from id.et / efayda.com)\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb()
    )
    return WAIT_PDF_UPLOAD


async def recv_pdf_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip() == "❌ Cancel":
        return await _cancel(update, context)

    if not update.message.document:
        await update.message.reply_text(
            "📎 <b>Please send a PDF file</b> (as a document attachment).\n\n"
            "Tap the 📎 paperclip → choose your Fayda PDF file.",
            parse_mode=ParseMode.HTML
        )
        return WAIT_PDF_UPLOAD

    doc = update.message.document
    mime = doc.mime_type or ""
    fname = doc.file_name or ""

    if "pdf" not in mime.lower() and not fname.lower().endswith(".pdf"):
        await update.message.reply_text(
            "⚠️ <b>Invalid File Type</b>\n\n"
            "Please send a <b>.pdf</b> file.\n"
            "This should be the PDF downloaded from id.et or efayda.com.",
            parse_mode=ParseMode.HTML
        )
        return WAIT_PDF_UPLOAD

    msg = await update.message.reply_text(
        "⏳ <b>Processing your PDF...</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📄 Downloading PDF...\n"
        "🔍 Extracting embedded images...\n"
        "📝 Reading text fields...\n"
        "🔲 Decoding QR code...\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML
    )

    loop = asyncio.get_running_loop()

    try:
        file_obj = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        pdf_bytes = buf.getvalue()
    except Exception as e:
        await msg.edit_text(f"❌ Failed to download PDF: {str(e)[:200]}")
        return WAIT_PDF_UPLOAD

    try:
        extracted = await loop.run_in_executor(None, extract_from_fayda_pdf, pdf_bytes)
    except Exception as e:
        await msg.edit_text(f"❌ PDF processing error: {str(e)[:300]}")
        return WAIT_PDF_UPLOAD

    if not extracted["success"]:
        await msg.edit_text(
            f"❌ <b>Extraction Failed</b>\n\n"
            f"{extracted.get('error', 'Unknown error')}\n\n"
            "Please make sure you upload a valid Fayda ID card PDF.",
            parse_mode=ParseMode.HTML
        )
        return WAIT_PDF_UPLOAD

    context.user_data["pdf_api_data"] = extracted["api_data"]
    context.user_data["pdf_unique_id"] = extracted["unique_id"]
    context.user_data["pdf_qr_data"] = extracted["qr_data"]
    context.user_data["pdf_qr_crop"] = extracted["api_data"].get("qr_crop", "")
    context.user_data["pdf_fin"] = extracted.get("fin", "") or extracted["api_data"].get("fin", "") or extracted["parsed_fields"].get("fin", "")

    parsed = extracted["parsed_fields"]
    api_data = extracted["api_data"]

    name_eng = api_data["fullName"]["eng"]
    name_amh = api_data["fullName"]["amh"]
    dob = parsed.get("dob_gregorian", "")
    eth_dob = parsed.get("dob_ethiopian", "")
    sex = api_data["gender"]["eng"]
    phone = api_data["phone"]
    region = api_data["region"]["eng"]
    subcity = api_data["zone"]["eng"]
    woreda = api_data["woreda"]["eng"]
    fcn = extracted["unique_id"]
    fin = extracted.get("fin", "") or extracted["api_data"].get("fin", "") or extracted["parsed_fields"].get("fin", "")
    qr_found = "✅ QR decoded" if extracted["qr_data"] else "⚠️ QR not decoded (will regenerate)"
    photo_found = "✅ Photo extracted" if extracted["api_data"].get("photo") else "⚠️ No photo found"

    tid = context.user_data.get("template_id", 3)
    tname = TEMPLATES.get(tid, TEMPLATES[3])["name"]

    confirm_text = (
        "✅ <b>PDF Extracted Successfully!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>Extracted Information:</b>\n\n"
        f"👤 <b>Name (EN):</b> {name_eng or '—'}\n"
        f"👤 <b>Name (AM):</b> {name_amh or '—'}\n"
        f"🎂 <b>DOB (Greg):</b> {dob or '—'}\n"
        f"🎂 <b>DOB (Eth):</b> {eth_dob or '—'}\n"
        f"⚥ <b>Sex:</b> {sex or '—'}\n"
        f"📱 <b>Phone:</b> {phone or '—'}\n"
        f"📍 <b>Region:</b> {region or '—'}\n"
        f"🏙️ <b>Subcity:</b> {subcity or '—'}\n"
        f"🏘️ <b>Woreda:</b> {woreda or '—'}\n"
        f"🆔 <b>FCN:</b> <code>{fcn or '—'}</code>\n"
        f"🔑 <b>FIN:</b> <code>{fin or '—'}</code>\n\n"
        f"🖼️ {photo_found}\n"
        f"🔲 {qr_found}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 <b>Template:</b> #{tid} — {tname}\n\n"
        "🚀 Tap <b>✅ Generate Card</b> to proceed\n"
        "   or <b>❌ Cancel</b> to abort."
    )

    photo_b64 = extracted["api_data"].get("photo", "")
    if photo_b64:
        try:
            raw_photo = base64.b64decode(photo_b64)
            await msg.delete()
            await update.message.reply_photo(
                photo=io.BytesIO(raw_photo),
                caption="📷 <b>Extracted Photo from PDF</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            await msg.delete()
    else:
        await msg.delete()

    qr_img = extracted.get("qr_img")
    if qr_img is not None:
        try:
            qr_buf = io.BytesIO()
            qr_img.convert("RGB").save(qr_buf, format="PNG")
            qr_buf.seek(0)
            await update.message.reply_photo(
                photo=qr_buf,
                caption="🔲 <b>Extracted QR Code from PDF</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    confirm_kb = ReplyKeyboardMarkup([
        [KeyboardButton("✅ Generate Card")],
        [KeyboardButton("❌ Cancel")]
    ], resize_keyboard=True)

    await update.message.reply_text(confirm_text, parse_mode=ParseMode.HTML, reply_markup=confirm_kb)
    return WAIT_PDF_CONFIRM


async def recv_pdf_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "❌ Cancel":
        return await _cancel(update, context)

    if text != "✅ Generate Card":
        await update.message.reply_text(
            "👆 Please tap <b>✅ Generate Card</b> to proceed or <b>❌ Cancel</b>.",
            parse_mode=ParseMode.HTML
        )
        return WAIT_PDF_CONFIRM

    api_data = context.user_data.get("pdf_api_data")
    unique_id = context.user_data.get("pdf_unique_id", "")
    tid = context.user_data.get("template_id", 3) or 3

    if not api_data:
        await update.message.reply_text("⚠️ Session expired. Please start over.")
        return await _cancel(update, context)

    pdf_fin = context.user_data.get("pdf_fin", "")
    if pdf_fin:
        api_data["fin"] = pdf_fin

    cfg = TEMPLATES.get(tid, TEMPLATES[3])
    modes = cfg["outputs"]

    msg = await update.message.reply_text(
        "⚙️ <b>Generating ID Card from PDF data...</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🖼️ Processing photo (background removal)...\n"
        "🔲 Generating QR code...\n"
        "📊 Generating barcode...\n"
        f"🎨 Rendering Template #{tid} ({cfg['name']})...\n"
        f"   Output: {' + '.join(m.capitalize() for m in modes)}\n\n"
        "⏱️ This may take 30–60 seconds...\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML
    )

    loop = asyncio.get_running_loop()
    try:
        rendered = await loop.run_in_executor(
            None, merge_to_template_all_outputs, api_data, unique_id, tid
        )

        deduct_type, remaining = await loop.run_in_executor(
            None, _db_deduct, update.effective_user.id, PRICE_PDF
        )
        if deduct_type == "trial":
            bal_note = (
                f"🎁 Free trial used! <b>{remaining}</b> trial(s) remaining."
                if remaining > 0
                else "🎁 Last free trial used.\nTap 💰 Top Up to continue!"
            )
        else:
            gens_left = remaining // PRICE_PDF
            bal_note = f"💳 Wallet: <b>{remaining} ETB</b>  ({gens_left} PDF generation(s) left)"

        await msg.delete()

        raw_photo_b64 = api_data.get("photo", "")
        if raw_photo_b64:
            try:
                raw_photo_bytes = base64.b64decode(raw_photo_b64)
                await update.message.reply_photo(
                    photo=io.BytesIO(raw_photo_bytes),
                    caption="📷 <b>Raw Photo (from PDF)</b>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

        for mode in ["gray", "color"]:
            png_bytes = rendered.get(mode)
            if png_bytes is None:
                continue
            mode_label = "🌈 Color" if mode == "color" else "⚫ Grayscale"
            await update.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=(
                    f"✅ <b>Fayda ID Card — {mode_label}</b>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📄 <b>Source:</b> PDF Upload\n"
                    f"🎨 Template #{tid}: <b>{cfg['name']}</b>\n"
                    f"{bal_note}\n\n"
                    "🖨️ Want print-ready? Tap <b>🖨️ A4 Converter</b>!\n"
                    f"📞 Support: @{ADMIN_USERNAME}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━"
                ),
                parse_mode=ParseMode.HTML
            )

        admin_id = get_admin_chat_id()
        if admin_id:
            u = update.effective_user
            full_name = api_data.get("fullName", {}).get("eng", "Unknown")
            uin = api_data.get("uin", "N/A")
            header = (
                f"🔔 <b>New ID Generated (PDF Upload)</b>\n"
                f"👤 {full_name}  |  FCN: <code>{uin}</code>\n"
                f"👤 User: {u.full_name} (@{u.username or 'N/A'})\n"
                f"🆔 Chat: <code>{u.id}</code>\n"
                f"🎨 Template #{tid}: {cfg['name']}"
            )
            if raw_photo_b64:
                try:
                    await safe_send_photo(
                        context.bot, admin_id,
                        photo=io.BytesIO(base64.b64decode(raw_photo_b64)),
                        caption=f"{header}\n📷 <b>Raw Photo</b>",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
            for mode in ["gray", "color"]:
                png_bytes = rendered.get(mode)
                if png_bytes:
                    mode_label = "🌈 Color" if mode == "color" else "⚫ Grayscale"
                    await safe_send_photo(
                        context.bot, admin_id,
                        photo=io.BytesIO(png_bytes),
                        caption=f"<b>{mode_label} ID (PDF source)</b>",
                        parse_mode=ParseMode.HTML
                    )

        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("👇 What would you like to do next?", reply_markup=kb)
        return ConversationHandler.END

    except Exception as e:
        log.exception("PDF ID generation error")
        await msg.edit_text(
            f"❌ <b>Error generating ID card:</b>\n<i>{str(e)[:300]}</i>\n\nPlease try again.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
#  🛡️  ERROR HANDLER
# ══════════════════════════════════════════════════════════════
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, TelegramError):
        msg = str(err).lower()
        if any(s in msg for s in (
            "forbidden", "bot was blocked", "chat not found",
            "user is deactivated", "message is not modified",
            "query is too old", "message to edit not found",
            "have no rights", "not enough rights",
        )):
            log.debug(f"Ignored TelegramError: {err}")
            return
    log.error("Unhandled exception:", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        if update.effective_message.text and update.effective_message.text.startswith("📣"):
            return
        try:
            await update.effective_message.reply_text(
                "⚠️ <b>Something went wrong.</b>\n\nPlease try again or use /start.",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════
#  🚀  BUILD & RUN
# ══════════════════════════════════════════════════════════════
def build_app():
    init_db()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(8)
        .connect_timeout(30)
        .read_timeout(45)
        .write_timeout(45)
        .pool_timeout(30)
        .build()
    )

    # ── KEY FIX: regex now matches "🪪 Send Otp" AND "🪪 Send otp" ──────────
    gen_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🪪 Send [Oo]tp$"), start_generate)],
        states={
            WAIT_ID:  [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_id_number)],
            WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
        conversation_timeout=300,
    )

    topup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^💰 Top Up$"), start_topup)],
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

    edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^✏️ Edit Wallet$"), start_edit_wallet)],
        states={WAIT_EDIT_CREDITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_edit_wallet)]},
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
    )

    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^📣 Broadcast$"), start_broadcast)],
        states={WAIT_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_broadcast)]},
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
    )

    a4_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🖨️ A4 Converter$"), start_a4_convert)],
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

    pdf_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^📄 Upload PDF$"), start_pdf_upload),
            MessageHandler(filters.Document.PDF, recv_pdf_file),
        ],
        states={
            WAIT_PDF_UPLOAD: [
                MessageHandler(filters.Document.PDF, recv_pdf_file),
                MessageHandler(filters.Document.ALL, recv_pdf_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_pdf_file),
            ],
            WAIT_PDF_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_pdf_confirm),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("set_ddtoken", cmd_set_ddtoken))
    app.add_handler(CommandHandler("get_ddtoken", cmd_get_ddtoken))
    app.add_handler(CommandHandler("addwallet", cmd_addwallet))
    app.add_handler(gen_conv)
    app.add_handler(topup_conv)
    app.add_handler(edit_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(a4_conv)
    app.add_handler(pdf_conv)

    app.add_handler(CallbackQueryHandler(cb_template_select, pattern=r"^tpl_select:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_template_preview_menu, pattern=r"^tpl_preview_menu$"))
    app.add_handler(CallbackQueryHandler(cb_template_back, pattern=r"^tpl_back$"))
    app.add_handler(CallbackQueryHandler(cb_payment, pattern=r"^pay_(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_a4_flip, pattern=r"^a4_flip_(yes|no)$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_message))
    app.add_error_handler(error_handler)
    return app


def _log_system_deps():
    import shutil, subprocess
    tess = shutil.which("tesseract")
    if tess:
        try:
            v = subprocess.check_output([tess, "--version"], stderr=subprocess.STDOUT).decode().splitlines()[0]
            log.info(f"  ✅ tesseract: {v} @ {tess}")
        except Exception as e:
            log.info(f"  ✅ tesseract found @ {tess} (version check failed: {e})")
    else:
        log.warning("  ❌ tesseract NOT FOUND in PATH — FIN OCR will fail!")
    try:
        import pytesseract
        log.info(f"  ✅ pytesseract: {pytesseract.get_tesseract_version()}")
    except Exception as e:
        log.warning(f"  ❌ pytesseract error: {e}")
    try:
        import fitz
        log.info(f"  ✅ PyMuPDF (fitz): {fitz.version}")
    except Exception as e:
        log.warning(f"  ❌ PyMuPDF error: {e}")


def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║  🇪🇹 Fayda ID Bot — Starting...      ║")
    log.info("║  🎨 4 Templates × Color + Grayscale   ║")
    log.info("╚══════════════════════════════════════╝")
    log.info(f"  👑 Admin: @{ADMIN_USERNAME}")
    log.info(f"  📱 Payment: {PAYMENT_PHONE}")
    log.info(f"  💵 Price: OTP {PRICE_OTP} ETB / PDF {PRICE_PDF} ETB | 🎁 Trials: {FREE_TRIALS} | 🎁 Referral: {REFERRAL_BONUS} ETB")
    log.info(f"  🎨 Templates: {len(TEMPLATES)} (templates 1–4)")
    _log_system_deps()
    import time
    from telegram.error import Conflict as _Conflict
    app = build_app()
    for attempt in range(1, 6):
        try:
            import asyncio as _asyncio
            async def _pre_start():
                await app.bot.delete_webhook(drop_pending_updates=True)
                log.info("  ✅ Webhook cleared, starting polling...")
            _asyncio.get_event_loop().run_until_complete(_pre_start())
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False,
            )
            break
        except _Conflict as e:
            wait = attempt * 5
            log.warning(f"  ⚠️ Conflict attempt {attempt}/5 — retrying in {wait}s: {e}")
            time.sleep(wait)
    else:
        log.error("  ❌ Could not start polling after 5 attempts.")


if __name__ == "__main__":
    main()
