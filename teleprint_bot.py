# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║      🇪🇹  FAYDA ID BOT  ✦  TELEPRINT EDITION  ✦  RAILWAY READY      ║
║                                                                        ║
║  ✦ Powered by teleprint.fayda.et Next.js Server Actions              ║
║  ✦ Smart OTP Flow  •  4 Templates  •  Color + Grayscale              ║
║  ✦ Wallet System  •  Referrals  •  A4 Converter                      ║
║  ✦ Admin Dashboard  •  Broadcast  •  Payment Management              ║
╚══════════════════════════════════════════════════════════════════════╝

API Flow (from etsample.txt):
  1. POST https://teleprint.fayda.et/home
     next-action: 70a3ccf5964dca07f7d3a397fa6925fe7b6c2b9c82
     payload: [{"id":"","version":"1.0.0","requesttime":null,"metadata":{},
                "request":{"fcn":"<FAN>","isReprint":false,"isOrderEdit":false}},
               "/otpService/getToken", true]
     → {"status":200,"response":{"transactionId":"...","maskedMobile":"XXXXXX..."}}

  2. POST https://teleprint.fayda.et/home
     next-action: 70a3ccf5964dca07f7d3a397fa6925fe7b6c2b9c82
     payload: [{"id":"","version":"1.0.0","requesttime":"<ISO>",
                "request":{"token":"<OTP>","fcn":"<FAN>",
                            "transactionId":"<txnId>",
                            "isReprint":false,"isOrderEdit":false}},
               "/otpService/validateToken", true]
     → Base64-encoded JSON with identity + photo
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
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Try to import template helpers from app.py (same as original bot)
try:
    from app import (
        TEMPLATES,
        get_template_sample,
        merge_to_template_all_outputs,
        safe_b64decode,
    )
    _APP_ENABLED = True
    logging.getLogger("teleprint_bot").info("✅ app.py loaded successfully.")
except Exception as _app_err:
    _APP_ENABLED = False
    TEMPLATES = {}
    logging.getLogger("teleprint_bot").error(
        f"❌ app.py import FAILED — card generation disabled: {_app_err}"
    )

    def get_template_sample(tid):
        return None

    def merge_to_template_all_outputs(data, unique_id, template_id=1):
        return {}

    def safe_b64decode(s):
        try:
            if isinstance(s, str):
                s = s.encode()
            padding = 4 - len(s) % 4
            if padding != 4:
                s += b"=" * padding
            return base64.b64decode(s)
        except Exception:
            return None

# PDF generator — graceful no-op if unavailable
try:
    from pdf_gen import generate_fayda_pdf as _gen_pdf
    _PDF_ENABLED = True
except Exception as _pdf_err:
    _PDF_ENABLED = False
    _gen_pdf = None


# ═══════════════════════════════════════════════════════════════════════
#  ⚙️  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "dhtechs_admin")
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0"))
PAYMENT_PHONE  = os.getenv("PAYMENT_PHONE", "0919545335")

PRICE_OTP      = int(os.getenv("PRICE_OTP", "10"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "10"))
FREE_TRIALS    = int(os.getenv("FREE_TRIALS", "1"))

DB_PATH        = os.getenv("DB_PATH", "teleprint_bot.db")
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
log = logging.getLogger("teleprint_bot")


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


# ═══════════════════════════════════════════════════════════════════════
#  🌐  TELEPRINT API CLIENT  (mirrors etsample.txt exactly)
# ═══════════════════════════════════════════════════════════════════════

_TELEPRINT_HOST   = "teleprint.fayda.et"
_TELEPRINT_URL    = f"https://{_TELEPRINT_HOST}/home"

# ⚠️  If you get "Identity data not found in validateToken response", this hash
# is likely stale.  To refresh: open teleprint.fayda.et in a browser → DevTools
# → Network tab → trigger an OTP request → copy the `next-action` header value.
_NEXT_ACTION_ID   = os.getenv(
    "NEXT_ACTION_ID",
    "70a3ccf5964dca07f7d3a397fa6925fe7b6c2b9c82",
)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
    "Gecko/20100101 Firefox/155.0"
)

_ROUTER_STATE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22home%22%2C%7B%22children%22%3A"
    "%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%2Ctrue%5D"
)


class TelePrintError(Exception):
    def __init__(self, code: str, message: str):
        self.code    = code
        self.message = message
        super().__init__(f"[{code}] {message}")

    def __str__(self):
        return self.message


