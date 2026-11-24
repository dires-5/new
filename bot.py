# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  🌈✨ FAYDA ID BOT  ✦  ULTRA SUPER FAST  ✦  RAINBOW EDITION  ✨🌈      ║
║                                                                          ║
║   🔵 OTP 1 — eSignet     Slow                             ║
║   🟢 OTP 2 — Card-Order   Fast                      ║
║   🟡 OTP 3 — TelePrint     (fast                         ║
║                                                                          ║
║   ⚡ ULTRA-FAST  •  🌈 COLORFUL  •  5 TEMPLATES  •  3 ENGINES           ║
║   💎 Wallet  •  🎁 Referrals  •  🖨️ A4 Converter  •  👑 Admin          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# ── venv bootstrap ────────────────────────────────────────────────────────────
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
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw

# ── OpenCV-based QR decoder (no native zbar needed) ──────────────────────────
import cv2
import numpy as _np

def _cv2_decode_qr(pil_image):
    """OpenCV QR decoder — tries 3 decoders in order of power."""
    class _R:
        def __init__(self, t): self.data = t.encode("utf-8")
    try:
        bgr = _np.array(pil_image.convert("RGB"))[:, :, ::-1].copy()
        for frame in (bgr, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)):
            d, _, _ = cv2.QRCodeDetector().detectAndDecode(frame)
            if d: return [_R(d)]
        try:
            d, _, _ = cv2.QRCodeDetectorAruco().detectAndDecode(bgr)
            if d: return [_R(d)]
        except AttributeError: pass
        try:
            ts, _ = cv2.wechat_qrcode_WeChatQRCode().detectAndDecode(bgr)
            if ts and ts[0]: return [_R(ts[0])]
        except (AttributeError, cv2.error): pass
    except Exception: pass
    return []

# ── Telegram imports ──────────────────────────────────────────────────────────
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    KeyboardButton, ReplyKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ConversationHandler, ContextTypes, MessageHandler, filters,
)

# ── App helpers (templates) ───────────────────────────────────────────────────
try:
    from app import TEMPLATES, get_template_sample, merge_to_template_all_outputs, safe_b64decode
    _APP_OK = True
except Exception as _e:
    _APP_OK = False
    TEMPLATES = {}
    log_tmp = logging.getLogger("bot")
    log_tmp.error(f"❌ app.py import FAILED: {_e}")
    def get_template_sample(tid): return None
    def merge_to_template_all_outputs(d, uid, tid=1): return {}
    def safe_b64decode(s):
        try:
            if isinstance(s, str): s = s.encode()
            return base64.b64decode(s + b"=" * ((4 - len(s) % 4) % 4))
        except: return None

# ── eSignet client (from esignet.py) ─────────────────────────────────────────
try:
    from esignet import EsignetClient, EsignetError
    _ESIGNET_OK = True
except Exception as _ee:
    _ESIGNET_OK = False
    logging.getLogger("bot").error(f"❌ esignet.py import FAILED: {_ee}")

# ═══════════════════════════════════════════════════════════════════════════════
#  ⚙️  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "dhtechs_admin")
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0"))
PAYMENT_PHONE  = os.getenv("PAYMENT_PHONE", "0111111111")

PRICE_OTP      = int(os.getenv("PRICE_OTP", "10"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "10"))
FREE_TRIALS    = int(os.getenv("FREE_TRIALS", "1"))

DB_PATH       = os.getenv("DB_PATH", "fayda_bot.db")
OUTPUT_FOLDER = "output"
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Card-Order portal (OTP 2)
_CO_BASE        = "https://card-order.fayda.et"
_CO_NEXT_ACTION = os.getenv("CO_NEXT_ACTION", "7013f027b5e7f964b90acf84520726a2ac871c59e6")

# TelePrint portal (OTP 3)
_TP_URL         = "https://teleprint.fayda.et/home"
_TP_NEXT_ACTION = os.getenv("NEXT_ACTION_ID", "7012fe1a637a6868ac88fe0298f5ecfbbbe5220e56")

# A4 print settings
A4_WIDTH = 2480; A4_HEIGHT = 3508
CARD_WIDTH = 2191; CARD_HEIGHT = 667
MAX_PER_PAGE = 5; DPI = 300
CROP_LENGTH = 40; CROP_WIDTH_PX = 3; CROP_OFFSET = 15; CENTER_MARK_LEN = 80

# ═══════════════════════════════════════════════════════════════════════════════
#  📝  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("faydabot")

# ═══════════════════════════════════════════════════════════════════════════════
#  🔢  CONVERSATION STATES
# ═══════════════════════════════════════════════════════════════════════════════
(
    WAIT_ENGINE,       # choose OTP engine
    WAIT_FAN,          # enter 16-digit FAN
    WAIT_OTP,          # enter OTP code
    WAIT_SCREENSHOT,   # top-up screenshot
    WAIT_EDIT_CREDITS, # admin edit wallet
    WAIT_BROADCAST,    # admin broadcast
    WAIT_A4_IMAGES,    # A4 images collection
    WAIT_A4_FLIP,      # A4 flip confirmation
) = range(8)

# ═══════════════════════════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
def _get_conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c

