# -*- coding: utf-8 -*-
"""
esignet.py  —  Fayda eSignet OTP Client
=========================================
Python port of the flow implemented in:
  • server.js  (CORS proxy → auth.fayda.et + api-resident.fayda.et)
  • index.html  (PKCE OAuth2 + OTP dance in the browser)

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

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
import time

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("esignet")

# One connection pool shared by all user sessions: TLS handshakes are paid once per
# host instead of once per Telegram user. Cookies stay per session, so users never
# share login state.
_POOL       = HTTPAdapter(pool_connections=8, pool_maxsize=64)
_T_AUTH     = (6, 30)    # (connect, read) seconds for auth.fayda.et
_T_RESIDENT = (6, 60)    # (connect, read) seconds for api-resident.fayda.et
_PREP_TTL   = 240        # seconds a prefetched CSRF token is trusted


# ─────────────────────────────────────────────────────────────────────────────
#  Constants (mirrored from server.js + index.html)
# ─────────────────────────────────────────────────────────────────────────────
_AUTH_HOST     = "auth.fayda.et"
_AUTH_BASE     = f"https://{_AUTH_HOST}/v1/esignet"
_RESIDENT_BASE = "https://api-resident.fayda.et/esignet"

# The Basic auth header used by server.js for the resident endpoints
_RESIDENT_BASIC = "Basic cmVzaWRlbnQ6V05sSUM5RVpYWnBZMlY="

# PKCE OAuth2 client credentials (from index.html)
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
#  Per-user session
# ─────────────────────────────────────────────────────────────────────────────
class EsignetClient:
    """One instance per Telegram user conversation."""

    def __init__(self):
        self._sess = requests.Session()
        self._sess.mount("https://", _POOL)
        self._sess.headers["User-Agent"] = _BROWSER_UA
        self._rsess = requests.Session()        # api-resident.fayda.et (same headers as before)
        self._rsess.mount("https://", _POOL)
        self._prepared_at = 0.0

        # PKCE + OAuth state
        self.code_verifier:   str = ""
        self.code_challenge:  str = ""
        self.nonce:           str = ""
        self.state:           str = ""

        # eSignet session
        self.csrf_token:          str = ""
        self.txn_id:              str = ""
        self.oauth_details_key:   str = ""
        self.oauth_details_hash:  str = ""
        self.fan:                 str = ""

        # Resident
        self.resident_jwt: str = ""

    # ── low-level helpers ────────────────────────────────────────────────────

    @staticmethod
    def _rand_b64url(n: int = 32) -> str:
        return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()

    @staticmethod
    def _sha256_b64url(text: str) -> str:
        """SHA-256 → URL-safe base64 (no padding) — mirrors sha256B64url() in index.html."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def _now_iso() -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _wrap(self, body: dict) -> dict:
        """Wrap body the way the eSignet API expects."""
        return {"requestTime": self._now_iso(), "request": body}

    def _auth_hdrs(self, extra: dict | None = None) -> dict:
        """Base headers for auth.fayda.et — mirrors server.js proxyRequest()."""
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
        """Base headers for api-resident.fayda.et."""
        h = {
            "Accept":          "application/json, text/plain, */*",
            "Content-Type":    "application/json",
            "Authorization":   _RESIDENT_BASIC,
            "Origin":          "https://resident.fayda.et",
            "Referer":         "https://resident.fayda.et/",
            "Sec-Fetch-Site":  "same-site",
            "Sec-Fetch-Mode":  "cors",
            "Sec-Fetch-Dest":  "empty",
        }
        if extra:
            h.update(extra)
        return h

    def _raise_on_errors(self, resp_json: dict) -> dict:
        """Extract response body or raise EsignetError if the API returned errors."""
        errs = resp_json.get("errors")
        if errs:
            e = errs[0]
            raise EsignetError(
                e.get("errorCode", "ERR"),
                e.get("errorMessage", str(e)),
            )
        return resp_json.get("response", resp_json)

    # ── flow steps ───────────────────────────────────────────────────────────

    def prepare(self) -> None:
        """
        Step 0a: PKCE + CSRF token. It needs no FAN, so the bot runs it while the user
        types their FAN (or while a QR code is decoded), which hides that round trip.
        """
        self.csrf_token     = ""
        self.code_verifier  = self._rand_b64url(32)
        self.code_challenge = self._sha256_b64url(self.code_verifier)
        self.nonce          = self._rand_b64url(16)
        self.state          = self._rand_b64url(16)

        r = self._sess.get(
            f"{_AUTH_BASE}/csrf/token",
            headers=self._auth_hdrs(),
            timeout=_T_AUTH,
        )
        r.raise_for_status()
        data = r.json()

        tok = (
            data.get("token")
            or data.get("csrfToken")
            or (data.get("response") or {}).get("token")
            or self._sess.cookies.get("XSRF-TOKEN", "")
        )
        if not tok:
            raise EsignetError("NO_CSRF", "Could not obtain CSRF token from auth.fayda.et")

        self.csrf_token = tok
        # Mirror what server.js does: store as session cookie too
        self._sess.cookies.set("XSRF-TOKEN", tok, domain=_AUTH_HOST)
        self._prepared_at = time.monotonic()
        log.debug("CSRF ok: %s…", tok[:20])

    def initialize(self, fan: str) -> None:
        """
        Step 0b: bind the FAN. Reuses a prepared CSRF token while it is fresh and
        fetches one otherwise, so calling this on a cold client still works.
        """
        self.fan = fan.strip()
        if not self.csrf_token or time.monotonic() - self._prepared_at > _PREP_TTL:
            self.prepare()

    def get_oauth_details(self) -> None:
        """
        Step 1 — POST /authorization/oauth-details → transactionId + hash.

        The oauth-details-hash is SHA-256(JSON.stringify(response)) exactly
        as computed in index.html before subsequent requests.
        """
        body = {
            "clientId":            _CLIENT_ID,
            "redirectUri":         _REDIRECT_URI,
            "responseType":        "code",
            "scope":               _SCOPE,
            "state":               self.state,
            "nonce":               self.nonce,
            "codeChallenge":       self.code_challenge,
            "codeChallengeMethod": "S256",
            "acrValues":           _ACR_VALUES,
            "claims":              _CLAIMS,
        }
        r = self._sess.post(
            f"{_AUTH_BASE}/authorization/v2/oauth-details",
            json=self._wrap(body),
            headers=self._auth_hdrs(),
            timeout=_T_AUTH,
        )
        r.raise_for_status()
        resp = self._raise_on_errors(r.json())

        self.txn_id = resp.get("transactionId", "")
        if not self.txn_id:
            raise EsignetError("NO_TXN", "transactionId missing from oauth-details response")

        self.oauth_details_key = self.txn_id

        # Compute hash exactly as the browser does:
        # sha256B64url(JSON.stringify(oauthData))
        # where oauthData = the parsed response object (no whitespace in serialization)
        resp_str = json.dumps(resp, separators=(",", ":"), ensure_ascii=False)
        self.oauth_details_hash = self._sha256_b64url(resp_str)
        log.debug("txnId=%s hash=%s…", self.txn_id, self.oauth_details_hash[:20])

    def send_otp(self) -> dict:
        """
        Step 2 — POST /authorization/send-otp.
        Returns {'maskedEmail': '…', 'maskedMobile': '…'}.
        """
        body = {
            "transactionId": self.txn_id,
            "individualId":  self.fan,
            "otpChannels":   ["email", "phone"],
            # captchaToken omitted — not accepted by fayda API
        }
        r = self._sess.post(
            f"{_AUTH_BASE}/authorization/send-otp",
            json=self._wrap(body),
            headers=self._auth_hdrs({
                "oauth-details-key":  self.oauth_details_key,
                "oauth-details-hash": self.oauth_details_hash,
            }),
            timeout=_T_AUTH,
        )
        r.raise_for_status()
        resp = self._raise_on_errors(r.json())
        return {
            "maskedEmail":  resp.get("maskedEmail",  ""),
            "maskedMobile": resp.get("maskedMobile", ""),
        }

    def verify_otp(self, otp: str) -> None:
        """Step 3 — POST /authorization/v2/authenticate."""
        body = {
            "transactionId": self.txn_id,
            "individualId":  self.fan,
            "challengeList": [{
                "authFactorType": "OTP",
                "challenge":      otp.strip(),
                "format":         "alpha-numeric",
            }],
        }
        r = self._sess.post(
            f"{_AUTH_BASE}/authorization/v2/authenticate",
            json=self._wrap(body),
            headers=self._auth_hdrs({
                "oauth-details-key":  self.oauth_details_key,
                "oauth-details-hash": self.oauth_details_hash,
            }),
            timeout=_T_AUTH,
        )
        r.raise_for_status()
        self._raise_on_errors(r.json())

    def get_auth_code(self) -> str:
        """Step 4 — POST /authorization/auth-code → returns the code string."""
        body = {
            "transactionId":            self.txn_id,
            "acceptedClaims":           [
                "name", "phone_number", "email", "picture",
                "gender", "birthdate", "address", "nationality", "individual_id",
            ],
            "permittedAuthorizeScopes": ["openid", "profile", "email", "phone", "address"],
        }
        r = self._sess.post(
            f"{_AUTH_BASE}/authorization/auth-code",
            json=self._wrap(body),
            headers=self._auth_hdrs({
                "oauth-details-key":  self.oauth_details_key,
                "oauth-details-hash": self.oauth_details_hash,
            }),
            timeout=_T_AUTH,
        )
        r.raise_for_status()
        resp = self._raise_on_errors(r.json())
        code = resp.get("code") or resp.get("authCode")
        if not code:
            raise EsignetError("NO_CODE", "Auth code missing from auth-code response")
        return code

    def exchange_auth_code(self, auth_code: str) -> None:
        """
        Step 5 — POST api-resident.fayda.et/esignet/exchangeAutheCode.
        Mirrors server.js /resident/exchange-auth-code route.
        Stores the resulting JWT in self.resident_jwt.
        """
        body = {
            "code":          auth_code,
            "code_verifier": self.code_verifier,
            "redirect_uri":  _REDIRECT_URI,
            "client_id":     _CLIENT_ID,
            "nonce":         self.nonce,
            "state":         self.state,
        }
        r = self._rsess.post(
            f"{_RESIDENT_BASE}/exchangeAutheCode",
            json=body,
            headers=self._resident_hdrs(),
            timeout=_T_RESIDENT,
        )
        r.raise_for_status()
        text = r.text.strip()
        # Response is a raw JWT (three base64url segments separated by dots)
        if text.count(".") == 2 and not text.startswith("{"):
            self.resident_jwt = text
            log.debug("JWT stored (%d chars)", len(text))
        else:
            raise EsignetError("BAD_JWT", f"exchangeAutheCode did not return a JWT: {text[:120]}")

    def get_identity(self) -> dict:
        """
        Step 6 — POST api-resident.fayda.et/esignet/exchangeResident.
        Mirrors server.js /resident/exchange-resident route.
        Returns the raw identity JSON dict.
        """
        if not self.resident_jwt:
            raise EsignetError("NO_JWT", "Call exchange_auth_code() first")
        r = self._rsess.post(
            f"{_RESIDENT_BASE}/exchangeResident",
            json={"headers": {"Content-Type": "application/json"}},
            headers=self._resident_hdrs({"X-Authorization": f"Bearer {self.resident_jwt}"}),
            timeout=_T_RESIDENT,
        )
        r.raise_for_status()
        raw = r.json()
        log.debug("exchangeResident top-level keys: %s", list(raw.keys()))
        return raw

    def close(self) -> None:
        """
        Forget this user's cookies and tokens. The sockets belong to the shared pool,
        so Session.close() is deliberately not called here.
        """
        self._sess.cookies.clear()
        self._rsess.cookies.clear()
        self.csrf_token   = ""
        self.resident_jwt = ""

    # ── identity parser ──────────────────────────────────────────────────────

    @staticmethod
    def parse_identity(raw: dict, fan: str) -> dict:
        """
        Normalise the exchangeResident response to the format expected by
        app.py's merge_to_template().

        Handles both the MOSIP identity block and OpenID userinfo formats.
        """

        def _bilingual(field) -> dict:
            if isinstance(field, list):
                eng = next((x.get("value","") for x in field if x.get("language") == "eng"), "")
                amh = next((x.get("value","") for x in field if x.get("language") == "amh"), "")
                return {"eng": eng, "amh": amh}
            if isinstance(field, dict):
                return {"eng": field.get("eng",""), "amh": field.get("amh","")}
            return {"eng": str(field or ""), "amh": ""}

        # The exchangeResident API returns: { data: { identity: {...}, userInfo: {...} } }
        # OR: { response: { identity: {...} } }  OR flat dict
        # Mirror exactly what index.html does: exchangeRes?.data?.identity || {}
        data_block = raw.get("data") or raw.get("response") or {}
        identity = (
            data_block.get("identity")
            or raw.get("identity")
            or data_block
            or raw
        )
        # Also capture userInfo (OpenID claims live here in some responses)
        user_info = (
            data_block.get("userInfo")
            or data_block.get("userinfo")
            or raw.get("userInfo")
            or raw.get("userinfo")
            or {}
        )
        log.debug("identity keys: %s", list(identity.keys()) if isinstance(identity, dict) else type(identity))
        log.debug("userInfo keys: %s", list(user_info.keys()) if isinstance(user_info, dict) else type(user_info))

        # ── Photo ────────────────────────────────────────────────────────────
        # photo — check data block for raw photo too (server.js stores it at exchangeRes.photo)
        photo = (
            identity.get("photo") or identity.get("Photo")
            or identity.get("photograph") or identity.get("face")
            or data_block.get("photo") or data_block.get("picture")
            or user_info.get("picture") or raw.get("picture")
            or raw.get("photo") or ""
        )
        if isinstance(photo, dict):
            photo = photo.get("value", "") or next(iter(photo.values()), "")

        # ── UIN / individual_id ───────────────────────────────────────────────
        # API returns uppercase "UIN"; index.html: identity.UIN || ui.individual_id
        uin = str(
            identity.get("UIN") or identity.get("uin")
            or user_info.get("individual_id") or raw.get("individual_id")
            or raw.get("sub") or ""
        )

        # ── Full name ────────────────────────────────────────────────────────
        fn_raw = (
            identity.get("fullName")
            or identity.get("name")
            or user_info.get("name")
            or raw.get("name") or ""
        )
        if isinstance(fn_raw, str):
            fullName = {"eng": fn_raw, "amh": ""}
        else:
            fullName = _bilingual(fn_raw)

        # ── Date of birth ────────────────────────────────────────────────────
        dob = (
            identity.get("dateOfBirth") or identity.get("dob")
            or user_info.get("birthdate") or raw.get("birthdate") or ""
        )

        # ── Gender ───────────────────────────────────────────────────────────
        g_raw = identity.get("gender") or user_info.get("gender") or raw.get("gender") or ""
        if isinstance(g_raw, str):
            g_eng = g_raw.strip().capitalize()
            _amh_map = {"male":"ወንድ","man":"ወንድ","female":"ሴት","woman":"ሴት"}
            gender = {"eng": g_eng, "amh": _amh_map.get(g_eng.lower(), "")}
        else:
            gender = _bilingual(g_raw)

        # ── Phone & email ────────────────────────────────────────────────────
        phone = str(
            identity.get("phone") or identity.get("phoneNumber")
            or user_info.get("phone_number") or raw.get("phone_number") or ""
        )
        email = str(identity.get("email") or user_info.get("email") or raw.get("email") or "")

        # ── Address fields ───────────────────────────────────────────────────
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

        # ── Nationality ──────────────────────────────────────────────────────
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
            "fin":             uin,        # FIN = UIN in eSignet OTP flow
            "uniqueId":        fan,
            "region":          region,
            "zone":            zone,
            "woreda":          woreda,
            "residenceStatus": nat,
            "email":           email,
            "photo":           photo,
            "qr_jwt":          "",         # filled by bot after exchange_auth_code
        }