class TelePrintClient:
    """
    One instance per Telegram user conversation.
    Implements the two-step OTP flow observed in etsample.txt.
    """

    def __init__(self):
        self._sess = requests.Session()
        self._sess.headers.update({
            "User-Agent":              _BROWSER_UA,
            "Accept":                  "text/x-component",
            "Accept-Language":         "en-US,en;q=0.9",
            "Accept-Encoding":         "gzip, deflate, br, zstd",
            "Referer":                 f"https://{_TELEPRINT_HOST}/",
            "Origin":                  f"https://{_TELEPRINT_HOST}",
            "next-action":             _NEXT_ACTION_ID,
            "next-router-state-tree":  _ROUTER_STATE,
            "Sec-Fetch-Dest":          "empty",
            "Sec-Fetch-Mode":          "cors",
            "Sec-Fetch-Site":          "same-origin",
            "Priority":                "u=0",
        })
        self.fan:            str = ""
        self.transaction_id: str = ""
        self.masked_mobile:  str = ""
        self._session_id:    str = ""  # "b" field from server

    @staticmethod
    def _now_iso() -> str:
        now = datetime.now(timezone.utc)
        return f"$D{now.strftime('%Y-%m-%dT%H:%M:%S.')+f'{now.microsecond//1000:03d}Z'}"

    @staticmethod
    def _diagnose_raw_response(raw: str, context_label: str = "") -> None:
        """
        Log the first 600 chars of a raw response and emit a structured
        diagnosis so operators know exactly which failure mode occurred.

        Call this whenever _parse_response_lines() returns an incomplete result.
        """
        snippet = repr(raw[:600]) if raw else "<empty>"
        log.warning(
            f"[TelePrint] {context_label} — raw response snippet:\n{snippet}"
        )
        low = raw.lower() if raw else ""
        if not raw or len(raw.strip()) == 0:
            log.warning(
                "[TelePrint] DIAGNOSIS: Response is empty. "
                "Likely cause: session cookie was dropped, or the server returned "
                "nothing for an expired/invalid next-action hash."
            )
        elif raw.lstrip().startswith("<"):
            log.warning(
                "[TelePrint] DIAGNOSIS: Server returned HTML (not RSC stream). "
                "This almost always means _NEXT_ACTION_ID is stale. "
                "Update it via DevTools → Network → next-action header."
            )
        elif "error" in low or "unauthorized" in low or "forbidden" in low:
            log.warning(
                "[TelePrint] DIAGNOSIS: Server returned an error payload. "
                "Possible causes: invalid FAN, expired session, or stale next-action hash."
            )
        elif "t" not in low:
            log.warning(
                "[TelePrint] DIAGNOSIS: No T-blob found in response. "
                "The response format may have changed, or the session from step 1 "
                "(getToken) was not carried into step 2 (validateToken)."
            )
        else:
            log.warning(
                "[TelePrint] DIAGNOSIS: T-blob line may exist but could not be parsed. "
                "The prefix format or encoding may have changed."
            )

    def _post(self, payload: list) -> str:
        """POST to teleprint.fayda.et/home with text/plain content-type (as in etsample)."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        resp = self._sess.post(
            _TELEPRINT_URL,
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _parse_response_lines(raw_text: str) -> dict:
        """
        Parse the Next.js RSC (React Server Components) streaming format.

        Lines look like:
          0:{"a":"$@1","f":"","b":"S5--_JWg4MuEc77zdE7H3"}
          1:{"status":200,"message":"Otp send successfully!","response":{...}}
          <N>:T<hex_len>,<base64_data>   ← ANY numeric/hex prefix, not just "2"

        The T<hex> blob can appear on any prefix line (2, 3, a, 1b, …)
        depending on the API version / response size.
        """
        result = {}
        for line in raw_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                colon_pos = line.index(":")
            except ValueError:
                continue
            prefix = line[:colon_pos]
            rest   = line[colon_pos + 1:]

            # ── JSON metadata / response blocks ──────────────────────────────
            if rest.startswith("{"):
                try:
                    parsed = json.loads(rest)
                    # meta block: has "a"/"f"/"b" keys
                    if "a" in parsed and "b" in parsed and "meta" not in result:
                        result["meta"] = parsed
                    # response block: has "status"/"message"/"response"
                    elif "status" in parsed or "message" in parsed:
                        result["response"] = parsed
                    # fall back by prefix for backward compat
                    elif prefix == "0":
                        result.setdefault("meta", parsed)
                    elif prefix == "1":
                        result.setdefault("response", parsed)
                except Exception:
                    pass

            # ── T<hex>,<b64> blob — ANY prefix ───────────────────────────────
            # Format: T5e4,<base64_data>
            # hex_len is the BASE64 char count of the payload that follows
            m = re.match(r"T([0-9a-fA-F]+),(.*)", rest, re.DOTALL)
            if m:
                hex_len   = int(m.group(1), 16)
                b64_data  = m.group(2)
                json_b64  = b64_data[:hex_len]
                remainder = b64_data[hex_len:]

                try:
                    padding = (4 - len(json_b64) % 4) % 4
                    decoded_bytes = base64.b64decode(json_b64 + "=" * padding)
                    result["identity_json"] = json.loads(
                        decoded_bytes.decode("utf-8", errors="replace")
                    )
                except Exception:
                    pass

                if remainder:
                    result["extra_data"] = remainder

        return result

    # ── Step 1: Request OTP ──────────────────────────────────────────────────
    def get_token(self, fan: str) -> dict:
        """
        POST to /home with getToken action.
        Returns {"maskedMobile": "XXXXXX3037", "transactionId": "5074752334"}
        Matches etsample.txt lines 77-81.
        """
        self.fan = fan.strip()
        payload = [
            {
                "id":          "",
                "version":     "1.0.0",
                "requesttime": None,
                "metadata":    {},
                "request":     {
                    "fcn":         self.fan,
                    "isReprint":   False,
                    "isOrderEdit": False,
                },
            },
            "/otpService/getToken",
            True,
        ]

        raw  = self._post(payload)
        log.debug("[TelePrint] get_token raw response: %s", repr(raw[:300]))
        data = self._parse_response_lines(raw)

        # Grab session id from meta ("b" field)
        if "meta" in data:
            self._session_id = data["meta"].get("b", "")

        resp = data.get("response", {})
        if not resp:
            self._diagnose_raw_response(raw, "get_token — no response block")
            raise TelePrintError(
                "NO_RESPONSE",
                "No response from getToken. "
                "The next-action hash may be stale — check the logs for a diagnosis.",
            )

        status = resp.get("status", 0)
        if status != 200:
            msg = resp.get("message", f"HTTP {status}")
            raise TelePrintError("OTP_FAILED", msg)

        inner = resp.get("response", {})
        self.transaction_id = str(inner.get("transactionId", ""))
        self.masked_mobile  = str(inner.get("maskedMobile", ""))

        if not self.transaction_id:
            raise TelePrintError("NO_TXN", "transactionId missing from getToken response")

        return {
            "transactionId": self.transaction_id,
            "maskedMobile":  self.masked_mobile,
            "maskedEmail":   str(inner.get("maskedEmail", "")),
        }

    # ── Step 2: Validate OTP + get identity ──────────────────────────────────
    def validate_token(self, otp: str) -> dict:
        """
        POST to /home with validateToken action.
        Returns full identity dict (parsed from base64 JSON in the response).
        Matches etsample.txt lines 125-130.
        """
        if not self.transaction_id:
            raise TelePrintError("NO_TXN", "Call get_token() first")

        payload = [
            {
                "id":          "",
                "version":     "1.0.0",
                "requesttime": self._now_iso(),
                "request":     {
                    "token":         otp.strip(),
                    "fcn":           self.fan,
                    "transactionId": self.transaction_id,
                    "isReprint":     False,
                    "isOrderEdit":   False,
                },
            },
            "/otpService/validateToken",
            True,
        ]

        raw  = self._post(payload)
        log.debug("[TelePrint] validate_token raw response: %s", repr(raw[:300]))
        data = self._parse_response_lines(raw)

        identity_raw = data.get("identity_json")
        if not identity_raw:
            self._diagnose_raw_response(raw, "validate_token — T-blob missing")
            # Provide a user-facing message that differs by failure mode so that
            # the bot user gets actionable feedback, not just a generic error.
            low = raw.lower() if raw else ""
            if not raw or len(raw.strip()) == 0:
                user_msg = (
                    "The Fayda server returned an empty response. "
                    "Your session may have expired — please start again."
                )
            elif raw.lstrip().startswith("<"):
                user_msg = (
                    "The Fayda server returned an unexpected page (HTML). "
                    "The service may be temporarily unavailable or the bot needs "
                    "an update. Please try again later or contact the admin."
                )
            elif "error" in low or "unauthorized" in low:
                user_msg = (
                    "The OTP was rejected by the Fayda server. "
                    "Please check the code and try again."
                )
            else:
                user_msg = (
                    "Identity data not found in the server response. "
                    "The OTP may have expired — please start again."
                )
            raise TelePrintError("NO_IDENTITY", user_msg)

        return identity_raw

    # ── Identity parser ───────────────────────────────────────────────────────
    @staticmethod
    def parse_identity(raw: dict, fan: str) -> dict:
        """
        Normalize the teleprint.fayda.et identity response to the format
        expected by app.py's merge_to_template().

        Response structure (from etsample.txt decode):
        {
          "id": "mosip.id.read",
          "version": "v1",
          "responsetime": "2026-09-05T02:24:27.666499225Z",
          "response": {
            "status": null,
            "identity": {
              "fullName": [{"language":"eng","value":"Mulusew Dires Alebachew"}, ...],
              "gender":   [{"language":"eng","value":"Male"}, ...],
              "dateOfBirth": "2002/07/19",
              "phone": "0941933037",
              "uin": "7694360132677",
              "region": [...], "zone": [...], "woreda": [...],
              "residenceStatus": [...],
              "individualBiometrics": {"format":"cbeff","value":"individualBiometrics_bio_CBEFF"},
              ...
            },
            "documents": null,
            "verifiedAttributes": null
          },
          "errors": []
        }
        """

        def _bilingual(field) -> dict:
            if isinstance(field, list):
                eng = next((x.get("value","") for x in field if x.get("language") == "eng"), "")
                amh = next((x.get("value","") for x in field if x.get("language") == "amh"), "")
                return {"eng": eng, "amh": amh}
            if isinstance(field, dict):
                return {"eng": field.get("eng",""), "amh": field.get("amh","")}
            return {"eng": str(field or ""), "amh": ""}

        # Check for errors first
        errors = raw.get("errors", [])
        if errors:
            e = errors[0]
            raise TelePrintError(
                e.get("errorCode", "ERR"),
                e.get("errorMessage", str(e)),
            )

        # Navigate: raw → response → identity
        resp_block = raw.get("response", {}) or {}
        identity   = resp_block.get("identity", {}) or {}

        if not identity:
            raise TelePrintError("NO_IDENTITY", "Identity block missing in API response")

        # ── Photo ────────────────────────────────────────────────────────────
        # individualBiometrics.value is often a CBEFF stub string, not real base64.
        # We only accept it if it looks like actual base64 image data (long, no spaces).
        photo_b64 = ""
        biometrics = identity.get("individualBiometrics", {}) or {}
        if isinstance(biometrics, dict):
            _bio_val = biometrics.get("value", "") or ""
            # Real photo b64 is long; CBEFF stub is a short descriptive string
            if len(_bio_val) > 200 and " " not in _bio_val:
                photo_b64 = _bio_val
        # Fall back to direct photo fields
        if not photo_b64:
            photo_b64 = (
                identity.get("photo") or identity.get("photograph") or
                identity.get("face") or ""
            )
        # Final guard: reject anything that is clearly not base64
        if photo_b64 and (len(photo_b64) < 100 or " " in photo_b64):
            photo_b64 = ""

        # ── UIN ──────────────────────────────────────────────────────────────
        uin = str(identity.get("UIN") or identity.get("uin") or identity.get("uid") or "")

        # ── Full name ────────────────────────────────────────────────────────
        fullName = _bilingual(identity.get("fullName") or identity.get("name") or "")

        # ── Date of birth ────────────────────────────────────────────────────
        dob = str(identity.get("dateOfBirth") or identity.get("dob") or "")

        # ── Gender ───────────────────────────────────────────────────────────
        gender = _bilingual(identity.get("gender") or "")

        # ── Phone & email ────────────────────────────────────────────────────
        phone = str(identity.get("phone") or identity.get("phoneNumber") or "")
        email = str(identity.get("email") or "")

        # ── Address ──────────────────────────────────────────────────────────
        region = _bilingual(identity.get("region") or "")
        zone   = _bilingual(identity.get("zone")   or "")
        woreda = _bilingual(identity.get("woreda") or "")

        # ── Nationality ──────────────────────────────────────────────────────
        nat_raw = identity.get("residenceStatus") or identity.get("nationality") or ""
        if nat_raw:
            nat = _bilingual(nat_raw)
            if not nat["eng"]:
                nat = {"eng": str(nat_raw), "amh": "ኢትዮጵያዊ"}
        else:
            nat = {"eng": "Ethiopian", "amh": "ኢትዮጵያዊ"}

        return {
            "fullName":        fullName,
            "dateOfBirth":     dob,
            "gender":          gender,
            "phone":           phone,
            "uin":             uin,
            "fin":             uin,
            "uniqueId":        fan,
            "region":          region,
            "zone":            zone,
            "woreda":          woreda,
            "residenceStatus": nat,
            "email":           email,
            "photo":           photo_b64,
            "qr_jwt":          "",
        }


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
            template_id         INTEGER DEFAULT 2,
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
            template_id  INTEGER DEFAULT 3,
            generated_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS global_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO global_settings (key, value) VALUES ('admin_chat_id', '0');
    """)
    for migration in [
        "ALTER TABLE users ADD COLUMN template_id         INTEGER DEFAULT 3",
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
    row  = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
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


# ═══════════════════════════════════════════════════════════════════════
#  🖨️  A4 CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════
def _draw_crop_marks(draw, x, y):
    c = "black"
    w = CROP_WIDTH_PX
    o = CROP_OFFSET
    L = CROP_LENGTH
    cw, ch = CARD_WIDTH, CARD_HEIGHT
    draw.line((x - o, y - L, x - o, y - o),             fill=c, width=w)
    draw.line((x - L, y - o, x - o, y - o),             fill=c, width=w)
    draw.line((x + cw + o, y - L, x + cw + o, y - o),   fill=c, width=w)
    draw.line((x + cw + o, y - o, x + cw + L, y - o),   fill=c, width=w)
    draw.line((x - o, y + ch + o, x - o, y + ch + L),   fill=c, width=w)
    draw.line((x - L, y + ch + o, x - o, y + ch + o),   fill=c, width=w)
    draw.line((x + cw + o, y + ch + o, x + cw + o, y + ch + L), fill=c, width=w)
    draw.line((x + cw + o, y + ch + o, x + cw + L, y + ch + o), fill=c, width=w)


def _draw_center_marks(draw):
    cx, cy = A4_WIDTH // 2, A4_HEIGHT // 2
    w = CROP_WIDTH_PX
    L = CENTER_MARK_LEN
    draw.line((cx, 0, cx, L),                       fill="black", width=w)
    draw.line((cx, A4_HEIGHT - L, cx, A4_HEIGHT),   fill="black", width=w)
    draw.line((0, cy, L, cy),                       fill="black", width=w)
    draw.line((A4_WIDTH - L, cy, A4_WIDTH, cy),     fill="black", width=w)


def generate_a4_pages(images, mirror=False) -> tuple[list, str | None, list]:
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
    left_margin  = (A4_WIDTH - CARD_WIDTH) // 2
    remaining_sp = A4_HEIGHT - MAX_PER_PAGE * CARD_HEIGHT
    vertical_gap = remaining_sp // (MAX_PER_PAGE + 1)
    page_num     = 1
    page         = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
    draw         = ImageDraw.Draw(page)

    for i, card in enumerate(processed):
        pos_on_page = i % MAX_PER_PAGE
        if pos_on_page == 0 and i > 0:
            _draw_center_marks(draw)
            pages.append(page)
            page     = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), "white")
            draw     = ImageDraw.Draw(page)
            page_num += 1

        y = vertical_gap + pos_on_page * (CARD_HEIGHT + vertical_gap)
        page.paste(card, (left_margin, y))
        _draw_crop_marks(draw, left_margin, y)

    _draw_center_marks(draw)
    pages.append(page)

    if len(pages) == 1:
        buf = io.BytesIO()
        pages[0].save(buf, "PNG", dpi=(DPI, DPI))
        buf.seek(0)
        return pages, buf, []
    else:
        bufs = []
        for p in pages:
            b = io.BytesIO()
            p.save(b, "PNG", dpi=(DPI, DPI))
            b.seek(0)
            bufs.append(b)
        return pages, None, bufs