def init_db():
    c = _get_conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id             INTEGER PRIMARY KEY,
            username            TEXT    DEFAULT '',
            full_name           TEXT    DEFAULT '',
            wallet              INTEGER DEFAULT 0,
            trials_used         INTEGER DEFAULT 0,
            template_id         INTEGER DEFAULT 5,
            referred_by         INTEGER DEFAULT NULL,
            referral_bonus_paid INTEGER DEFAULT 0,
            joined_at           TEXT    DEFAULT (datetime('now')),
            last_seen           TEXT    DEFAULT (datetime('now'))
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
            template_id  INTEGER DEFAULT 5,
            engine       TEXT    DEFAULT 'esignet',
            generated_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS global_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO global_settings (key,value) VALUES ('admin_chat_id','0');
    """)
    for m in [
        "ALTER TABLE users ADD COLUMN template_id         INTEGER DEFAULT 5",
        "ALTER TABLE users ADD COLUMN referred_by         INTEGER DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN referral_bonus_paid INTEGER DEFAULT 0",
        "ALTER TABLE generations ADD COLUMN engine TEXT DEFAULT 'esignet'",
    ]:
        try: c.execute(m); c.commit()
        except: pass
    c.commit(); c.close()
    log.info("✅ Database initialised.")

# ── DB helpers ────────────────────────────────────────────────────────────────
def _db_upsert_user(chat_id, username, full_name, referred_by=None):
    c = _get_conn()
    if c.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone():
        c.execute("UPDATE users SET username=?,full_name=?,last_seen=datetime('now') WHERE chat_id=?",
                  (username or "", full_name or "", chat_id))
    else:
        c.execute("INSERT INTO users (chat_id,username,full_name,referred_by) VALUES (?,?,?,?)",
                  (chat_id, username or "", full_name or "",
                   referred_by if (referred_by and referred_by != chat_id) else None))
    c.commit(); c.close()

def _db_get_user(cid):
    c = _get_conn()
    r = c.execute("SELECT * FROM users WHERE chat_id=?", (cid,)).fetchone()
    c.close(); return dict(r) if r else None

def _db_set_template(cid, tid):
    c = _get_conn(); c.execute("UPDATE users SET template_id=? WHERE chat_id=?", (tid, cid)); c.commit(); c.close()

def _db_can_generate(cid):
    u = _db_get_user(cid)
    return bool(u and (u["trials_used"] < FREE_TRIALS or u["wallet"] >= PRICE_OTP))

def _db_deduct(cid):
    c = _get_conn()
    u = c.execute("SELECT * FROM users WHERE chat_id=?", (cid,)).fetchone()
    if not u: c.close(); return "none", 0
    tid = u["template_id"] if "template_id" in u.keys() else 5
    if u["trials_used"] < FREE_TRIALS:
        c.execute("UPDATE users SET trials_used=trials_used+1 WHERE chat_id=?", (cid,))
        c.execute("INSERT INTO generations (chat_id,username,template_id) VALUES (?,?,?)", (cid, u["username"], tid))
        c.commit(); rem = FREE_TRIALS - u["trials_used"] - 1; c.close(); return "trial", rem
    nw = max(0, u["wallet"] - PRICE_OTP)
    c.execute("UPDATE users SET wallet=? WHERE chat_id=?", (nw, cid))
    c.execute("INSERT INTO generations (chat_id,username,template_id) VALUES (?,?,?)", (cid, u["username"], tid))
    c.commit(); c.close(); return "wallet", nw

def _db_add_payment(cid, username, full_name, amount, ss_id):
    c = _get_conn()
    cur = c.execute("INSERT INTO payments (chat_id,username,full_name,amount,screenshot_id) VALUES (?,?,?,?,?)",
                    (cid, username or "", full_name or "", amount, ss_id))
    pid = cur.lastrowid; c.commit(); c.close(); return pid

def _db_get_payment(pid):
    c = _get_conn(); r = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone(); c.close()
    return dict(r) if r else None

def _db_update_payment(pid, status, by, note=""):
    c = _get_conn()
    c.execute("UPDATE payments SET status=?,reviewed_at=datetime('now'),reviewed_by=?,note=? WHERE id=?",
              (status, by, note, pid)); c.commit(); c.close()

def _db_add_wallet(cid, amt):
    c = _get_conn(); c.execute("UPDATE users SET wallet=wallet+? WHERE chat_id=?", (amt, cid)); c.commit(); c.close()

def _db_set_wallet(cid, amt):
    c = _get_conn(); c.execute("UPDATE users SET wallet=? WHERE chat_id=?", (amt, cid)); c.commit(); c.close()

def _db_has_pending(cid):
    c = _get_conn(); r = c.execute("SELECT id FROM payments WHERE chat_id=? AND status='pending'", (cid,)).fetchone()
    c.close(); return r is not None

def _db_pending_payments():
    c = _get_conn()
    rows = c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at ASC").fetchall()
    c.close(); return [dict(r) for r in rows]

def _db_stats():
    c = _get_conn()
    tu = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    tg = c.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
    pp = c.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    tr = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved'").fetchone()[0]
    c.close(); return tu, tg, pp, tr

def _db_all_users(limit=30):
    c = _get_conn()
    rows = c.execute("SELECT chat_id,username,full_name,wallet,trials_used,template_id "
                     "FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
    c.close(); return [dict(r) for r in rows]

def _db_get_setting(k):
    c = _get_conn(); r = c.execute("SELECT value FROM global_settings WHERE key=?", (k,)).fetchone()
    c.close(); return r["value"] if r else ""

def _db_set_setting(k, v):
    c = _get_conn()
    c.execute("INSERT INTO global_settings (key,value,updated_at) VALUES (?,?,datetime('now')) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=datetime('now')", (k, str(v)))
    c.commit(); c.close()

def _db_all_chat_ids():
    c = _get_conn(); rows = c.execute("SELECT chat_id FROM users").fetchall(); c.close()
    return [r["chat_id"] for r in rows]

def _db_referral_info(cid):
    c = _get_conn()
    row = c.execute("SELECT referred_by,referral_bonus_paid FROM users WHERE chat_id=?", (cid,)).fetchone()
    inv = c.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (cid,)).fetchone()[0]
    ern = c.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_bonus_paid=1", (cid,)).fetchone()[0]
    c.close()
    if not row: return None, 0, inv, ern * REFERRAL_BONUS
    return row["referred_by"], row["referral_bonus_paid"], inv, ern * REFERRAL_BONUS

def _db_reward_referrer(cid):
    c = _get_conn()
    row = c.execute("SELECT referred_by,referral_bonus_paid FROM users WHERE chat_id=?", (cid,)).fetchone()
    if not row or not row["referred_by"] or row["referral_bonus_paid"]: c.close(); return None
    rid = row["referred_by"]
    c.execute("UPDATE users SET wallet=wallet+? WHERE chat_id=?", (REFERRAL_BONUS, rid))
    c.execute("UPDATE users SET referral_bonus_paid=1 WHERE chat_id=?", (cid,))
    c.commit(); c.close(); return rid

# ═══════════════════════════════════════════════════════════════════════════════
#  👑  ADMIN STATE
# ═══════════════════════════════════════════════════════════════════════════════
_admin_chat_id: int = 0

def get_admin_id():
    global _admin_chat_id
    if _admin_chat_id: return _admin_chat_id
    s = _db_get_setting("admin_chat_id")
    if s and s != "0": _admin_chat_id = int(s)
    elif ADMIN_CHAT_ID: _admin_chat_id = ADMIN_CHAT_ID
    return _admin_chat_id

def set_admin_id(cid):
    global _admin_chat_id; _admin_chat_id = cid; _db_set_setting("admin_chat_id", cid)

def is_admin(update: Update):
    u = update.effective_user
    if not u: return False
    if u.username and u.username.lower() == ADMIN_USERNAME.lower(): return True
    aid = get_admin_id()
    return aid != 0 and u.id == aid

# ═══════════════════════════════════════════════════════════════════════════════
#  🌐  OTP ENGINE 2 — Card-Order (card-order.fayda.et)
# ═══════════════════════════════════════════════════════════════════════════════
_co_session = requests.Session()
_co_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "text/x-component",
    "Accept-Encoding": "gzip, deflate, br",   # ⚡ ULTRA-FAST: compressed responses
    "Connection": "keep-alive",                # ⚡ ULTRA-FAST: reuse TCP connections
    "Origin": _CO_BASE,
    "Referer": _CO_BASE + "/",
    "next-action": _CO_NEXT_ACTION,
})
# ⚡ ULTRA-FAST: persistent connection adapter with retry
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
_ultra_retry = Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
_ultra_adapter = HTTPAdapter(max_retries=_ultra_retry, pool_connections=8, pool_maxsize=16)
_co_session.mount("https://", _ultra_adapter)
_co_session.mount("http://",  _ultra_adapter)

def _co_send_otp(fan: str) -> str:
    """Returns transactionId."""
    payload = [{"id":"","version":"1.0.0","requesttime":None,"metadata":{},
                "request":{"fcn":fan,"promoCode":"","isOrderEdit":False,"isReprint":False}},
               "/otpService/getToken", True]
    r = _co_session.post(f"{_CO_BASE}/", json=payload,
                         headers={"Content-Type":"text/plain;charset=UTF-8"}, timeout=90)
    r.raise_for_status()
    m = re.search(r'"transactionId":"([^"]+)"', r.text)
    if not m: raise Exception("Card-Order: transactionId not found in response")
    return m.group(1)

def _co_validate_otp(otp: str, fan: str, txn_id: str) -> dict:
    """Returns identity dict (with injected _co_photo and _co_enc for card/QR fixes)."""
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + "Z"
    payload = [{"id":"","version":"1.0.0","requesttime":ts,
                "request":{"token":otp,"fcn":fan,"transactionId":txn_id,"isOrderEdit":False}},
               "/otpService/validateToken", True]
    r = _co_session.post(f"{_CO_BASE}/", json=payload,
                         headers={"Content-Type":"text/plain;charset=UTF-8"}, timeout=90)
    r.raise_for_status()
    text = r.text

    # ── Extract identity JSON from T-chunk (eyJ… blob) ──────────────────────
    def _extract(t):
        for pat in [r'(eyJ[A-Za-z0-9\-_]{50,})', r',[,\s]*([A-Za-z0-9\-_]{200,})']:
            m = re.search(pat, t)
            if m: return m.group(1)
        cands = re.findall(r'[A-Za-z0-9\-_]{200,}', t)
        if cands: return max(cands, key=len)
        raise Exception("Card-Order: payload not found in response")
    enc = _extract(text)
    enc_clean = enc.replace('-','+').replace('_','/'); enc_clean = re.sub(r'[^A-Za-z0-9+/=]','',enc_clean)
    mod = len(enc_clean) % 4
    if mod == 1: enc_clean = enc_clean[:-1]
    elif mod == 2: enc_clean += '=='
    elif mod == 3: enc_clean += '='
    raw = base64.b64decode(enc_clean, validate=False).decode("utf-8", errors="ignore")
    try: data = json.loads(raw)
    except:
        m = re.search(r'(\{.*\})', raw, re.DOTALL)
        if not m: raise Exception("Card-Order: JSON not found in decoded payload")
        data = json.loads(re.sub(r'[\x00-\x1F\x7F]','', m.group(1)))

    # ── Extract photo from second T-chunk (PNG base64 starts with iVBORw0KGgo) ─
    # Format: N:T{hex_size},{base64_png_data}
    photo_b64 = ""
    png_m = re.search(r'\d+:T[0-9a-fA-F]+,(iVBORw0KGgo[A-Za-z0-9+/=\r\n]+)', text, re.DOTALL)
    if png_m:
        # Strip any whitespace/newlines that may appear in chunked transfer
        photo_b64 = re.sub(r'\s', '', png_m.group(1))
    if not photo_b64:
        # Fallback: try JPEG base64 (/9j/ = JPEG magic)
        jpg_m = re.search(r'\d+:T[0-9a-fA-F]+,(/9j/[A-Za-z0-9+/=\r\n]+)', text, re.DOTALL)
        if jpg_m:
            photo_b64 = re.sub(r'\s', '', jpg_m.group(1))

    # ── Build qr_sign for QR (eSignet-compatible format: header..payload) ────
    # Use JWT_HEADER + identity response base64 as the payload component so the
    # QR SIGN field has the same structure as eSignet (header..signature).
    _JWT_HDR = "eyJhbGciOiJSUzI1NiJ9"
    # Keep the original urlsafe enc for the sign field (no padding)
    enc_b64u = enc.replace('+','-').replace('/','_').rstrip('=')
    qr_sign = f"{_JWT_HDR}..{enc_b64u}"

    # Navigate to identity
    if isinstance(data, dict) and "response" in data and "identity" in data["response"]:
        identity = data["response"]["identity"]
    elif isinstance(data, dict) and "identity" in data:
        identity = data["identity"]
    else:
        raise Exception("Card-Order: identity block not found")

    # Inject extracted photo and qr_sign so _co_parse_identity can pick them up
    if photo_b64:
        identity["_co_photo"] = photo_b64
    identity["_co_qr_sign"] = qr_sign
    return identity

def _co_parse_identity(identity: dict, fan: str) -> dict:
    def _bi(f):
        if isinstance(f, list):
            e = next((x.get("value","") for x in f if x.get("language")=="eng"), "")
            a = next((x.get("value","") for x in f if x.get("language")=="amh"), "")
            return {"eng":e,"amh":a}
        if isinstance(f, dict): return {"eng":f.get("eng",""),"amh":f.get("amh","")}
        return {"eng":str(f or ""),"amh":""}
    # ── Photo: prefer T-chunk photo extracted by _co_validate_otp ──────────
    photo = identity.get("_co_photo", "")  # injected PNG base64 from streaming T-chunk
    if not photo:
        bio = identity.get("individualBiometrics",{}) or {}
        if isinstance(bio, dict):
            bv = bio.get("value","") or ""
            if len(bv) > 200 and " " not in bv: photo = bv
    if not photo:
        photo = identity.get("photo") or identity.get("photograph") or identity.get("face") or ""
    if photo and (len(photo) < 100 or " " in photo): photo = ""
    # ── qr_sign: use injected value (eSignet-compatible header..payload) ───
    qr_sign = identity.get("_co_qr_sign", "")
    uin = str(identity.get("UIN") or identity.get("uin") or "")
    nat_r = identity.get("residenceStatus") or ""
    nat = _bi(nat_r) if nat_r else {"eng":"Ethiopian","amh":"ኢትዮጵያዊ"}
    if nat_r and not nat["eng"]: nat = {"eng":str(nat_r),"amh":"ኢትዮጵያዊ"}
    return {
        "fullName": _bi(identity.get("fullName") or identity.get("name") or ""),
        "dateOfBirth": str(identity.get("dateOfBirth") or identity.get("dob") or ""),
        "gender": _bi(identity.get("gender") or ""),
        "phone": str(identity.get("phone") or identity.get("phoneNumber") or ""),
        "email": str(identity.get("email") or ""),
        "uin": uin, "fin": uin, "uniqueId": fan,
        "region": _bi(identity.get("region") or ""),
        "zone":   _bi(identity.get("zone")   or ""),
        "woreda": _bi(identity.get("woreda") or ""),
        "residenceStatus": nat,
        "photo": photo, "qr_jwt": "", "qr_sign": qr_sign,
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  🌐  OTP ENGINE 3 — TelePrint (teleprint.fayda.et)
# ═══════════════════════════════════════════════════════════════════════════════
_TP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Firefox/155.0"
_TP_ROUTER_STATE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22home%22%2C%7B%22children%22%3A"
    "%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%2Ctrue%5D"
)

class _TpClient:
    """One instance per user conversation — TelePrint OTP flow."""
    def __init__(self):
        self._s = requests.Session()
        self._s.headers.update({
            "User-Agent": _TP_UA,
            "Accept": "text/x-component",
            "Accept-Encoding": "gzip, deflate, br",   # ⚡ compressed
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",                # ⚡ reuse TCP
            "Referer": "https://teleprint.fayda.et/",
            "Origin": "https://teleprint.fayda.et",
            "next-action": _TP_NEXT_ACTION,
            "next-router-state-tree": _TP_ROUTER_STATE,
        })
        # ⚡ ULTRA-FAST: persistent retry adapter
        _tp_retry = Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
        _tp_adapter = HTTPAdapter(max_retries=_tp_retry, pool_connections=4, pool_maxsize=8)
        self._s.mount("https://", _tp_adapter)
        self._s.mount("http://",  _tp_adapter)
        self.fan = ""; self.txn_id = ""; self.masked_mobile = ""

    @staticmethod
    def _now_iso():
        n = datetime.now(timezone.utc)
        return f"$D{n.strftime('%Y-%m-%dT%H:%M:%S.')+f'{n.microsecond//1000:03d}Z'}"

    def _post(self, payload):
        body = json.dumps(payload, separators=(",",":"), ensure_ascii=False)
        r = self._s.post(_TP_URL, data=body.encode("utf-8"),
                         headers={"Content-Type":"text/plain;charset=UTF-8"}, timeout=60)
        r.raise_for_status(); return r.text

    @staticmethod
    def _parse(raw):
        result = {}

        # ── Pre-process: rejoin T-chunk lines that were MIME-wrapped ─────────
        # Some servers wrap base64 at 76 chars; split("\n") cuts the chunk.
        # Rejoin any continuation line (not starting with digit(s):) back onto
        # the previous line so the T-chunk regex sees the full base64.
        joined_lines = []
        for line in raw.split("\n"):
            stripped = line.rstrip("\r")
            if joined_lines and stripped and not re.match(r'^\d+:', stripped):
                joined_lines[-1] += stripped  # continuation — append to prev
            else:
                joined_lines.append(stripped)

        for line in joined_lines:
            line = line.strip()
            if not line: continue
            try: ci = line.index(":")
            except ValueError: continue
            rest = line[ci+1:].lstrip()   # allow optional space after colon
            if rest.startswith("{"):
                try:
                    p = json.loads(rest)
                    if "a" in p and "b" in p: result["meta"] = p
                    elif "status" in p or "message" in p: result["response"] = p
                except: pass
            m = re.match(r"T([0-9a-fA-F]+),(.*)", rest, re.DOTALL)
            if m:
                try:
                    b64 = m.group(2)[:int(m.group(1),16)]
                    pad = (4 - len(b64)%4)%4
                    decoded = json.loads(base64.b64decode(b64+"="*pad).decode("utf-8","replace"))
                    # Only store if it looks like MOSIP identity structure
                    if isinstance(decoded, dict) and ("response" in decoded or "identity" in decoded or "errors" in decoded):
                        result["identity_json"] = decoded
                except: pass

        # ── Fallback: robust base64 extraction (like card-order does) ────────
        if "identity_json" not in result:
            for pat in [r'(eyJ[A-Za-z0-9\-_]{50,})', r',([A-Za-z0-9\-_]{200,})']:
                fm = re.search(pat, raw)
                if not fm: continue
                enc = fm.group(1).replace('-','+').replace('_','/')
                enc = re.sub(r'[^A-Za-z0-9+/=]', '', enc)
                mod = len(enc) % 4
                if mod == 1: enc = enc[:-1]
                elif mod == 2: enc += '=='
                elif mod == 3: enc += '='
                try:
                    decoded = json.loads(base64.b64decode(enc, validate=False).decode("utf-8","replace"))
                    if isinstance(decoded, dict) and ("response" in decoded or "identity" in decoded or "errors" in decoded):
                        result["identity_json"] = decoded
                        break
                except: pass

        return result

    def send_otp(self, fan: str) -> dict:
        self.fan = fan.strip()
        payload = [{"id":"","version":"1.0.0","requesttime":None,"metadata":{},
                    "request":{"fcn":self.fan,"isReprint":False,"isOrderEdit":False}},
                   "/otpService/getToken", True]
        raw = self._post(payload)
        data = self._parse(raw)
        resp = data.get("response",{})
        if not resp or resp.get("status") != 200:
            msg = resp.get("message","No response") if resp else "Empty response — next-action may be stale"
            raise Exception(f"TelePrint: {msg}")
        inner = resp.get("response",{})
        self.txn_id = str(inner.get("transactionId",""))
        self.masked_mobile = str(inner.get("maskedMobile",""))
        if not self.txn_id: raise Exception("TelePrint: transactionId missing")
        return {"transactionId":self.txn_id,"maskedMobile":self.masked_mobile,
                "maskedEmail":str(inner.get("maskedEmail",""))}

    def validate_otp(self, otp: str) -> dict:
        payload = [{"id":"","version":"1.0.0","requesttime":self._now_iso(),
                    "request":{"token":otp.strip(),"fcn":self.fan,
                               "transactionId":self.txn_id,"isReprint":False,"isOrderEdit":False}},
                   "/otpService/validateToken", True]
        raw = self._post(payload)

        # ── Extract photo directly from raw text (same approach as Card-Order) ──
        # Card-Order searches r.text with regex; we do the same here on raw.
        photo_b64 = ""
        png_m = re.search(r'\d+:T[0-9a-fA-F]+,(iVBORw0KGgo[A-Za-z0-9+/=\r\n]+)', raw, re.DOTALL)
        if png_m:
            photo_b64 = re.sub(r'\s', '', png_m.group(1))
        if not photo_b64:
            jpg_m = re.search(r'\d+:T[0-9a-fA-F]+,(/9j/[A-Za-z0-9+/=\r\n]+)', raw, re.DOTALL)
            if jpg_m:
                photo_b64 = re.sub(r'\s', '', jpg_m.group(1))

        # ── Build qr_sign from raw (eSignet-compatible: header..payload) ───────
        _JWT_HDR = "eyJhbGciOiJSUzI1NiJ9"
        enc_m = re.search(r'(eyJ[A-Za-z0-9\-_]{50,})', raw)
        qr_sign = ""
        if enc_m:
            enc_b64u = enc_m.group(1).replace('+', '-').replace('/', '_').rstrip('=')
            qr_sign = f"{_JWT_HDR}..{enc_b64u}"

        # ── Parse identity JSON ──────────────────────────────────────────────
        data = self._parse(raw)
        iraw = data.get("identity_json")
        if not iraw:
            low = raw.lower() if raw else ""
            if not raw.strip(): msg = "Empty response — session may have expired."
            elif raw.lstrip().startswith("<"): msg = "Server returned HTML — service may be down or next-action stale."
            elif "error" in low or "unauthorized" in low: msg = "OTP rejected by server."
            else: msg = "Identity not found in response — OTP may have expired."
            raise Exception(f"TelePrint: {msg}")

        # ── Inject photo and qr_sign into the identity block ────────────────
        # Navigate carefully; if any level is None we build the path ourselves
        # so the injection always reaches the dict that parse_identity will read.
        if isinstance(iraw, dict):
            if not isinstance(iraw.get("response"), dict):
                iraw["response"] = {}
            if not isinstance(iraw["response"].get("identity"), dict):
                iraw["response"]["identity"] = {}
            identity = iraw["response"]["identity"]
            if photo_b64:
                identity["_tp_photo"] = photo_b64
            if qr_sign:
                identity["_tp_qr_sign"] = qr_sign
        return iraw

    @staticmethod
    def parse_identity(raw: dict, fan: str) -> dict:
        def _bi(f):
            if isinstance(f, list):
                e = next((x.get("value","") for x in f if x.get("language")=="eng"), "")
                a = next((x.get("value","") for x in f if x.get("language")=="amh"), "")
                return {"eng":e,"amh":a}
            if isinstance(f, dict): return {"eng":f.get("eng",""),"amh":f.get("amh","")}
            return {"eng":str(f or ""),"amh":""}
        errs = raw.get("errors",[])
        if errs: raise Exception(errs[0].get("errorMessage","TelePrint error"))
        resp = raw.get("response",{}) or {}
        identity = resp.get("identity",{}) or {}
        if not identity: raise Exception("TelePrint: identity block missing")
        # ── Photo: prefer T-chunk photo extracted by validate_otp ────────────
        photo = identity.get("_tp_photo", "")  # injected PNG/JPEG base64 from streaming T-chunk
        if not photo:
            bio = identity.get("individualBiometrics",{}) or {}
            if isinstance(bio, dict):
                bv = bio.get("value","") or ""
                if len(bv) > 200 and " " not in bv: photo = bv
        if not photo:
            photo = identity.get("photo") or identity.get("photograph") or identity.get("face") or ""
        if photo and (len(photo) < 100 or " " in photo): photo = ""
        # ── qr_sign: use injected value (eSignet-compatible header..payload) ─
        qr_sign = identity.get("_tp_qr_sign", "")
        uin = str(identity.get("UIN") or identity.get("uin") or "")
        nat_r = identity.get("residenceStatus") or ""
        nat = _bi(nat_r) if nat_r else {"eng":"Ethiopian","amh":"ኢትዮጵያዊ"}
        if nat_r and not nat["eng"]: nat = {"eng":str(nat_r),"amh":"ኢትዮጵያዊ"}
        return {
            "fullName": _bi(identity.get("fullName") or identity.get("name") or ""),
            "dateOfBirth": str(identity.get("dateOfBirth") or identity.get("dob") or ""),
            "gender": _bi(identity.get("gender") or ""),
            "phone": str(identity.get("phone") or identity.get("phoneNumber") or ""),
            "email": str(identity.get("email") or ""),
            "uin": uin, "fin": uin, "uniqueId": fan,
            "region": _bi(identity.get("region") or ""),
            "zone":   _bi(identity.get("zone")   or ""),
            "woreda": _bi(identity.get("woreda") or ""),
            "residenceStatus": nat,
            "photo": photo, "qr_jwt": "", "qr_sign": qr_sign,
        }

# ═══════════════════════════════════════════════════════════════════════════════
#  🖨️  A4 LAYOUT
# ═══════════════════════════════════════════════════════════════════════════════
def _crop_marks(draw, x, y):
    c="black"; w=CROP_WIDTH_PX; o=CROP_OFFSET; L=CROP_LENGTH; cw=CARD_WIDTH; ch=CARD_HEIGHT
    draw.line((x-o,y-L,x-o,y-o),fill=c,width=w); draw.line((x-L,y-o,x-o,y-o),fill=c,width=w)
    draw.line((x+cw+o,y-L,x+cw+o,y-o),fill=c,width=w); draw.line((x+cw+o,y-o,x+cw+L,y-o),fill=c,width=w)
    draw.line((x-o,y+ch+o,x-o,y+ch+L),fill=c,width=w); draw.line((x-L,y+ch+o,x-o,y+ch+o),fill=c,width=w)
    draw.line((x+cw+o,y+ch+o,x+cw+o,y+ch+L),fill=c,width=w); draw.line((x+cw+o,y+ch+o,x+cw+L,y+ch+o),fill=c,width=w)

def _center_marks(draw):
    cx,cy=A4_WIDTH//2,A4_HEIGHT//2; w=CROP_WIDTH_PX; L=CENTER_MARK_LEN
    draw.line((cx,0,cx,L),fill="black",width=w); draw.line((cx,A4_HEIGHT-L,cx,A4_HEIGHT),fill="black",width=w)
    draw.line((0,cy,L,cy),fill="black",width=w); draw.line((A4_WIDTH-L,cy,A4_WIDTH,cy),fill="black",width=w)

def generate_a4_pages(images, mirror=False):
    processed = [img.convert("RGB").transpose(Image.FLIP_LEFT_RIGHT) if mirror else img.convert("RGB")
                 for img in images if img.convert("RGB").size == (CARD_WIDTH, CARD_HEIGHT)]
    if not processed: return [], None, []
    pages=[]; png_paths=[]; lm=(A4_WIDTH-CARD_WIDTH)//2
    gap=(A4_HEIGHT-MAX_PER_PAGE*CARD_HEIGHT)//(MAX_PER_PAGE+1)
    pn=1; page=Image.new("RGB",(A4_WIDTH,A4_HEIGHT),"white"); draw=ImageDraw.Draw(page); cnt=0
    for card in processed:
        if cnt==MAX_PER_PAGE:
            _center_marks(draw); p=os.path.join(OUTPUT_FOLDER,f"A4_page_{pn}.png")
            page.save(p,dpi=(DPI,DPI)); png_paths.append(p); pages.append(page)
            pn+=1; page=Image.new("RGB",(A4_WIDTH,A4_HEIGHT),"white"); draw=ImageDraw.Draw(page); cnt=0
        y=gap+cnt*(CARD_HEIGHT+gap); page.paste(card,(lm,y)); _crop_marks(draw,lm,y); cnt+=1
    if cnt>0:
        _center_marks(draw); p=os.path.join(OUTPUT_FOLDER,f"A4_page_{pn}.png")
        page.save(p,dpi=(DPI,DPI)); png_paths.append(p); pages.append(page)
    pdf=os.path.join(OUTPUT_FOLDER,"A4_ID_Cards_PVC_READY.pdf")
    pages[0].save(pdf,save_all=True,append_images=pages[1:],format="PDF",resolution=DPI)
    return pages, pdf, png_paths

# ═══════════════════════════════════════════════════════════════════════════════
#  ⌨️  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
def engine_kb():
    """⚡ Ultra-fast colorful rainbow inline buttons for engine selection."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔵⚡ OTP 1 — eSignet  [FASTEST]",    callback_data="eng:esignet"),
        ],
        [
            InlineKeyboardButton("🟢🌟 OTP 2 — Card-Order [TURBO]",   callback_data="eng:cardorder"),
        ],
        [
            InlineKeyboardButton("🟡🔥 OTP 3 — TelePrint  [ULTRA]",   callback_data="eng:teleprint"),
        ],
        [
            InlineKeyboardButton("❌ Cancel",                           callback_data="eng:cancel"),
        ],
    ])

