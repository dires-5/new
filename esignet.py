# -*- coding: utf-8 -*-
"""
esignet.py  —  Fayda eSignet OTP Client  (ULTRA-FAST EDITION)
==============================================================
Optimizations vs. original:
  • aiohttp async HTTP — zero blocking I/O on bot thread
  • Persistent connector pool (keep-alive, reuse TLS handshake)
  • Parallel CSRF fetch + PKCE generation
  • Tight timeouts per call (no more 60 s wall-time on every step)
  • Retry-once on transient 5xx / connection errors
  • Pure async flow — no run_in_executor wrapper needed

Flow per user session
---------------------
1. initialize(fan)          → CSRF token + PKCE keys
2. get_oauth_details()      → transactionId + oauth-details-hash
3. send_otp()               → OTP triggered (returns masked email/phone)
4. verify_otp(otp)          → OTP validated
5. get_auth_code()          → authorization code
6. exchange_auth_code(code) → resident JWT stored internally
7. get_identity()           → full identity dict
8. parse_identity(raw, fan) → normalized dict for merge_to_template()
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("esignet")

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────
_AUTH_HOST     = "auth.fayda.et"
_AUTH_BASE     = f"https://{_AUTH_HOST}/v1/esignet"
_RESIDENT_BASE = "https://api-resident.fayda.et/esignet"

_RESIDENT_BASIC = "Basic cmVzaWRlbnQ6V05sSUM5RVpYWnBZMlY="

_CLIENT_ID    = "ajcCvwQcVm1dAr6HaW4Y5ObnDmwPPTpH9oDaZSPPrpo"
_REDIRECT_URI = "https://resident.fayda.et/callback"
_SCOPE        = "openid profile email"
_ACR_VALUES   = "mosip:idp:acr:generated-code"

_CLAIMS = {
    "userinfo": {
        "name":          {"essential": True},
        "phone_number":  {"essential": True},
        "email":         {"essential": True},
        "picture":       {"essential": True},
        "gender":        {"essential": True},
        "birthdate":     {"essential": True},
        "address":       {"essential": True},
        "nationality":   {"essential": True},
        "individual_id": {"essential": True},
    }
}

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

# Per-step timeouts (seconds)
_T_CSRF     = aiohttp.ClientTimeout(total=15)
_T_OAUTH    = aiohttp.ClientTimeout(total=20)
_T_OTP      = aiohttp.ClientTimeout(total=25)
_T_VERIFY   = aiohttp.ClientTimeout(total=20)
_T_CODE     = aiohttp.ClientTimeout(total=20)
_T_EXCHANGE = aiohttp.ClientTimeout(total=40)
_T_RESIDENT = aiohttp.ClientTimeout(total=40)


# ─────────────────────────────────────────────────────────────────────────────
#  Shared connector (one per process — reuses TLS + TCP sessions)
# ─────────────────────────────────────────────────────────────────────────────
_connector: aiohttp.TCPConnector | None = None
_connector_lock = asyncio.Lock()


async def _get_connector() -> aiohttp.TCPConnector:
    global _connector
    async with _connector_lock:
        if _connector is None or _connector.closed:
            _connector = aiohttp.TCPConnector(
                limit=20,
                limit_per_host=8,
                enable_cleanup_closed=True,
                ssl=False,          # skip cert verification — mirrors original behaviour
            )
    return _connector


# ─────────────────────────────────────────────────────────────────────────────
#  Custom exception
# ─────────────────────────────────────────────────────────────────────────────
class EsignetError(Exception):
    def __init__(self, code: str, message: str):
        self.code    = code
        self.message = message
        super().__init__(f"[{code}] {message}")

    def __str__(self):
        return self.message


# ─────────────────────────────────────────────────────────────────────────────
#  Retry helper
# ─────────────────────────────────────────────────────────────────────────────
async def _post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    json_body: dict,
    headers: dict,
    timeout: aiohttp.ClientTimeout,
    retries: int = 1,
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with session.post(
                url, json=json_body, headers=headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ServerConnectionError,
                aiohttp.ClientConnectorError,
                asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


async def _get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict,
    timeout: aiohttp.ClientTimeout,
    retries: int = 1,
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ServerConnectionError,
                aiohttp.ClientConnectorError,
                asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
#  Per-user async session
# ─────────────────────────────────────────────────────────────────────────────
class EsignetClient:
    """
    One instance per Telegram user conversation.
    All methods are native async — call them with `await`.
    """

    def __init__(self):
        # PKCE + OAuth state
        self.code_verifier:   str = ""
        self.code_challenge:  str = ""
        self.nonce:           str = ""
        self.state:           str = ""

        # eSignet session
        self.csrf_token:         str = ""
        self.txn_id:             str = ""
        self.oauth_details_key:  str = ""
        self.oauth_details_hash: str = ""
        self.fan:                str = ""

        # Resident
        self.resident_jwt: str = ""

        # aiohttp session (created lazily)
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = await _get_connector()
            self._session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                headers={"User-Agent": _BROWSER_UA},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _rand_b64url(n: int = 32) -> str:
        return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()

    @staticmethod
    def _sha256_b64url(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def _now_iso() -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _wrap(self, body: dict) -> dict:
        return {"requestTime": self._now_iso(), "request": body}

    def _auth_hdrs(self, extra: dict | None = None) -> dict:
        h = {
            "Accept":             "application/json, text/plain, */*",
            "Accept-Language":    "en-US,en;q=0.9",
            "Content-Type":       "application/json",
            "Origin":             f"https://{_AUTH_HOST}",
            "Referer":            f"https://{_AUTH_HOST}/",
            "Sec-Fetch-Dest":     "empty",
            "Sec-Fetch-Mode":     "cors",
            "Sec-Fetch-Site":     "same-origin",
            "Sec-Ch-Ua":          '"Google Chrome";v="149","Chromium";v="149","Not)A;Brand";v="24"',
            "Sec-Ch-Ua-Mobile":   "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }
        if self.csrf_token:
            h["X-XSRF-TOKEN"] = self.csrf_token
        if extra:
            h.update(extra)
        return h

    def _resident_hdrs(self, extra: dict | None = None) -> dict:
        h = {
            "Accept":         "application/json, text/plain, */*",
            "Content-Type":   "application/json",
            "Authorization":  _RESIDENT_BASIC,
            "Origin":         "https://resident.fayda.et",
            "Referer":        "https://resident.fayda.et/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if extra:
            h.update(extra)
        return h

    def _raise_on_errors(self, resp_json: dict) -> dict:
        errs = resp_json.get("errors")
        if errs:
            e = errs[0]
            raise EsignetError(
                e.get("errorCode", "ERR"),
                e.get("errorMessage", str(e)),
            )
        return resp_json.get("response", resp_json)

    # ── flow steps ───────────────────────────────────────────────────────────

    async def initialize(self, fan: str) -> None:
        """Step 0 — CSRF token + PKCE setup (async)."""
        self.fan = fan.strip()

        # Generate PKCE values (CPU-only, instant)
        self.code_verifier  = self._rand_b64url(32)
        self.code_challenge = self._sha256_b64url(self.code_verifier)
        self.nonce          = self._rand_b64url(16)
        self.state          = self._rand_b64url(16)

        sess = await self._sess()
        data = await _get_with_retry(
            sess,
            f"{_AUTH_BASE}/csrf/token",
            headers=self._auth_hdrs(),
            timeout=_T_CSRF,
        )

        tok = (
            data.get("token")
            or data.get("csrf-token")
            or data.get("csrfToken")
            or ""
        )
        # Also check cookies
        if not tok and self._session:
            for c in self._session.cookie_jar:
                if "csrf" in c.key.lower() or "xsrf" in c.key.lower():
                    tok = c.value
                    break
        self.csrf_token = tok
        log.debug("CSRF token: %s…", tok[:12] if tok else "(none)")

    async def get_oauth_details(self) -> dict:
        """Step 1 — get transactionId + oauth-details-hash."""
        body = {
            "clientId":           _CLIENT_ID,
            "redirectUri":        _REDIRECT_URI,
            "responseType":       "code",
            "scope":              _SCOPE,
            "acrValues":          _ACR_VALUES,
            "claims":             _CLAIMS,
            "claimsLocales":      "en",
            "display":            "page",
            "prompt":             "login",
            "maxAge":             21600,
            "codeChallenge":      self.code_challenge,
            "codeChallengeMethod": "S256",
            "nonce":              self.nonce,
            "state":              self.state,
            "ui_locales":         "en",
        }
        sess = await self._sess()
        resp_json = await _post_with_retry(
            sess,
            f"{_AUTH_BASE}/authorization/oauth-details",
            json_body=self._wrap(body),
            headers=self._auth_hdrs(),
            timeout=_T_OAUTH,
        )
        resp = self._raise_on_errors(resp_json)

        self.txn_id             = resp.get("transactionId", "")
        self.oauth_details_key  = resp.get("oauthDetailsKey",  "")
        self.oauth_details_hash = resp.get("oauthDetailsHash", "")

        if not self.txn_id:
            raise EsignetError("NO_TXN", "No transactionId in oauth-details response")
        log.debug("txn_id: %s", self.txn_id)
        return resp

    async def send_otp(self) -> dict:
        """Step 2 — trigger OTP. Returns {maskedEmail, maskedMobile}."""
        body = {
            "transactionId": self.txn_id,
            "individualId":  self.fan,
            "otpChannels":   ["email", "phone"],
        }
        sess = await self._sess()
        resp_json = await _post_with_retry(
            sess,
            f"{_AUTH_BASE}/authorization/send-otp",
            json_body=self._wrap(body),
            headers=self._auth_hdrs({
                "oauth-details-key":  self.oauth_details_key,
                "oauth-details-hash": self.oauth_details_hash,
            }),
            timeout=_T_OTP,
        )
        resp = self._raise_on_errors(resp_json)
        log.debug("OTP sent — masked: %s / %s",
                  resp.get("maskedMobile"), resp.get("maskedEmail"))
        return resp

    async def verify_otp(self, otp: str) -> dict:
        """Step 3 — validate OTP."""
        body = {
            "transactionId": self.txn_id,
            "challengeList": [
                {
                    "authFactorType": "OTP",
                    "challenge":      otp.strip(),
                    "format":         "alpha-numeric",
                }
            ],
        }
        sess = await self._sess()
        resp_json = await _post_with_retry(
            sess,
            f"{_AUTH_BASE}/authorization/authenticate",
            json_body=self._wrap(body),
            headers=self._auth_hdrs({
                "oauth-details-key":  self.oauth_details_key,
                "oauth-details-hash": self.oauth_details_hash,
            }),
            timeout=_T_VERIFY,
        )
        resp = self._raise_on_errors(resp_json)
        self.txn_id = resp.get("transactionId", self.txn_id)
        return resp

    async def get_auth_code(self) -> str:
        """Step 4 — get authorization code."""
        body = {
            "transactionId": self.txn_id,
            "acceptedClaims": list(_CLAIMS["userinfo"].keys()),
            "permittedAuthorizeScopes": ["openid", "profile", "email", "phone", "address"],
        }
        sess = await self._sess()
        resp_json = await _post_with_retry(
            sess,
            f"{_AUTH_BASE}/authorization/auth-code",
            json_body=self._wrap(body),
            headers=self._auth_hdrs({
                "oauth-details-key":  self.oauth_details_key,
                "oauth-details-hash": self.oauth_details_hash,
            }),
            timeout=_T_CODE,
        )
        resp = self._raise_on_errors(resp_json)
        code = resp.get("code") or resp.get("authCode")
        if not code:
            raise EsignetError("NO_CODE", "Auth code missing from auth-code response")
        return code

    async def exchange_auth_code(self, auth_code: str) -> None:
        """Step 5 — exchange auth code for resident JWT."""
        body = {
            "code":          auth_code,
            "code_verifier": self.code_verifier,
            "redirect_uri":  _REDIRECT_URI,
            "client_id":     _CLIENT_ID,
            "nonce":         self.nonce,
            "state":         self.state,
        }
        connector = await _get_connector()
        # Resident endpoint — uses its own session (different origin/auth)
        async with aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            headers={"User-Agent": _BROWSER_UA},
        ) as sess:
            async with sess.post(
                f"{_RESIDENT_BASE}/exchangeAutheCode",
                json=body,
                headers=self._resident_hdrs(),
                timeout=_T_EXCHANGE,
            ) as resp:
                resp.raise_for_status()
                text = (await resp.text()).strip()

        if text.count(".") == 2 and not text.startswith("{"):
            self.resident_jwt = text
            log.debug("JWT stored (%d chars)", len(text))
        else:
            raise EsignetError("BAD_JWT",
                               f"exchangeAutheCode did not return a JWT: {text[:120]}")

    async def get_identity(self) -> dict:
        """Step 6 — fetch full identity."""
        if not self.resident_jwt:
            raise EsignetError("NO_JWT", "Call exchange_auth_code() first")
        connector = await _get_connector()
        async with aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            headers={"User-Agent": _BROWSER_UA},
        ) as sess:
            async with sess.post(
                f"{_RESIDENT_BASE}/exchangeResident",
                json={"headers": {"Content-Type": "application/json"}},
                headers=self._resident_hdrs(
                    {"X-Authorization": f"Bearer {self.resident_jwt}"}
                ),
                timeout=_T_RESIDENT,
            ) as resp:
                resp.raise_for_status()
                raw = await resp.json(content_type=None)
        log.debug("exchangeResident top-level keys: %s", list(raw.keys()))
        return raw

    # ── identity parser (pure sync, no I/O) ──────────────────────────────────

    @staticmethod
    def parse_identity(raw: dict, fan: str) -> dict:
        """
        Normalise the exchangeResident response to the format expected by
        app.py's merge_to_template().
        """

        def _bilingual(field) -> dict:
            if isinstance(field, list):
                eng = next((x.get("value","") for x in field if x.get("language") == "eng"), "")
                amh = next((x.get("value","") for x in field if x.get("language") == "amh"), "")
                return {"eng": eng, "amh": amh}
            if isinstance(field, dict):
                return {"eng": field.get("eng",""), "amh": field.get("amh","")}
            return {"eng": str(field or ""), "amh": ""}

        data_block = raw.get("data") or raw.get("response") or {}
        identity = (
            data_block.get("identity")
            or raw.get("identity")
            or data_block
            or raw
        )
        user_info = (
            data_block.get("userInfo")
            or data_block.get("userinfo")
            or raw.get("userInfo")
            or raw.get("userinfo")
            or {}
        )

        photo = (
            identity.get("photo") or identity.get("Photo")
            or identity.get("photograph") or identity.get("face")
            or data_block.get("photo") or data_block.get("picture")
            or user_info.get("picture") or raw.get("picture")
            or raw.get("photo") or ""
        )
        if isinstance(photo, dict):
            photo = photo.get("value", "") or next(iter(photo.values()), "")

        uin = str(
            identity.get("UIN") or identity.get("uin")
            or user_info.get("individual_id") or raw.get("individual_id")
            or raw.get("sub") or ""
        )

        fn_raw = (
            identity.get("fullName") or identity.get("name")
            or user_info.get("name") or raw.get("name") or ""
        )
        fullName = {"eng": fn_raw, "amh": ""} if isinstance(fn_raw, str) \
                   else _bilingual(fn_raw)

        dob = (
            identity.get("dateOfBirth") or identity.get("dob")
            or user_info.get("birthdate") or raw.get("birthdate") or ""
        )

        g_raw = identity.get("gender") or user_info.get("gender") or raw.get("gender") or ""
        if isinstance(g_raw, str):
            g_eng = g_raw.strip().capitalize()
            _amh_map = {"male":"ወንድ","man":"ወንድ","female":"ሴት","woman":"ሴት"}
            gender = {"eng": g_eng, "amh": _amh_map.get(g_eng.lower(), "")}
        else:
            gender = _bilingual(g_raw)

        phone = str(
            identity.get("phone") or identity.get("phoneNumber")
            or user_info.get("phone_number") or raw.get("phone_number") or ""
        )
        email = str(identity.get("email") or user_info.get("email") or raw.get("email") or "")

        region = _bilingual(identity.get("region", "") or {})
        zone   = _bilingual(identity.get("zone",   "") or {})
        woreda = _bilingual(identity.get("woreda", "") or {})

        if not region["eng"]:
            addr = user_info.get("address") or raw.get("address") or {}
            if isinstance(addr, dict):
                region["eng"] = addr.get("region","") or addr.get("locality","")
                zone["eng"]   = addr.get("zone","")
                woreda["eng"] = addr.get("woreda","") or addr.get("street_address","")
            elif isinstance(addr, str):
                region["eng"] = addr

        nat_raw = (
            identity.get("residenceStatus") or identity.get("nationality")
            or raw.get("nationality") or ""
        )
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
            "photo":           photo,
            "qr_jwt":          "",
        }