# ═══════════════════════════════════════════════════════════════════════
#  ⌨️  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════
def user_menu():
    kb = [
        [KeyboardButton("🪪 Send OTP"), KeyboardButton("💰 Top Up")],
        [KeyboardButton("🎨 Templates"), KeyboardButton("🖨️ A4 Converter")],
        [KeyboardButton("👛 Wallet"), KeyboardButton("👥 Referral")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def admin_menu():
    kb = [
        [KeyboardButton("🪪 Send OTP"), KeyboardButton("💰 Top Up")],
        [KeyboardButton("🎨 Templates"), KeyboardButton("🖨️ A4 Converter")],
        [KeyboardButton("👛 Wallet"), KeyboardButton("👥 Referral")],
        [KeyboardButton("📋 Pending"), KeyboardButton("📊 Stats")],
        [KeyboardButton("👥 All Users"), KeyboardButton("✏️ Edit Wallet")],
        [KeyboardButton("📣 Broadcast")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cancel_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)


def a4_collect_kb():
    kb = [
        [KeyboardButton("✅ Done — Generate A4")],
        [KeyboardButton("❌ Cancel")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def template_inline_keyboard(current_tid: int) -> InlineKeyboardMarkup:
    if not TEMPLATES:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("No templates available", callback_data="tpl_back")]
        ])
    rows = []
    for tid, tpl in TEMPLATES.items():
        name = tpl.get("name", f"Template {tid}")
        mark = "✅ " if tid == current_tid else ""
        rows.append([
            InlineKeyboardButton(f"{mark}{name}", callback_data=f"tpl_select:{tid}"),
            InlineKeyboardButton("👁️ Preview", callback_data=f"tpl_preview:{tid}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="tpl_back")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════
#  🛠️  HELPERS
# ═══════════════════════════════════════════════════════════════════════
async def safe_send(bot, chat_id, text, **kw):
    try:
        await bot.send_message(chat_id, text, **kw)
    except TelegramError as e:
        log.warning(f"safe_send failed: {e}")


async def safe_send_photo(bot, chat_id, photo, caption=None, **kw):
    try:
        await bot.send_photo(chat_id, photo, caption=caption, **kw)
    except TelegramError as e:
        log.warning(f"safe_send_photo failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
#  🏠  /start
# ═══════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    chat_id   = user.id
    username  = user.username or ""
    full_name = user.full_name or ""

    referred_by = None
    args        = context.args or []
    if args:
        try:
            ref_id = int(args[0])
            if ref_id != chat_id:
                referred_by = ref_id
        except ValueError:
            pass

    _db_upsert_user(chat_id, username, full_name, referred_by)

    if is_admin(update):
        set_admin_chat_id(chat_id)

    kb = admin_menu() if is_admin(update) else user_menu()
    u  = _db_get_user(chat_id)

    await update.message.reply_text(
        f"🇪🇹 <b>Welcome to Fayda ID Bot!</b>\n\n"
        f"👤 <b>Name:</b> {full_name}\n"
        f"💎 <b>Wallet:</b> {u['wallet'] if u else 0} ETB\n"
        f"🎟️ <b>Free trials left:</b> {max(0, FREE_TRIALS - (u['trials_used'] if u else 0))}\n\n"
        f"Use <b>🪪 Send OTP</b> to fetch your Fayda ID card.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text("❌ Cancelled.", reply_markup=kb)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════
#  🎨  TEMPLATE SELECTION
# ═══════════════════════════════════════════════════════════════════════
async def show_template_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    u       = _db_get_user(chat_id)
    cur_tid = u["template_id"] if u else 2

    if not _APP_ENABLED or not TEMPLATES:
        await update.message.reply_text(
            "⚠️ Templates not available. Place <code>app.py</code> in the same folder.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        "🎨 <b>Choose a card template:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=template_inline_keyboard(cur_tid),
    )


async def cb_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.from_user.id
    tid     = int(query.data.split(":")[1])
    _db_set_template(chat_id, tid)
    await query.answer("✅ Template selected!")
    u = _db_get_user(chat_id)
    await query.edit_message_reply_markup(
        reply_markup=template_inline_keyboard(tid)
    )


async def cb_template_preview_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 2:
        return
    tid = int(parts[1])
    if not _APP_ENABLED:
        await query.answer("Templates not available.", show_alert=True)
        return
    sample = get_template_sample(tid)
    if sample:
        await safe_send_photo(
            query.bot, query.message.chat_id,
            photo=sample,
            caption=f"👁️ Template {tid} preview",
        )
    else:
        await query.answer("No preview available.", show_alert=True)


async def cb_template_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.delete_message()


# ═══════════════════════════════════════════════════════════════════════
#  🪪  OTP GENERATION FLOW  (teleprint.fayda.et)
# ═══════════════════════════════════════════════════════════════════════
async def start_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    _db_upsert_user(chat_id, update.effective_user.username, update.effective_user.full_name)

    if not _db_can_generate(chat_id):
        u = _db_get_user(chat_id)
        await update.message.reply_text(
            f"❌ <b>Insufficient balance.</b>\n\n"
            f"💎 Wallet: {u['wallet']} ETB\n"
            f"💵 Cost: {PRICE_OTP} ETB\n\n"
            f"Use <b>💰 Top Up</b> to add funds.",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_kb(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🪪 <b>Enter your 16-digit Fayda Application Number (FAN):</b>\n\n"
        "<i>Example: 1234567890123456</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_FAN


async def recv_fan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fan = update.message.text.strip().replace(" ", "")

    if not re.fullmatch(r"\d{16}", fan):
        await update.message.reply_text(
            "⚠️ Please enter exactly <b>16 digits</b>. Try again:",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_FAN

    chat_id = update.effective_user.id
    await update.message.reply_text("⏳ Sending OTP… please wait.")

    client = TelePrintClient()
    context.user_data["client"] = client

    def _do_send_otp():
        return client.get_token(fan)

    loop = asyncio.get_event_loop()
    try:
        otp_info = await loop.run_in_executor(None, _do_send_otp)
    except TelePrintError as e:
        await update.message.reply_text(
            f"❌ <b>OTP failed:</b> {e}\n\nPlease check your FAN and try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu() if not is_admin(update) else admin_menu(),
        )
        return ConversationHandler.END
    except Exception as e:
        log.exception("get_token error")
        await update.message.reply_text(
            f"❌ <b>Network error:</b> {e}\n\nPlease try again later.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu() if not is_admin(update) else admin_menu(),
        )
        return ConversationHandler.END

    masked = otp_info.get("maskedMobile", "")
    context.user_data["fan"] = fan

    await update.message.reply_text(
        f"✅ OTP sent to <b>{masked}</b>\n\n"
        f"📱 Enter the 6-digit OTP code:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_OTP


async def recv_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    otp = update.message.text.strip()

    if not re.fullmatch(r"\d{4,8}", otp):
        await update.message.reply_text(
            "⚠️ Please enter a valid OTP (4-8 digits). Try again:"
        )
        return WAIT_OTP

    chat_id = update.effective_user.id
    client: TelePrintClient = context.user_data.get("client")
    fan = context.user_data.get("fan", "")

    if not client:
        await update.message.reply_text("⚠️ Session expired. Please start again with /start.")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Verifying OTP and fetching your ID…")

    def _complete_flow():
        raw      = client.validate_token(otp)
        identity = TelePrintClient.parse_identity(raw, fan)
        return identity

    loop = asyncio.get_event_loop()
    try:
        identity = await loop.run_in_executor(None, _complete_flow)
    except TelePrintError as e:
        await update.message.reply_text(
            f"❌ <b>Verification failed:</b> {e}",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu() if not is_admin(update) else admin_menu(),
        )
        return ConversationHandler.END
    except Exception as e:
        log.exception("validate_token error")
        await update.message.reply_text(
            f"❌ <b>Error:</b> {e}",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu() if not is_admin(update) else admin_menu(),
        )
        return ConversationHandler.END

    # Deduct wallet / trial
    deduct_type, remaining = _db_deduct(chat_id)

    # Reward referrer on first successful generation
    referrer_id = _db_reward_referrer_if_eligible(chat_id)
    if referrer_id:
        await safe_send(
            update.get_bot() if hasattr(update, "get_bot") else context.bot,
            referrer_id,
            f"🎉 Your referral used the bot! You earned <b>{REFERRAL_BONUS} ETB</b>.",
            parse_mode=ParseMode.HTML,
        )

    # Generate card images
    u   = _db_get_user(chat_id)
    tid = u["template_id"] if u else 2

    kb = admin_menu() if is_admin(update) else user_menu()

    fn_eng = identity.get("fullName", {}).get("eng", "")
    fn_amh = identity.get("fullName", {}).get("amh", "")
    dob    = identity.get("dateOfBirth", "")
    gen    = identity.get("gender", {})
    ph     = identity.get("phone", "")
    uin    = identity.get("uin", "")
    reg    = identity.get("region", {})
    nat    = identity.get("residenceStatus", {})

    if not _APP_ENABLED:
        # app.py failed to import — show identity as text
        info = (
            f"✅ <b>Fayda ID Retrieved</b>\n\n"
            f"👤 <b>Name:</b> {fn_eng} | {fn_amh}\n"
            f"🎂 <b>DOB:</b> {dob}\n"
            f"⚧ <b>Gender:</b> {gen.get('eng','')}\n"
            f"📱 <b>Phone:</b> {ph}\n"
            f"🆔 <b>UIN:</b> {uin}\n"
            f"📍 <b>Region:</b> {reg.get('eng','')}\n"
            f"🇪🇹 <b>Nationality:</b> {nat.get('eng','')}\n\n"
            f"💎 Wallet: {u['wallet'] if u else 0} ETB\n\n"
            f"⚠️ Card image generation is currently unavailable (app.py not loaded)."
        )
        await update.message.reply_text(info, parse_mode=ParseMode.HTML, reply_markup=kb)
        return ConversationHandler.END

    # ── Send raw photo first ──────────────────────────────────────────────────
    raw_photo_b64 = identity.get("photo", "")
    raw_photo_bytes = None
    if raw_photo_b64:
        try:
            raw_photo_bytes = safe_b64decode(raw_photo_b64)
        except Exception as e:
            log.warning(f"Raw photo decode failed: {e}")

    if raw_photo_bytes:
        try:
            await update.message.reply_photo(
                photo=io.BytesIO(raw_photo_bytes),
                caption="📷 <b>Raw Photo from Fayda</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning(f"Could not send raw photo: {e}")

    # ── Generate card (Bug 1 fix: pass unique_id string + template_id kwarg) ─
    unique_id = str(
        identity.get("uniqueId") or identity.get("fan") or identity.get("uin") or fan or ""
    )

    def _gen():
        return merge_to_template_all_outputs(identity, unique_id, template_id=tid)

    try:
        outputs = await loop.run_in_executor(None, _gen)
    except Exception as e:
        log.exception("merge_to_template_all_outputs error")
        await update.message.reply_text(
            f"⚠️ Card generation failed: {e}",
            reply_markup=kb,
        )
        return ConversationHandler.END

    # ── outputs is a dict {"color": bytes, "gray": bytes} (Bug 3 fix) ────────
    if not outputs:
        await update.message.reply_text("⚠️ No card images generated.", reply_markup=kb)
        return ConversationHandler.END

    bal_note = (
        f"🎟️ Trials left: {remaining}" if deduct_type == "trial"
        else f"💎 Wallet: {u['wallet'] if u else 0} ETB"
    )
    caption = (
        f"🪪 <b>{fn_eng}</b>\n"
        f"🆔 UIN: {uin}\n"
        f"{bal_note}"
    )

    cfg = TEMPLATES.get(tid, {})

    for mode in ["gray", "color"]:
        png_bytes = outputs.get(mode)
        if not png_bytes:
            continue
        mode_label = "🌈 Color" if mode == "color" else "⚫ Grayscale"
        try:
            await update.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=(
                    f"✅ <b>Fayda ID Card — {mode_label}</b>\n\n"
                    f"🎨 Template #{tid}: <b>{cfg.get('name','')}</b>\n"
                    f"{caption}\n\n"
                    "🖨️ Tap <b>🖨️ A4 Converter</b> for print-ready layout!"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb if mode == "color" else None,
            )
        except Exception as e:
            log.exception(f"send {mode} card error")
            await update.message.reply_text(
                f"⚠️ Could not send {mode} card: {e}", reply_markup=kb
            )

    context.user_data.clear()
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════
#  💰  TOP-UP FLOW
# ═══════════════════════════════════════════════════════════════════════
async def start_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    if _db_has_pending(chat_id):
        await update.message.reply_text(
            "⚠️ You already have a <b>pending payment</b>. "
            "Please wait for admin approval.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"💰 <b>Top Up Wallet</b>\n\n"
        f"Send payment to: <code>{PAYMENT_PHONE}</code>\n\n"
        f"Then send a <b>screenshot</b> of your payment confirmation here.\n"
        f"Minimum: <b>{PRICE_OTP} ETB</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_SCREENSHOT


async def recv_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_user.id
    username  = update.effective_user.username or ""
    full_name = update.effective_user.full_name or ""

    screenshot_id = None
    amount        = PRICE_OTP * 5  # default assumed amount

    if update.message.photo:
        screenshot_id = update.message.photo[-1].file_id
    elif update.message.document:
        screenshot_id = update.message.document.file_id
    else:
        # Text fallback — treat as amount
        text = update.message.text.strip()
        try:
            amount = int(re.search(r"\d+", text).group())
        except Exception:
            pass
        await update.message.reply_text(
            "⚠️ Please send a <b>photo</b> of your payment screenshot.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_SCREENSHOT

    pid = _db_add_payment(chat_id, username, full_name, amount, screenshot_id)

    kb = admin_menu() if is_admin(update) else user_menu()
    await update.message.reply_text(
        "✅ <b>Payment received!</b>\n\n"
        "⏳ Your payment is being reviewed. You'll be notified when approved.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

    # Notify admin
    aid = get_admin_chat_id()
    if aid:
        approve_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{pid}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no:{pid}"),
            ]
        ])
        admin_text = (
            f"💰 <b>New Payment Request #{pid}</b>\n\n"
            f"👤 @{username} ({full_name})\n"
            f"🆔 Chat ID: <code>{chat_id}</code>\n"
            f"💵 Amount: {amount} ETB (estimated)"
        )
        if screenshot_id:
            await safe_send_photo(
                context.bot, aid,
                photo=screenshot_id,
                caption=admin_text,
            )
            await safe_send(context.bot, aid, "Approve or reject:", reply_markup=approve_kb)
        else:
            await safe_send(context.bot, aid, admin_text,
                            parse_mode=ParseMode.HTML, reply_markup=approve_kb)

    return ConversationHandler.END


async def cb_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    data   = query.data
    action, pid = data.split(":")
    pid    = int(pid)
    admin  = query.from_user

    pay = _db_get_payment(pid)
    if not pay:
        await query.answer("Payment not found.", show_alert=True)
        return

    if pay["status"] != "pending":
        await query.answer(f"Already {pay['status']}.", show_alert=True)
        return

    if action == "pay_ok":
        _db_update_payment(pid, "approved", admin.username or str(admin.id))
        _db_add_wallet(pay["chat_id"], pay["amount"])
        await query.answer("✅ Approved!")
        await query.edit_message_caption(
            caption=f"✅ Payment #{pid} APPROVED by @{admin.username}",
            reply_markup=None,
        )
        await safe_send(
            context.bot, pay["chat_id"],
            f"✅ <b>Payment Approved!</b>\n\n"
            f"💎 <b>{pay['amount']} ETB</b> added to your wallet.",
            parse_mode=ParseMode.HTML,
        )
    else:
        _db_update_payment(pid, "rejected", admin.username or str(admin.id))
        await query.answer("❌ Rejected.")
        await query.edit_message_caption(
            caption=f"❌ Payment #{pid} REJECTED by @{admin.username}",
            reply_markup=None,
        )
        await safe_send(
            context.bot, pay["chat_id"],
            "❌ <b>Payment Rejected.</b>\n\n"
            "Please contact admin if you believe this is an error.",
            parse_mode=ParseMode.HTML,
        )


# ═══════════════════════════════════════════════════════════════════════
#  🖨️  A4 CONVERTER
# ═══════════════════════════════════════════════════════════════════════
async def start_a4_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["a4_images"] = []
    await update.message.reply_text(
        "🖨️ <b>A4 Converter</b>\n\n"
        "Send ID card images one by one, then tap <b>✅ Done</b>.\n"
        "Cards must be exactly 2191×667 px.",
        parse_mode=ParseMode.HTML,
        reply_markup=a4_collect_kb(),
    )
    return WAIT_A4_IMAGES


async def recv_a4_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if "Done" in text:
        images = context.user_data.get("a4_images", [])
        if not images:
            await update.message.reply_text("⚠️ No images received.")
            return WAIT_A4_IMAGES

        flip_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes", callback_data="a4_flip_yes"),
                InlineKeyboardButton("❌ No",  callback_data="a4_flip_no"),
            ]
        ])
        await update.message.reply_text(
            f"📄 {len(images)} card(s) received.\n\n🔄 Mirror/flip the cards?",
            reply_markup=flip_kb,
        )
        return WAIT_A4_FLIP

    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and "image" in doc.mime_type:
            file_id = doc.file_id

    if not file_id:
        await update.message.reply_text("⚠️ Please send an image.")
        return WAIT_A4_IMAGES

    try:
        file = await context.bot.get_file(file_id)
        data = await file.download_as_bytearray()
        img  = Image.open(io.BytesIO(data))
        context.user_data.setdefault("a4_images", []).append(img)
        count = len(context.user_data["a4_images"])
        await update.message.reply_text(
            f"✅ Image {count} added. Send more or tap Done.",
            reply_markup=a4_collect_kb(),
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not load image: {e}")

    return WAIT_A4_IMAGES


async def cb_a4_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    mirror = "yes" in query.data
    await query.answer()
    await query.delete_message()

    images = context.user_data.get("a4_images", [])
    if not images:
        await safe_send(context.bot, query.message.chat_id, "⚠️ No images.")
        return ConversationHandler.END

    kb = admin_menu() if query.from_user.username == ADMIN_USERNAME else user_menu()

    pages, single_buf, multi_bufs = generate_a4_pages(images, mirror=mirror)

    if single_buf:
        await context.bot.send_document(
            query.message.chat_id,
            document=single_buf,
            filename="a4_print.png",
            caption="🖨️ A4 print-ready sheet",
        )
    elif multi_bufs:
        for i, buf in enumerate(multi_bufs, 1):
            await context.bot.send_document(
                query.message.chat_id,
                document=buf,
                filename=f"a4_page_{i}.png",
                caption=f"🖨️ Page {i} of {len(multi_bufs)}",
            )
    else:
        await safe_send(context.bot, query.message.chat_id,
                        "⚠️ Cards must be exactly 2191×667 px.")

    await safe_send(context.bot, query.message.chat_id, "Done!", reply_markup=kb)
    context.user_data.clear()
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════
#  👑  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    payments = _db_pending_payments()
    if not payments:
        await update.message.reply_text("✅ No pending payments.")
        return
    for pay in payments[:10]:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{pay['id']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no:{pay['id']}"),
            ]
        ])
        await update.message.reply_text(
            f"💰 Payment #{pay['id']}\n"
            f"👤 @{pay['username']} ({pay['full_name']})\n"
            f"💵 {pay['amount']} ETB\n"
            f"🕐 {pay['created_at']}",
            reply_markup=kb,
        )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    tu, tg, pp, tr = _db_stats()
    await update.message.reply_text(
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total users: {tu}\n"
        f"🎯 Total generations: {tg}\n"
        f"⏳ Pending payments: {pp}\n"
        f"💰 Total revenue: {tr} ETB",
        parse_mode=ParseMode.HTML,
    )


async def admin_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    users = _db_all_users(30)
    if not users:
        await update.message.reply_text("No users yet.")
        return
    lines = ["👥 <b>Recent Users</b>\n"]
    for u in users:
        lines.append(
            f"• @{u['username'] or 'anon'} | 💎{u['wallet']} ETB | "
            f"🎯{u['trials_used']} gens | tpl:{u['template_id']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def start_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ Enter: <code>chat_id amount</code>\nExample: <code>123456789 50</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb(),
    )
    return WAIT_EDIT_CREDITS


async def recv_edit_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    parts = update.message.text.strip().split()
    try:
        cid    = int(parts[0])
        amount = int(parts[1])
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Format: chat_id amount")
        return WAIT_EDIT_CREDITS

    _db_set_wallet(cid, amount)
    await update.message.reply_text(
        f"✅ Wallet for <code>{cid}</code> set to <b>{amount} ETB</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu(),
    )
    return ConversationHandler.END


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "📣 Enter your broadcast message:",
        reply_markup=cancel_kb(),
    )
    return WAIT_BROADCAST


async def recv_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    msg     = update.message.text.strip()
    cids    = _db_get_all_chat_ids()
    sent    = 0
    failed  = 0
    for cid in cids:
        try:
            await context.bot.send_message(cid, msg)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📣 Broadcast sent: ✅ {sent} | ❌ {failed}",
        reply_markup=admin_menu(),
    )
    return ConversationHandler.END


async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /addwallet <chat_id> <amount>")
        return
    try:
        cid    = int(args[0])
        amount = int(args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid arguments.")
        return
    _db_add_wallet(cid, amount)
    await update.message.reply_text(f"✅ Added {amount} ETB to {cid}.")


# ═══════════════════════════════════════════════════════════════════════
#  🗺️  ROUTE TEXT MESSAGES
# ═══════════════════════════════════════════════════════════════════════
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text or ""
    chat_id = update.effective_user.id
    u       = _db_get_user(chat_id)

    if "Wallet" in text:
        wallet = u["wallet"] if u else 0
        trials = max(0, FREE_TRIALS - (u["trials_used"] if u else 0))
        await update.message.reply_text(
            f"👛 <b>Your Wallet</b>\n\n"
            f"💎 Balance: <b>{wallet} ETB</b>\n"
            f"🎟️ Free trials left: <b>{trials}</b>\n"
            f"💵 Cost per generation: {PRICE_OTP} ETB",
            parse_mode=ParseMode.HTML,
        )

    elif "Referral" in text:
        ref_by, bonus_paid, invited, earned = _db_get_referral_info(chat_id)
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={chat_id}"
        await update.message.reply_text(
            f"👥 <b>Referral Program</b>\n\n"
            f"📎 Your link:\n<code>{ref_link}</code>\n\n"
            f"👤 People invited: <b>{invited}</b>\n"
            f"💰 Earned: <b>{earned} ETB</b>\n\n"
            f"Each successful referral earns you <b>{REFERRAL_BONUS} ETB</b>!",
            parse_mode=ParseMode.HTML,
        )

    elif "Templates" in text:
        await show_template_menu(update, context)

    elif "Pending" in text and is_admin(update):
        await admin_pending(update, context)

    elif "Stats" in text and is_admin(update):
        await admin_stats(update, context)

    elif "All Users" in text and is_admin(update):
        await admin_all_users(update, context)

    else:
        kb = admin_menu() if is_admin(update) else user_menu()
        await update.message.reply_text(
            "Please use the menu buttons below.",
            reply_markup=kb,
        )


# ═══════════════════════════════════════════════════════════════════════
#  ⚠️  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════
async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
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

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    async def _post_init(app):
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("  ✅ Webhook cleared — starting polling…")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .concurrent_updates(True)
        .connection_pool_size(8)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    # OTP generation flow (teleprint.fayda.et)
    gen_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🪪 Send OTP$"), start_generate)
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
                MessageHandler(filters.Document.ALL, recv_screenshot),
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

    application.add_handler(CommandHandler("start",     cmd_start))
    application.add_handler(CommandHandler("cancel",    cmd_cancel))
    application.add_handler(CommandHandler("addwallet", cmd_addwallet))

    application.add_handler(gen_conv)
    application.add_handler(topup_conv)
    application.add_handler(edit_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(a4_conv)

    application.add_handler(
        CallbackQueryHandler(cb_template_select,       pattern=r"^tpl_select:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_template_preview_menu, pattern=r"^tpl_preview:\d+$")
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
        MessageHandler(filters.TEXT & ~filters.COMMAND, route_message)
    )
    application.add_error_handler(error_handler)

    return application


def main():
    log.info("╔═══════════════════════════════════════════════╗")
    log.info("║  🇪🇹  Fayda ID Bot — TelePrint Edition        ║")
    log.info("╚═══════════════════════════════════════════════╝")
    log.info(f"  👑 Admin:    @{ADMIN_USERNAME}")
    log.info(f"  📱 Payment:  {PAYMENT_PHONE}")
    log.info(f"  💵 Price:    {PRICE_OTP} ETB / OTP  |  Trials: {FREE_TRIALS}")
    log.info(f"  🌐 API via:  teleprint.fayda.et (Next.js server actions)")
    log.info(f"  🔑 next-action hash: {_NEXT_ACTION_ID[:12]}…  (set NEXT_ACTION_ID env var to override)")

    from telegram.error import Conflict as _Conflict

    for attempt in range(1, 6):
        try:
            app = build_app()
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