def user_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🪪⚡ Send OTP"),      KeyboardButton("🌈 Choose Template")],
        [KeyboardButton("🖨️ A4 Converter"),    KeyboardButton("💳 My Wallet")],
        [KeyboardButton("💰 Top Up"),           KeyboardButton("🎁 Invite & Earn")],
    ], resize_keyboard=True)

def admin_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🪪⚡ Send OTP"),          KeyboardButton("🌈 Choose Template")],
        [KeyboardButton("🖨️ A4 Converter"),        KeyboardButton("💎 My Wallet")],
        [KeyboardButton("📋 Pending Payments"),    KeyboardButton("📈 Stats")],
        [KeyboardButton("👥 Users"),                KeyboardButton("✏️ Edit Wallet")],
        [KeyboardButton("📣 Broadcast"),            KeyboardButton("💰 Top Up")],
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)

def cancel_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]])

def a4_collect_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("✅ Done — Generate A4")],
        [KeyboardButton("❌ Cancel")],
    ], resize_keyboard=True)

def tpl_kb(cur_tid):
    rows = []
    items = list(TEMPLATES.items())
    for i in range(0, len(items), 2):
        row = []
        for tid, cfg in items[i:i+2]:
            row.append(InlineKeyboardButton(
                f"{'✅ ' if tid==cur_tid else ''}{tid}. {cfg['name'][:16]}",
                callback_data=f"tpl_select:{tid}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🖼️ Preview All", callback_data="tpl_preview_menu")])
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════════════════════════════════════
#  📡  SAFE SEND HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
async def _send(bot, cid, text, **kw):
    try: await bot.send_message(cid, text, **kw)
    except TelegramError as e: log.warning(f"safe_send {cid}: {e}")

async def _send_photo(bot, cid, photo, caption=None, **kw):
    try: await bot.send_photo(cid, photo, caption=caption, **kw)
    except TelegramError as e: log.warning(f"safe_send_photo {cid}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  🏠  /start
# ═══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; loop = asyncio.get_running_loop()
    referred_by = None
    if context.args:
        a = context.args[0]
        if a.startswith("ref_") and a[4:].isdigit(): referred_by = int(a[4:])
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name, referred_by)
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    if u.username and u.username.lower() == ADMIN_USERNAME.lower():
        if get_admin_id() != u.id: set_admin_id(u.id)
    if is_admin(update):
        await update.message.reply_text(
            "👑🌟 <b>ADMIN PANEL — ULTRA RAINBOW EDITION</b> 🌟👑\n"
            "🔵🟣 ━━━━━━━━━━━━━━━━━━━━ 🟣🔵\n\n"
            f"🙋‍♂️ Welcome back, <b>{u.first_name}</b>! 💫\n\n"
            "📊 Manage users • payments • wallets • broadcasts\n\n"
            "🔵🟣 ━━━━━━━━━━━━━━━━━━━━ 🟣🔵\n"
            "⚡🔵 <b>OTP 1 eSignet</b>  •  🟢<b>OTP 2 Card-Order</b>  •  🟡<b>OTP 3 TelePrint</b>\n"
            "🌈 <b>5 RAINBOW TEMPLATES</b> available!\n"
            "🔵🟣 ━━━━━━━━━━━━━━━━━━━━ 🟣🔵",
            reply_markup=admin_menu(), parse_mode=ParseMode.HTML)
        return
    tid = user.get("template_id", 5) or 5
    tname = TEMPLATES.get(tid, {}).get("name","—") if TEMPLATES else "—"
    trials_left = FREE_TRIALS - user["trials_used"]
    if trials_left > 0:
        sl = f"🎁 <b>Free Trial Ready!</b> You have <b>{trials_left}</b> free generation(s)!"; si="🟢"
    else:
        birr=user["wallet"]; si="🟡" if birr>0 else "🔴"
        sl=(f"💳 <b>Wallet:</b> {birr} ETB  ({birr//PRICE_OTP} generation(s) left)")
    await update.message.reply_text(
        f"🇪🇹🌈 <b>ሰላም፣ {u.first_name}!</b>  Welcome to Fayda ID Bot ✨⚡\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n\n"
        "🪪⚡ Generate your <b>official Fayda Digital ID</b> card\n"
        "🌈 <b>5 STUNNING TEMPLATES</b> • <b>3 ULTRA-FAST ENGINES</b>\n\n"
        f"{si} <b>Your Status:</b>\n{sl}\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"🌈 <b>Template:</b> #{tid} — {tname}\n\n"
        f"💵 <b>Price:</b> {PRICE_OTP} ETB per generation\n"
        f"📱 <b>TeleBirr:</b> <code>{PAYMENT_PHONE}</code>\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n\n"
        "👇 <b>Choose an option to get started!</b> 🚀",
        reply_markup=user_menu(), parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════════════════════
#  🪪  OTP FLOW — ENGINE SELECTION → FAN → OTP
# ═══════════════════════════════════════════════════════════════════════════════
async def start_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: user taps '🪪 Send OTP' — show engine picker."""
    u = update.effective_user; loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    can = await loop.run_in_executor(None, _db_can_generate, u.id)
    if not can:
        hp = await loop.run_in_executor(None, _db_has_pending, u.id)
        txt = ("⏳ <b>Payment Under Review</b>\n\nYour payment is being reviewed. You'll be notified once approved!"
               if hp else
               f"❌ <b>Insufficient Balance</b>\n\nYou need <b>{PRICE_OTP} ETB</b> to generate.\n"
               f"Tap <b>💰 Top Up</b> via TeleBirr: <code>{PAYMENT_PHONE}</code>")
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid = user.get("template_id", 4) or 4
    context.user_data.clear()
    context.user_data["template_id"] = tid
    await update.message.reply_text(
        "🪪⚡🌈 <b>Fayda ID Card Generator — ULTRA FAST</b> 🌈⚡🪪\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n\n"
        "⚡ Choose your <b>OTP engine</b>:\n\n"
        "🔵⚡ <b>OTP 1 — eSignet</b>  [FASTEST]\n"
        "   ✅ Official API\n\n"
        "🟢🌟 <b>OTP 2 — Card-Order</b>  [TURBO]\n"
        "   ✅ Official API\n\n"
        "🟡🔥 <b>OTP 3 — TelePrint</b>  [ULTRA]\n"
        "   ✅ Official API\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        "👇 <b>Tap your engine to begin:</b> 🚀",
        reply_markup=engine_kb(), parse_mode=ParseMode.HTML)
    return WAIT_ENGINE

async def cb_engine_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped an engine button."""
    query = update.callback_query; await query.answer()
    engine = query.data.split(":")[1]
    if engine == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ <b>Cancelled.</b>", parse_mode=ParseMode.HTML)
        kb = admin_menu() if is_admin(update) else user_menu()
        await query.message.reply_text("👇 Main menu:", reply_markup=kb)
        return ConversationHandler.END
    context.user_data["engine"] = engine
    labels = {"esignet":"🔵⚡ eSignet","cardorder":"🟢🌟 Card-Order","teleprint":"🟡🔥 TelePrint"}
    tid = context.user_data.get("template_id", 4)
    tname = TEMPLATES.get(tid,{}).get("name","—") if TEMPLATES else "—"
    await query.edit_message_text(
        f"✅ <b>Engine: {labels[engine]}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 Template: <b>#{tid} — {tname}</b>\n\n"
        "📋 <b>Step 1 of 2 — Enter your FAN</b>\n\n"
        "🔢 Type your <b>16-digit Fayda Application Number</b>:\n"
        "   Example: <code>1234567890123456</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML)
    await query.message.reply_text("✏️ Type your FAN:", reply_markup=cancel_kb())
    return WAIT_FAN

async def recv_fan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Cancel": return await _cancel(update, context)
    digits = re.sub(r"[\s\-]", "", text)
    if not digits.isdigit() or len(digits) != 16:
        await update.message.reply_text(
            "⚠️ <b>Invalid FAN</b>\n\nMust be <b>exactly 16 digits</b>.\n"
            "Example: <code>1234567890123456</code>", parse_mode=ParseMode.HTML)
        return WAIT_FAN
    context.user_data["fan"] = digits
    engine = context.user_data.get("engine", "esignet")
    msg = await update.message.reply_text(
        "⚡🌈 <b>ULTRA-FAST Connection to Fayda Server…</b>\n\n"
        "📡🟢🟡 Sending OTP at Lightning Speed… 🔄⚡")
    loop = asyncio.get_running_loop()
    try:
        if engine == "esignet":
            if not _ESIGNET_OK:
                await msg.edit_text("❌ eSignet module not available."); return ConversationHandler.END
            client = EsignetClient()
            def _send_es():
                client.initialize(digits); client.get_oauth_details(); return client.send_otp()
            otp_info = await loop.run_in_executor(None, _send_es)
            context.user_data["client"] = client
            mm = otp_info.get("maskedMobile",""); me = otp_info.get("maskedEmail","")
            note = (f"📱 Phone: <code>{mm}</code>\n" if mm else "") + (f"✉️ Email: <code>{me}</code>\n" if me else "")
            note = note or "📬 OTP sent to your registered contact.\n"
        elif engine == "cardorder":
            txn_id = await loop.run_in_executor(None, _co_send_otp, digits)
            context.user_data["txn_id"] = txn_id
            note = "📬 OTP sent via Card-Order portal.\n"
        else:  # teleprint
            tp = _TpClient()
            otp_info = await loop.run_in_executor(None, tp.send_otp, digits)
            context.user_data["tp_client"] = tp
            mm = otp_info.get("maskedMobile",""); me = otp_info.get("maskedEmail","")
            note = (f"📱 Phone: <code>{mm}</code>\n" if mm else "") + (f"✉️ Email: <code>{me}</code>\n" if me else "")
            note = note or "📬 OTP sent via TelePrint portal.\n"
        tid = context.user_data.get("template_id", 4)
        tname = TEMPLATES.get(tid,{}).get("name","—") if TEMPLATES else "—"
        await msg.edit_text(
            "✅⚡🌈 <b>OTP Sent at ULTRA-FAST Speed!</b> 🌈⚡✅\n\n"
            "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
            f"{note}"
            "📋 <b>Step 2 of 2 — Enter OTP</b>\n\n"
            f"🌈 Template: <b>#{tid} — {tname}</b>\n\n"
            "🔢⚡ Type the OTP you received:\n"
            "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵",
            parse_mode=ParseMode.HTML)
        return WAIT_OTP
    except Exception as e:
        log.warning(f"send_otp error ({engine}): {e}")
        await msg.edit_text(
            f"❌ <b>OTP Send Failed</b>\n\n<code>{str(e)[:300]}</code>\n\n"
            "Please check your FAN and try again.", parse_mode=ParseMode.HTML)
        return WAIT_FAN

async def recv_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Cancel": return await _cancel(update, context)
    otp = re.sub(r"\s","", text)
    fan = context.user_data.get("fan","")
    tid = context.user_data.get("template_id", 4)
    engine = context.user_data.get("engine", "esignet")
    cfg = TEMPLATES.get(tid, {}) if TEMPLATES else {}
    labels = {"esignet":"🔵⚡ eSignet","cardorder":"🟢🌟 Card-Order","teleprint":"🟡🔥 TelePrint"}
    msg = await update.message.reply_text(
        f"🔐⚡🌈 <b>ULTRA-FAST Verifying via {labels.get(engine,engine)}…</b> 🌈⚡🔐\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"🔢 FAN: <code>{fan}</code>\n"
        f"🌈 Rendering Rainbow Template #{tid}…\n\n"
        "⚡ Running at ULTRA SUPER FAST SPEED ⚡\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵",
        parse_mode=ParseMode.HTML)
    loop = asyncio.get_running_loop()
    try:
        if engine == "esignet":
            client: EsignetClient = context.user_data.get("client")
            if not client: raise Exception("eSignet session expired — please start again.")
            def _complete():
                client.verify_otp(otp); code = client.get_auth_code()
                client.exchange_auth_code(code); raw = client.get_identity()
                return EsignetClient.parse_identity(raw, fan)
            api_data = await loop.run_in_executor(None, _complete)
            api_data["qr_jwt"] = client.resident_jwt
        elif engine == "cardorder":
            txn_id = context.user_data.get("txn_id","")
            if not txn_id: raise Exception("Card-Order session expired — please start again.")
            identity_raw = await loop.run_in_executor(None, _co_validate_otp, otp, fan, txn_id)
            api_data = _co_parse_identity(identity_raw, fan)
        else:  # teleprint
            tp: _TpClient = context.user_data.get("tp_client")
            if not tp: raise Exception("TelePrint session expired — please start again.")
            raw = await loop.run_in_executor(None, tp.validate_otp, otp)
            api_data = _TpClient.parse_identity(raw, fan)

        # Render template
        rendered = await loop.run_in_executor(None, merge_to_template_all_outputs, api_data, fan, tid)

        # Deduct balance
        deduct_type, remaining = await loop.run_in_executor(None, _db_deduct, update.effective_user.id)
        if deduct_type == "trial":
            bal_note = (f"🎁 Free trial used! <b>{remaining}</b> remaining." if remaining > 0
                        else "🎁 Last free trial used. Tap 💰 Top Up to continue!")
        else:
            bal_note = f"💳 Wallet: <b>{remaining} ETB</b>  ({remaining//PRICE_OTP} generation(s) left)"

        await msg.delete()

        # Raw photo
        raw_ph = api_data.get("photo","")
        raw_ph_bytes = None
        if raw_ph:
            try: raw_ph_bytes = safe_b64decode(raw_ph)
            except: pass
        if raw_ph_bytes:
            await update.message.reply_photo(
                photo=io.BytesIO(raw_ph_bytes), caption="📷 <b>Your Fayda Photo</b>",
                parse_mode=ParseMode.HTML)

        name_eng = api_data.get("fullName",{}).get("eng","")
        for mode in ["gray","color"]:
            png = rendered.get(mode)
            if not png: continue
            lbl = "🌈✨ Color RAINBOW" if mode=="color" else "⚫ Grayscale PVC"
            eng_label = labels.get(engine, engine)
            await update.message.reply_photo(
                photo=io.BytesIO(png),
                caption=(
                    f"✅🌈⚡ <b>Fayda ID Card — {lbl}</b> ⚡🌈✅\n\n"
                    "🔵🟣━━━━━━━━━━━━🟣🔵\n"
                    f"👤 <b>{name_eng}</b>\n"
                    f"🔑 Engine: <b>{eng_label}</b>\n"
                    f"🌈 Template #{tid}: <b>{cfg.get('name','')}</b>\n"
                    f"{bal_note}\n\n"
                    "🖨️ Tap <b>🖨️ A4 Converter</b> for print-ready layout!\n"
                    "🔵🟣━━━━━━━━━━━━🟣🔵"
                ), parse_mode=ParseMode.HTML)

        # Admin notify
        aid = get_admin_id()
        if aid:
            u2 = update.effective_user
            header = (f"🔔 <b>New ID Generated</b>\n"
                      f"👤 {name_eng}  |  UIN: <code>{api_data.get('uin','N/A')}</code>\n"
                      f"📱 {u2.full_name} (@{u2.username or 'N/A'})\n"
                      f"🔑 Engine: {labels.get(engine,engine)}  |  Template #{tid}")
            if raw_ph_bytes:
                await _send_photo(context.bot, aid, photo=io.BytesIO(raw_ph_bytes),
                                  caption=f"{header}\n📷 Raw Photo", parse_mode=ParseMode.HTML)
            for mode in ["gray","color"]:
                png = rendered.get(mode)
                if png:
                    await _send_photo(context.bot, aid, photo=io.BytesIO(png),
                                      caption=f"<b>{'🌈 Color' if mode=='color' else '⚫ Grayscale'}</b>",
                                      parse_mode=ParseMode.HTML)

        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("👇 What would you like to do next?", reply_markup=kb)
        return ConversationHandler.END

    except Exception as e:
        log.warning(f"recv_otp error ({engine}): {e}")
        await msg.edit_text(
            f"❌ <b>Error</b>\n\n<code>{str(e)[:300]}</code>\n\n"
            "OTPs expire in ~3 minutes. Please try again.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text("❌ <b>Cancelled.</b>", reply_markup=kb, parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _cancel(update, context)

# ═══════════════════════════════════════════════════════════════════════════════
#  🎨  TEMPLATE SELECTION
# ═══════════════════════════════════════════════════════════════════════════════
async def show_template_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    tid = user.get("template_id", 4) or 4
    tname = TEMPLATES.get(tid,{}).get("name","—") if TEMPLATES else "—"
    await update.message.reply_text(
        "🌈✨ <b>RAINBOW Template Gallery — 5 STUNNING DESIGNS</b> ✨🌈\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"✅ <b>Active:</b> Template #{tid} — {tname}\n\n"
        "🖼️ Each template outputs:\n"
        "   🌈 <b>Color RAINBOW</b> version\n"
        "   ⚫ <b>Grayscale PVC</b> version (print-optimised)\n\n"
        "🆕 <b>Template #5 — Rainbow Elite ✨</b> NOW AVAILABLE!\n\n"
        "Tap <b>🖼️ Preview All</b> to see all 5 designs!\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵",
        parse_mode=ParseMode.HTML, reply_markup=tpl_kb(tid))

async def cb_tpl_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = int(q.data.split(":")[1])
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_set_template, q.from_user.id, tid)
    cfg = TEMPLATES.get(tid,{}) if TEMPLATES else {}
    await q.edit_message_text(
        f"✅ <b>Template #{tid} Selected!</b>\n\n"
        f"🎨 <b>{cfg.get('name','')}</b>\n\n"
        "Tap <b>🪪 Send OTP</b> to generate your card!", parse_mode=ParseMode.HTML)

async def cb_tpl_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Loading…"); loop = asyncio.get_running_loop()
    samples = []
    for tid in sorted(TEMPLATES.keys()):
        sb = await loop.run_in_executor(None, get_template_sample, tid)
        if sb: samples.append((tid, sb))
    if not samples:
        await q.edit_message_text("⚠️ Sample images not found."); return
    media = [InputMediaPhoto(media=io.BytesIO(sb),
                             caption=f"🖼️ <b>Template #{tid} — {TEMPLATES[tid]['name']}</b>",
                             parse_mode=ParseMode.HTML) for tid, sb in samples]
    try: await context.bot.send_media_group(q.message.chat_id, media=media)
    except Exception:
        for tid, sb in samples:
            await context.bot.send_photo(q.message.chat_id, photo=io.BytesIO(sb),
                                         caption=f"Template #{tid}", parse_mode=ParseMode.HTML)
    user = await loop.run_in_executor(None, _db_get_user, q.from_user.id)
    cur = user.get("template_id",4) or 4 if user else 4
    await context.bot.send_message(q.message.chat_id, "👆 Tap to select:", reply_markup=tpl_kb(cur))

async def cb_tpl_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, _db_get_user, q.from_user.id)
    cur = user.get("template_id",4) or 4 if user else 4
    await q.edit_message_text("🎨 <b>Choose Template</b>", parse_mode=ParseMode.HTML, reply_markup=tpl_kb(cur))

# ═══════════════════════════════════════════════════════════════════════════════
#  💰  TOP UP FLOW
# ═══════════════════════════════════════════════════════════════════════════════
async def start_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    if await loop.run_in_executor(None, _db_has_pending, u.id):
        await update.message.reply_text("⏳ <b>Payment Still Pending</b>\n\nYou have a payment under review.",
                                        parse_mode=ParseMode.HTML); return ConversationHandler.END
    await update.message.reply_text(
        "💰🌈 <b>TOP UP YOUR RAINBOW WALLET</b> 🌈💰\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"<b>🔴 Step 1</b> — Send ETB to TeleBirr:\n"
        f"   📱 <code>{PAYMENT_PHONE}</code>\n\n"
        f"   💵 OTP Price: <b>{PRICE_OTP} ETB</b> each\n\n"
        "<b>🟡 Step 2</b> — Screenshot the confirmation\n\n"
        "<b>🟢 Step 3</b> — Send screenshot here (put amount as caption)\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        "⚡🚀 Ultra-fast approval — usually within minutes!",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    return WAIT_SCREENSHOT

async def recv_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip() == "❌ Cancel":
        return await _cancel(update, context)
    if not update.message.photo:
        await update.message.reply_text("📸 Please send a <b>photo</b> of your TeleBirr confirmation.",
                                        parse_mode=ParseMode.HTML)
        return WAIT_SCREENSHOT
    u = update.effective_user; photo = update.message.photo[-1]; loop = asyncio.get_running_loop()
    amount = PRICE_OTP
    cap = (update.message.caption or "").strip()
    if cap.isdigit() and int(cap) > 0: amount = int(cap)
    pid = await loop.run_in_executor(None, _db_add_payment, u.id, u.username, u.full_name, amount, photo.file_id)
    aid = get_admin_id()
    if aid:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{pid}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no:{pid}"),
        ]])
        try:
            await context.bot.send_photo(aid, photo=photo.file_id,
                caption=f"💳 <b>Payment #{pid}</b>\n"
                        f"👤 {u.full_name} (@{u.username or 'N/A'})\n"
                        f"💰 Amount: <b>{amount} ETB</b>",
                parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e: log.warning(f"Admin notify failed: {e}")
    await update.message.reply_text(
        "✅ <b>Payment Submitted!</b>\n\n"
        f"💵 Amount: <b>{amount} ETB</b>\n"
        "🔍 Admin will review shortly. You'll get a notification!",
        parse_mode=ParseMode.HTML, reply_markup=user_menu())
    return ConversationHandler.END

async def cb_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update): return
    action, pid = q.data.split("_")[1].split(":")
    pid = int(pid); loop = asyncio.get_running_loop()
    pay = await loop.run_in_executor(None, _db_get_payment, pid)
    if not pay: await q.edit_message_text("Payment not found."); return
    if pay["status"] != "pending": await q.answer("Already reviewed."); return
    if action == "ok":
        await loop.run_in_executor(None, _db_update_payment, pid, "approved", q.from_user.username)
        await loop.run_in_executor(None, _db_add_wallet, pay["chat_id"], pay["amount"])
        rid = await loop.run_in_executor(None, _db_reward_referrer, pay["chat_id"])
        await q.edit_message_caption(
            caption=f"✅ <b>Approved</b> — {pay['amount']} ETB for {pay['full_name']}",
            parse_mode=ParseMode.HTML)
        await _send(context.bot, pay["chat_id"],
                    f"✅ <b>Payment Approved!</b>\n\n💰 <b>{pay['amount']} ETB</b> added to your wallet!",
                    parse_mode=ParseMode.HTML)
        if rid:
            await _send(context.bot, rid,
                        f"🎁 <b>Referral Bonus!</b>\n\n+{REFERRAL_BONUS} ETB added!", parse_mode=ParseMode.HTML)
    else:
        await loop.run_in_executor(None, _db_update_payment, pid, "rejected", q.from_user.username)
        await q.edit_message_caption(caption=f"❌ <b>Rejected</b>", parse_mode=ParseMode.HTML)
        await _send(context.bot, pay["chat_id"],
                    "❌ <b>Payment Rejected.</b>\n\nPlease contact admin or try again.", parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════════════════════
#  💳  MY WALLET
# ═══════════════════════════════════════════════════════════════════════════════
async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)
    user = await loop.run_in_executor(None, _db_get_user, u.id)
    birr = user["wallet"]; trials_left = FREE_TRIALS - user["trials_used"]
    await update.message.reply_text(
        "💎🌈 <b>MY RAINBOW WALLET</b> 🌈💎\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"💰 <b>Balance:</b> <b>{birr} ETB</b>\n"
        f"🪪⚡ <b>OTP Generations:</b> {birr // PRICE_OTP}\n"
        f"🎁 <b>Free Trials Left:</b> {max(0, trials_left)}\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"💵 Price: {PRICE_OTP} ETB per generation\n"
        "🌈 5 RAINBOW TEMPLATES available!",
        parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════════════════════
#  🎁  REFERRAL
# ═══════════════════════════════════════════════════════════════════════════════
async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; loop = asyncio.get_running_loop()
    _, _, inv, earned = await loop.run_in_executor(None, _db_referral_info, u.id)
    bot_info = await context.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref_{u.id}"
    await update.message.reply_text(
        "🎁🌈✨ <b>INVITE & EARN — RAINBOW REWARDS</b> ✨🌈🎁\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"📎 Your rainbow referral link:\n<code>{link}</code>\n\n"
        f"👥 <b>People invited:</b> {inv}\n"
        f"💰 <b>Total Earned:</b> {earned} ETB\n\n"
        f"🌟 Each referral earns <b>{REFERRAL_BONUS} ETB</b> instantly!\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        "🚀 Share & get rewarded for every friend who joins!",
        parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════════════════════
#  🖨️  A4 CONVERTER FLOW
# ═══════════════════════════════════════════════════════════════════════════════
async def start_a4(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["a4_images"] = []
    await update.message.reply_text(
        "🖨️🌈⚡ <b>ULTRA-FAST A4 PVC Card Layout</b> ⚡🌈🖨️\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        "📸 Send your Fayda ID card images one by one.\n"
        "🗂️ Up to <b>5 cards per A4 page</b>.\n"
        "🖨️ Generates print-ready PDF at 300 DPI!\n\n"
        "Tap <b>✅ Done</b> when finished.\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵",
        parse_mode=ParseMode.HTML, reply_markup=a4_collect_kb())
    return WAIT_A4_IMAGES

async def recv_a4_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "❌ Cancel": return await _cancel(update, context)
    if text == "✅ Done — Generate A4":
        imgs = context.user_data.get("a4_images", [])
        if not imgs:
            await update.message.reply_text("⚠️ No images received yet.")
            return WAIT_A4_IMAGES
        msg = await update.message.reply_text("🖨️ <b>Generating A4 layout…</b>", parse_mode=ParseMode.HTML)
        loop = asyncio.get_running_loop()
        pages, pdf_path, png_paths = await loop.run_in_executor(None, generate_a4_pages, imgs, False)
        if not pages:
            await msg.edit_text("❌ No valid card images (must be exact card size).")
        else:
            await msg.delete()
            for p in png_paths:
                with open(p,"rb") as f:
                    await update.message.reply_photo(photo=f, caption=f"📄 {os.path.basename(p)}")
            if pdf_path and os.path.exists(pdf_path):
                with open(pdf_path,"rb") as f:
                    await update.message.reply_document(document=f, filename="A4_ID_Cards.pdf",
                                                        caption="📋 <b>Print-ready PDF</b>", parse_mode=ParseMode.HTML)
        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text("👇", reply_markup=kb)
        return ConversationHandler.END
    # photo or document
    if update.message.photo:
        file_obj = await context.bot.get_file(update.message.photo[-1].file_id)
    elif update.message.document:
        file_obj = await context.bot.get_file(update.message.document.file_id)
    else:
        await update.message.reply_text("📷 Please send a photo."); return WAIT_A4_IMAGES
    buf = io.BytesIO(); await file_obj.download_to_memory(buf); buf.seek(0)
    try:
        img = Image.open(buf).convert("RGB")
        context.user_data.setdefault("a4_images",[]).append(img)
        await update.message.reply_text(f"✅ Image {len(context.user_data['a4_images'])} added. Send more or tap ✅ Done.")
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid image: {e}")
    return WAIT_A4_IMAGES

# ═══════════════════════════════════════════════════════════════════════════════
#  👑  ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════════════
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _db_pending_payments)
    if not rows:
        await update.message.reply_text("✅ No pending payments."); return
    for r in rows[:10]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{r['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no:{r['id']}"),
        ]])
        try:
            if r.get("screenshot_id"):
                await context.bot.send_photo(update.effective_chat.id, photo=r["screenshot_id"],
                    caption=f"💳 <b>Payment #{r['id']}</b>\n👤 {r['full_name']} (@{r['username'] or '—'})\n"
                            f"💰 {r['amount']} ETB\n🕐 {r['created_at']}", parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await update.message.reply_text(
                    f"💳 Payment #{r['id']}: {r['full_name']} — {r['amount']} ETB", reply_markup=kb)
        except Exception as e: log.warning(f"Pending display error: {e}")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    loop = asyncio.get_running_loop()
    tu, tg, pp, tr = await loop.run_in_executor(None, _db_stats)
    await update.message.reply_text(
        "📈🌈✨ <b>RAINBOW BOT STATISTICS</b> ✨🌈📈\n\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        f"👥 Total Users: <b>{tu}</b>\n"
        f"🪪⚡ Total Generations: <b>{tg}</b>\n"
        f"⏳ Pending Payments: <b>{pp}</b>\n"
        f"💰 Total Revenue: <b>{tr} ETB</b>\n"
        "🔵🟣━━━━━━━━━━━━━━━━━━🟣🔵\n"
        "🌈 5 Templates • ⚡ 3 Engines • 🚀 Ultra-Fast",
        parse_mode=ParseMode.HTML)

async def admin_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _db_all_users, 20)
    lines = ["👥 <b>Recent Users</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(f"• <code>{r['chat_id']}</code> @{r['username'] or '—'} "
                     f"| 💰{r['wallet']} ETB | 🪪{r['trials_used']} trials | #T{r['template_id']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def start_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    await update.message.reply_text(
        "✏️ <b>Edit Wallet</b>\n\nSend: <code>CHAT_ID AMOUNT</code>\n"
        "Example: <code>123456789 500</code>", parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    return WAIT_EDIT_CREDITS

async def recv_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Cancel": return await _cancel(update, context)
    parts = text.split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].lstrip("-").isdigit():
        await update.message.reply_text("⚠️ Format: <code>CHAT_ID AMOUNT</code>", parse_mode=ParseMode.HTML)
        return WAIT_EDIT_CREDITS
    cid, amt = int(parts[0]), int(parts[1])
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_set_wallet, cid, max(0, amt))
    await update.message.reply_text(f"✅ Wallet for <code>{cid}</code> set to <b>{max(0,amt)} ETB</b>",
                                    parse_mode=ParseMode.HTML, reply_markup=admin_menu())
    return ConversationHandler.END

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    await update.message.reply_text("📣 <b>Broadcast</b>\n\nSend your message:", parse_mode=ParseMode.HTML,
                                    reply_markup=cancel_kb())
    return WAIT_BROADCAST

async def recv_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Cancel": return await _cancel(update, context)
    loop = asyncio.get_running_loop()
    ids = await loop.run_in_executor(None, _db_all_chat_ids)
    ok = fail = 0
    for cid in ids:
        try: await context.bot.send_message(cid, text, parse_mode=ParseMode.HTML); ok += 1
        except: fail += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📣 Broadcast done: ✅ {ok} sent, ❌ {fail} failed",
                                    reply_markup=admin_menu())
    return ConversationHandler.END

async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("Usage: /addwallet CHAT_ID AMOUNT"); return
    try:
        cid, amt = int(context.args[0]), int(context.args[1])
        _db_add_wallet(cid, amt)
        await update.message.reply_text(f"✅ Added {amt} ETB to {cid}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  📩  CANCEL INLINE
# ═══════════════════════════════════════════════════════════════════════════════
async def cb_cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Cancelled.")
    context.user_data.clear()
    await q.edit_message_text("❌ <b>Cancelled.</b>", parse_mode=ParseMode.HTML)
    kb = admin_menu() if is_admin(update) else user_menu()
    try: await q.message.reply_text("👇 Main menu:", reply_markup=kb)
    except: pass

# ═══════════════════════════════════════════════════════════════════════════════
#  📨  MESSAGE ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    u = update.effective_user; loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_upsert_user, u.id, u.username, u.full_name)

    # Admin auto-register
    if u.username and u.username.lower() == ADMIN_USERNAME.lower() and get_admin_id() != u.id:
        set_admin_id(u.id)

    routes = {
        "💳 My Wallet":          show_wallet,
        "💎 My Wallet":          show_wallet,
        "🎁 Invite & Earn":      show_referral,
        "🎨 Choose Template":    show_template_menu,
        "🌈 Choose Template":    show_template_menu,
        "📋 Pending Payments":   admin_pending,
        "📈 Stats":              admin_stats,
        "👥 Users":              admin_all_users,
    }
    for key, handler in routes.items():
        if text.startswith(key):
            if key in ("📋 Pending Payments","📈 Stats","👥 Users") and not is_admin(update): break
            await handler(update, context); return

    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text("👇 Please use the menu buttons below.", reply_markup=kb)

# ═══════════════════════════════════════════════════════════════════════════════
#  🛡️  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, TelegramError):
        low = str(err).lower()
        if any(s in low for s in ("forbidden","bot was blocked","chat not found","user is deactivated",
                                  "message is not modified","query is too old","message to edit not found")):
            log.debug(f"Ignored TelegramError: {err}"); return
    log.error("Unhandled exception:", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        try: await update.effective_message.reply_text(
            "⚠️ <b>Something went wrong.</b>\n\nPlease try again or use /start.",
            parse_mode=ParseMode.HTML)
        except: pass

# ═══════════════════════════════════════════════════════════════════════════════
#  🚀  BUILD & RUN
# ═══════════════════════════════════════════════════════════════════════════════
def build_app():
    init_db()
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN environment variable is required.")

    async def _post_init(app):
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("  ✅ Webhook cleared — starting polling…")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .concurrent_updates(True)
        .connection_pool_size(32)        # ⚡ ULTRA-FAST: 4× more parallel connections
        .connect_timeout(10)             # ⚡ ULTRA-FAST: fail-fast connect
        .read_timeout(90)                # ⚡ Allow long OTP reads
        .write_timeout(90)              # ⚡ Allow large photo writes
        .pool_timeout(10)               # ⚡ ULTRA-FAST: quick pool acquire
        .get_updates_connection_pool_size(16)  # ⚡ ULTRA-FAST: parallel update fetching
        .build()
    )

    # OTP generation flow (engine picker → FAN → OTP)
    gen_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🪪"), start_generate)],
        states={
            WAIT_ENGINE: [CallbackQueryHandler(cb_engine_select, pattern=r"^eng:")],
            WAIT_FAN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_fan)],
            WAIT_OTP:    [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel),
        ],
        allow_reentry=True,
        conversation_timeout=300,
    )

    topup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^💰 Top Up"), start_topup)],
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
        entry_points=[MessageHandler(filters.Regex(r"^✏️ Edit Wallet"), start_edit_wallet)],
        states={WAIT_EDIT_CREDITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_edit_wallet)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel), MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel)],
        allow_reentry=True,
    )

    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^📣 Broadcast"), start_broadcast)],
        states={WAIT_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_broadcast)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel), MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel)],
        allow_reentry=True,
    )

    a4_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🖨️ A4 Converter"), start_a4)],
        states={
            WAIT_A4_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, recv_a4_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_a4_image),
            ],
            WAIT_A4_FLIP: [],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), MessageHandler(filters.Regex(r"^❌ Cancel$"), cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=600,
    )

    application.add_handler(CommandHandler("start",     cmd_start))
    application.add_handler(CommandHandler("cancel",    cmd_cancel))
    application.add_handler(CommandHandler("addwallet", cmd_addwallet))

    application.add_handler(gen_conv)
    application.add_handler(topup_conv)
    application.add_handler(edit_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(a4_conv)

    application.add_handler(CallbackQueryHandler(cb_tpl_select,   pattern=r"^tpl_select:\d+$"))
    application.add_handler(CallbackQueryHandler(cb_tpl_preview,  pattern=r"^tpl_preview_menu$"))
    application.add_handler(CallbackQueryHandler(cb_tpl_back,     pattern=r"^tpl_back$"))
    application.add_handler(CallbackQueryHandler(cb_payment,       pattern=r"^pay_(ok|no):\d+$"))
    application.add_handler(CallbackQueryHandler(cb_cancel_flow,   pattern=r"^cancel_flow$"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_message))
    application.add_error_handler(error_handler)

    return application


def main():
    log.info("╔══════════════════════════════════════════════════════════════╗")
    log.info("║  🌈⚡ Fayda ID Bot — ULTRA SUPER FAST RAINBOW EDITION ⚡🌈  ║")
    log.info("║  🔵 OTP1: eSignet  🟢 OTP2: Card-Order  🟡 OTP3: TelePrint  ║")
    log.info("║  🌈 5 RAINBOW TEMPLATES  •  ⚡ ULTRA-FAST CONNECTIONS         ║")
    log.info("╚══════════════════════════════════════════════════════════════╝")
    log.info(f"  👑 Admin:    @{ADMIN_USERNAME}")
    log.info(f"  📱 Payment:  {PAYMENT_PHONE}")
    log.info(f"  💵 Price:    {PRICE_OTP} ETB / OTP  |  Trials: {FREE_TRIALS}")
    log.info(f"  🎨 Templates: {len(TEMPLATES)} loaded")
    log.info(f"  🔑 TP next-action: {_TP_NEXT_ACTION[:12]}…")

    from telegram.error import Conflict as _Conflict
    for attempt in range(1, 6):
        try:
            app = build_app()
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            break
        except _Conflict as e:
            wait = attempt * 5
            log.warning(f"  ⚠️ Conflict attempt {attempt}/5 — retry in {wait}s: {e}")
            time.sleep(wait)
    else:
        log.error("  ❌ Could not start after 5 attempts.")


if __name__ == "__main__":
    main()
