# -*- coding: utf-8 -*-
import os
import io
import re
import base64
import threading
import time
import qrcode
import barcode
import requests
import json
import gc
import numpy as np
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string, Response
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps
from barcode.writer import ImageWriter

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL = "https://card-order.fayda.et"
PORTAL   = "https://card-order.fayda.et"
HEADERS  = {
    "Content-Type": "application/json",
    "Accept":       "application/json, text/plain, */*",
    "Origin":       PORTAL,
    "Referer":      PORTAL + "/"
}

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")   # Set this env var to protect /api/download-photo

DPI = 300
LINE_SPACING_FRONT = 40
LINE_SPACING_BACK  = 35
ORANGE_RED = (100, 50, 0)
BLACK      = (0, 0, 0)
YELLOW     = (242, 205, 45)

UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# PERMANENT BASE64 DECODER — handles every variant the Fayda API emits
# ──────────────────────────────────────────────────────────────────────────────
# Variants covered:
#   • Standard base64 (alphabet +/)
#   • URL-safe base64 (alphabet -_)           ← Fayda API uses this
#   • Missing = padding  (len%4 == 2 or 3)   ← Fayda strips trailing =
#   • Impossible 1-mod-4 length              ← THE reported error (1441, 1465…)
#     (API occasionally appends 1 garbage byte after stripping padding;
#      we drop it — the 1-byte loss is invisible in a photo)
#   • data-URI prefix  data:image/...;base64,…
#   • Embedded whitespace / newlines
# ══════════════════════════════════════════════════════════════════════════════
def safe_b64decode(data: str) -> bytes:
    if not data:
        raise ValueError("safe_b64decode: empty input")
    # 1. Strip data-URI prefix
    if isinstance(data, bytes):
        data = data.decode("ascii", errors="ignore")
    if "," in data[:80]:
        data = data.split(",", 1)[1]
    # 2. Remove whitespace
    data = data.strip()
    # 3. Normalise URL-safe → standard alphabet
    data = data.replace("-", "+").replace("_", "/")
    # 4. Strip any stray non-base64 characters (spaces, newlines, etc.)
    data = re.sub(r'[^A-Za-z0-9+/=]', '', data)
    # 5. Strip existing padding, re-add correctly
    data = data.rstrip("=")
    mod = len(data) % 4
    if mod == 1:
        # 1-mod-4 is mathematically impossible in valid base64.
        # Drop the stray trailing byte the API occasionally appends.
        data = data[:-1]
        # After dropping 1, mod becomes 0 → no padding needed
    elif mod == 2:
        data += "=="
    elif mod == 3:
        data += "="
    # mod == 0: already aligned
    return base64.b64decode(data)


app     = Flask(__name__)
CORS(app)
session = requests.Session()

# ── TEMPLATE DEFINITIONS ─────────────────────────────────────────────────────
TEMPLATES = {
    1: {
        "name":            "Classic Standard",
        "file":            "static/template/template1.png",
        "sample":          "static/template/sample1.png",
        "shift":           (-97, -58),
        "photo_large_pos": (50,  162),
        "photo_large_sz":  (350, 400),
        "photo_small_pos": (848, 482),
        "photo_small_sz":  (124, 124),
        "barcode_pos":     (485, 497),
        "fin_pos":         (1173, 504),
        "qr_back_pos":     (1631, 57),
        "qr_size":         (487, 487),
        "sn_pos":          (2008, 585),
        "text_front_pos":  (417, 112),
        "text_back_pos":   (1193, 57),
        "outputs":         ["color", "gray"],
    },
    2: {
        "name":            "Modern Blue",
        "file":            "static/template/template2.png",
        "sample":          "static/template/sample2.png",
        "shift":           (-100, -45),
        "photo_large_pos": (55,  170),
        "photo_large_sz":  (350, 400),
        "photo_small_pos": (870, 500),
        "photo_small_sz":  (124, 124),
        "barcode_pos":     (470, 495),
        "fin_pos":         (1195, 535),
        "qr_back_pos":     (1619, 44),
        "qr_size":         (545, 545),
        "sn_pos":          (2005, 606),
        "text_front_pos":  (420, 112),
        "text_back_pos":   (1200, 57),
        "outputs":         ["color", "gray"],
    },
    3: {
        "name":            "Premium Gold",
        "file":            "static/template/template3.png",
        "sample":          "static/template/sample3.png",
        "shift":           (-95, -50),
        "photo_large_pos": (52,  165),
        "photo_large_sz":  (355, 410),
        "photo_small_pos": (870, 490),
        "photo_small_sz":  (130, 130),
        "barcode_pos":     (503, 526),
        "fin_pos":         (1180, 520),
        "qr_back_pos":     (1612, 34),
        "qr_size":         (545, 545),
        "sn_pos":          (2010, 616),
        "text_front_pos":  (417, 115),
        "text_back_pos":   (1195, 60),
        "outputs":         ["color", "gray"],
    },
    4: {
        "name":            "Extended Layout",
        "file":            "static/template/template4.png",
        "sample":          "static/template/sample4.png",
        "shift":           (-100, -55),
        "photo_large_pos": (52,  163),
        "photo_large_sz":  (350, 400),
        "photo_small_pos": (848, 487),
        "photo_small_sz":  (124, 124),
        "barcode_pos":     (485, 502),
        "fin_pos":         (1172, 505),
        "qr_back_pos":     (1632, 63),
        "qr_size":         (487, 487),
        "sn_pos":          (2005, 585),
        "text_front_pos":  (417, 112),
        "text_back_pos":   (1193, 57),
        "outputs":         ["color", "gray"],
    },
}

_FALLBACK_TEMPLATE = "static/template/id_template.png"

# ── TEMPLATE IMAGE CACHE ──────────────────────────────────────────────────────
_template_cache      = {}
_template_cache_lock = threading.Lock()


def _load_template(template_id: int) -> Image.Image:
    with _template_cache_lock:
        if template_id in _template_cache:
            return _template_cache[template_id].copy()
        cfg  = TEMPLATES[template_id]
        path = cfg["file"]
        if not os.path.exists(path):
            path = _FALLBACK_TEMPLATE
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Template {template_id} not found (tried {cfg['file']} and {_FALLBACK_TEMPLATE})"
            )
        img = Image.open(path).convert("RGBA")
        _template_cache[template_id] = img
        return img.copy()


def preload_templates():
    for tid in TEMPLATES:
        try:
            _load_template(tid)
        except Exception as e:
            print(f"[warn] Could not preload template {tid}: {e}")


# ── FONT CACHE ────────────────────────────────────────────────────────────────
_fonts      = {}
_fonts_lock = threading.Lock()


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    with _fonts_lock:
        if key not in _fonts:
            try:
                _fonts[key] = ImageFont.truetype(name, size)
            except Exception:
                _fonts[key] = ImageFont.load_default()
        return _fonts[key]


# ── BACKGROUND REMOVAL ────────────────────────────────────────────────────────
# Pure Pillow + NumPy — zero extra libs, zero startup RAM, safe on 512 MB plan.
#
# Key improvements over the previous version:
#   • Peak RAM ≈ 28 MB (was 200+ MB → was causing OOM on Render $5 plan)
#   • White halo / fringe eliminated via hard alpha clip + erode-before-feather
#   • BG spill suppression: un-premultiplies edge pixels to kill white tinge
#   • Multi-strip BG sampling: corners + all 4 edge strips → robust median
#   • Adaptive LAB threshold based on BG brightness
#   • Full flood-fill + morphological close
# ─────────────────────────────────────────────────────────────────────────────

def remove_bg(img: Image.Image) -> Image.Image:
    """
    Remove background using remove.bg API (https://www.remove.bg/upload).
    Falls back to pure Pillow+NumPy method if API key not set or call fails.
    Set REMOVE_BG_API_KEY env var to enable the API.
    """
    api_key = os.getenv("REMOVE_BG_API_KEY", "")
    if api_key:
        try:
            buf_in = io.BytesIO()
            img.convert("RGB").save(buf_in, format="PNG")
            buf_in.seek(0)
            resp = requests.post(
                "https://api.remove.bg/v1.0/removebg",
                files={"image_file": ("photo.png", buf_in, "image/png")},
                data={"size": "auto"},
                headers={"X-Api-Key": api_key},
                timeout=60,
            )
            if resp.status_code == 200:
                result = Image.open(io.BytesIO(resp.content)).convert("RGBA")
                print("[remove_bg] Done via remove.bg API")
                return result
            else:
                print(f"[remove_bg] API error {resp.status_code}: {resp.text[:100]} — falling back")
        except Exception as e:
            print(f"[remove_bg] API call failed: {e} — falling back")

    # ── Fallback: pure Pillow + NumPy ─────────────────────────────────────────
    # Peak RAM ≈ 28 MB — safe on Render 512 MB plan
    try:
        from collections import deque

        orig_w, orig_h = img.size
        rgb_orig = img.convert("RGB")

        mw  = 320
        mh  = max(1, int(orig_h * 320 / orig_w))
        arr = np.array(rgb_orig.resize((mw, mh), Image.LANCZOS),
                       dtype=np.float32) / 255.0

        cs = max(2, int(min(mw, mh) * 0.07))
        es = max(1, int(min(mw, mh) * 0.03))
        bg_samples = np.concatenate([
            arr[:cs,  :cs ].reshape(-1, 3),
            arr[:cs,  -cs:].reshape(-1, 3),
            arr[-cs:, :cs ].reshape(-1, 3),
            arr[-cs:, -cs:].reshape(-1, 3),
            arr[:es,   :  ].reshape(-1, 3),
            arr[-es:,  :  ].reshape(-1, 3),
            arr[:,   :es  ].reshape(-1, 3),
            arr[:,   -es: ].reshape(-1, 3),
        ], axis=0)
        bg_rgb    = np.median(bg_samples, axis=0)
        bg_bright = float(np.mean(bg_rgb))
        del bg_samples

        def _lab(a: np.ndarray) -> np.ndarray:
            a   = np.clip(a, 0.0, 1.0)
            lin = np.where(a <= 0.04045,
                           a / 12.92,
                           ((a + 0.055) / 1.055) ** 2.4)
            M   = np.array([[0.4124564, 0.3575761, 0.1804375],
                             [0.2126729, 0.7151522, 0.0721750],
                             [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
            xyz = np.clip((lin @ M.T) /
                          np.array([0.95047, 1.00000, 1.08883], dtype=np.float32),
                          0, None)
            f   = np.where(xyz > 0.008856,
                           np.cbrt(xyz),
                           (903.3 * xyz + 16.0) / 116.0)
            return np.stack([116*f[...,1]-16,
                             500*(f[...,0]-f[...,1]),
                             200*(f[...,1]-f[...,2])], axis=-1)

        lab  = _lab(arr)
        bg_L = _lab(bg_rgb.reshape(1, 1, 3))[0, 0]
        dist = np.linalg.norm(lab - bg_L, axis=-1).astype(np.float32)
        del lab

        if bg_bright > 0.80:
            threshold = 18.0
        elif bg_bright > 0.55:
            threshold = 22.0
        else:
            threshold = 26.0

        fg_mask = (dist > threshold).astype(np.uint8) * 255
        del dist

        m = Image.fromarray(fg_mask)
        for _ in range(8): m = m.filter(ImageFilter.MaxFilter(3))
        for _ in range(8): m = m.filter(ImageFilter.MinFilter(3))
        fg_mask = np.array(m, dtype=np.uint8)

        H, W = fg_mask.shape
        vis  = np.zeros((H, W), dtype=bool)
        q    = deque()
        for x in range(W):
            for ye in [0, H-1]:
                if fg_mask[ye, x] == 0 and not vis[ye, x]:
                    vis[ye, x] = True; q.append((ye, x))
        for y in range(H):
            for xe in [0, W-1]:
                if fg_mask[y, xe] == 0 and not vis[y, xe]:
                    vis[y, xe] = True; q.append((y, xe))
        while q:
            cy, cx = q.popleft()
            for dy, dx in ((-1,0),(1,0),(0,-1),(0,1)):
                ny, nx = cy+dy, cx+dx
                if (0 <= ny < H and 0 <= nx < W
                        and not vis[ny, nx] and fg_mask[ny, nx] == 0):
                    vis[ny, nx] = True; q.append((ny, nx))
        fg_mask[vis] = 0
        del vis

        m = Image.fromarray(fg_mask)
        for _ in range(4): m = m.filter(ImageFilter.MaxFilter(3))
        for _ in range(3): m = m.filter(ImageFilter.MinFilter(3))
        fg_mask = np.array(m, dtype=np.uint8)

        a_blur = Image.fromarray(fg_mask).filter(ImageFilter.GaussianBlur(1.5))
        a_arr  = np.array(a_blur, dtype=np.float32)
        a_arr  = np.where(a_arr < 80,  0,
                 np.where(a_arr > 180, 255,
                          ((a_arr - 80) / 100.0 * 255))).astype(np.uint8)
        del fg_mask, a_blur, m

        alpha_full = Image.fromarray(a_arr).resize((orig_w, orig_h), Image.LANCZOS)
        del a_arr

        alpha_np = np.array(alpha_full, dtype=np.float32) / 255.0
        rgb_np   = np.array(rgb_orig,   dtype=np.float32)
        bg_color = bg_rgb * 255.0

        edge   = (alpha_np > 0.05) & (alpha_np < 0.90)
        a3     = alpha_np[..., np.newaxis]
        fg_est = np.where(
            a3 > 0.05,
            (rgb_np - np.clip(1.0 - a3, 0, 1) * bg_color) / np.maximum(a3, 0.05),
            rgb_np
        )
        fg_est  = np.clip(fg_est, 0, 255)
        rgb_out = np.where(edge[..., np.newaxis], fg_est, rgb_np).astype(np.uint8)
        del fg_est, edge, a3, alpha_np, rgb_np

        r, g, b = Image.fromarray(rgb_out).split()
        result  = Image.merge("RGBA", (r, g, b, alpha_full))
        del r, g, b, alpha_full, rgb_out, rgb_orig, arr
        gc.collect()

        print("[remove_bg] Done via fallback — peak RAM ~28 MB, no halo")
        return result

    except Exception as e:
        print(f"[remove_bg] Fallback error: {e} — returning original RGBA")
        gc.collect()
        return img.convert("RGBA")


# ── PHOTO PROCESSING: COLOR + GRAYSCALE ──────────────────────────────────────

def prepare_photos(photo_b64: str, cfg: dict) -> dict:
    """
    Returns dict with keys: color_large, color_small, gray_large, gray_small
    Background is removed before resizing.
    """
    result = {}
    if not photo_b64:
        print("[prepare_photos] No photo_b64 in API data — card will have no photo")
        return result

    try:
        raw       = safe_b64decode(photo_b64)
        photo_img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as e:
        print(f"[prepare_photos] ❌ Failed to decode photo: {e} "
              f"(b64 len={len(photo_b64)}, mod4={len(photo_b64.strip())%4}) "
              f"— card will render without photo")
        return result

    # Remove background (graceful fallback inside remove_bg)
    photo_no_bg = remove_bg(photo_img)
    del photo_img
    gc.collect()

    lw, lh = cfg["photo_large_sz"]
    sw, sh = cfg["photo_small_sz"]

    if "color" in cfg["outputs"]:
        result["color_large"] = photo_no_bg.resize((lw, lh), Image.LANCZOS).convert("RGBA")
        result["color_small"] = photo_no_bg.resize((sw, sh), Image.LANCZOS).convert("RGBA")

    if "gray" in cfg["outputs"]:
        _, _, _, a = photo_no_bg.split()
        gray_rgb   = photo_no_bg.convert("RGB").convert("L")
        gray_l     = gray_rgb.convert("L")
        gray_l     = ImageOps.autocontrast(gray_l, cutoff=1)
        gray_rgba  = Image.merge("RGBA", (gray_l, gray_l, gray_l, a))
        result["gray_large"] = gray_rgba.resize((lw, lh), Image.LANCZOS).convert("RGBA")
        result["gray_small"] = gray_rgba.resize((sw, sh), Image.LANCZOS).convert("RGBA")
        del gray_rgb, gray_l, gray_rgba

    del photo_no_bg
    gc.collect()

    return result


# ── RECAPTCHA SITE KEY CACHE ──────────────────────────────────────────────────
_rc_key_cache = {"key": "", "ts": 0}
_RC_KEY_LOCK  = threading.Lock()
_RC_KEY_TTL   = 86400

_BROWSER_HEADERS = {
    "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;"
                                 "q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile":          "?0",
    "Sec-Ch-Ua-Platform":        '"Windows"',
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control":             "no-cache",
}


def _scrape_site_key_from_js(portal_html: str, js_base: str) -> str:
    js_paths = re.findall(r'src=["\']([^"\']*\.js(?:\?[^"\']*)?)["\']', portal_html)
    chunks   = re.findall(r'["\']([^"\']*chunk[^"\']*\.js)["\']', portal_html)
    js_paths = list(dict.fromkeys(js_paths + chunks))
    KEY_RE   = re.compile(r'\b(6L[0-9A-Za-z_\-]{38})\b')
    for path in js_paths[:30]:
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            url = js_base + path
        else:
            url = js_base + "/" + path
        try:
            r = session.get(url, headers=_BROWSER_HEADERS, timeout=10)
            if r.status_code == 200:
                m = KEY_RE.search(r.text)
                if m:
                    return m.group(1)
        except Exception:
            pass
    m = KEY_RE.search(portal_html)
    return m.group(1) if m else ""


def _fetch_site_key_fresh() -> str:
    # Strategy 1: Playwright headless
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser    = p.chromium.launch(headless=True)
            page       = browser.new_page()
            keys_found = []
            def on_request(req):
                m = re.search(r'[?&]k=(6L[0-9A-Za-z_\-]{38})', req.url)
                if m:
                    keys_found.append(m.group(1))
            page.on("request", on_request)
            page.goto(PORTAL, wait_until="networkidle", timeout=30000)
            browser.close()
            if keys_found:
                return keys_found[0]
    except ImportError:
        pass
    except Exception as e:
        print(f"[rc_key] Playwright failed: {e}")

    # Strategy 2: Fetch HTML + JS bundles
    try:
        r = session.get(PORTAL, headers=_BROWSER_HEADERS, timeout=20)
        if r.status_code == 200 and len(r.text) > 100:
            from urllib.parse import urlparse
            parsed  = urlparse(PORTAL)
            js_base = f"{parsed.scheme}://{parsed.netloc}"
            key = _scrape_site_key_from_js(r.text, js_base)
            if key:
                return key
    except Exception as e:
        print(f"[rc_key] HTML scrape failed: {e}")

    # Strategy 3: Known CDN paths
    KEY_RE = re.compile(r'\b(6L[0-9A-Za-z_\-]{38})\b')
    for path in ["/static/js/main.chunk.js", "/static/js/2.chunk.js",
                 "/main.js", "/app.js", "/bundle.js", "/vendor.js"]:
        try:
            r = session.get(PORTAL.rstrip("/") + path,
                            headers=_BROWSER_HEADERS, timeout=10)
            if r.status_code == 200:
                m = KEY_RE.search(r.text)
                if m:
                    return m.group(1)
        except Exception:
            pass

    return ""


def get_recaptcha_site_key() -> str:
    with _RC_KEY_LOCK:
        if _rc_key_cache["key"] and (time.time() - _rc_key_cache["ts"]) < _RC_KEY_TTL:
            return _rc_key_cache["key"]
        key = _fetch_site_key_fresh()
        if key:
            _rc_key_cache["key"] = key
            _rc_key_cache["ts"]  = time.time()
        return _rc_key_cache["key"]


threading.Thread(target=get_recaptcha_site_key, daemon=True).start()


# ── API CALLS ─────────────────────────────────────────────────────────────────

def api_verify(id_number: str) -> str:
    """Send FCN to card-order API — automatically triggers OTP to the user's phone."""
    r = session.post(
        f"{BASE_URL}/api/sendOtp",
        json={"idNumber": id_number, "verificationMethod": "FCN"},
        headers=HEADERS, timeout=90,
    )
    r.raise_for_status()
    resp = r.json()
    return resp.get("token", resp.get("transactionId", ""))


def api_validate_otp(otp: str, unique_id: str, token: str) -> dict:
    r = session.post(
        f"{BASE_URL}/validateOtp",
        json={"otp": otp, "uniqueId": unique_id, "verificationMethod": "FCN"},
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()


# ── HELPERS ───────────────────────────────────────────────────────────────────

def decode_photo_b64(photo_b64: str):
    try:
        raw = safe_b64decode(photo_b64)
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as e:
        print(f"[decode_photo_b64] ❌ {e}")
        return None


def _encode_photo_webp(photo_b64: str) -> str:
    """
    Encode photo as a tiny URL-safe base64 WebP string for embedding in QR.
    Returns "" on any failure (QR will be generated without photo miniature).
    """
    try:
        raw = safe_b64decode(photo_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = img.resize((50, 67), Image.LANCZOS)
        last_buf = None
        for quality in (85, 75, 65, 55, 45, 35, 25):
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=quality)
            encoded = base64.urlsafe_b64encode(buf.getvalue()).decode().rstrip("=")
            if len(encoded) <= 1100:
                return encoded
            last_buf = buf
        # All quality levels exceeded 1100 chars — return lowest quality result
        if last_buf:
            last_buf.seek(0)
            return base64.urlsafe_b64encode(last_buf.read()).decode().rstrip("=")
        return ""
    except Exception as e:
        print(f"[_encode_photo_webp] ❌ {e} — QR will be generated without photo")
        return ""


def gregorian_to_ethiopian(year: int, month: int, day: int) -> tuple:
    JDN_EPOCH_OFFSET_AMETE_MIHRET = 1723855
    def _jdn(y, m, d):
        a  = (14 - m) // 12
        y2 = y + 4800 - a
        m2 = m + 12 * a - 3
        return d + (153*m2+2)//5 + 365*y2 + y2//4 - y2//100 + y2//400 - 32045
    jdn       = _jdn(year, month, day)
    r         = jdn - JDN_EPOCH_OFFSET_AMETE_MIHRET
    eth_year  = 4 * (r // 1461)
    r        %= 1461
    if r > 365:
        eth_year += (r - 1) // 365
        r         = (r - 1) % 365
    eth_month = r // 30 + 1
    eth_day   = r % 30 + 1
    return eth_year, eth_month, eth_day


def format_dob_dual(dob_str: str, eth_dob_raw: str = "") -> str:
    """Format DOB as 'ETH_DD/MM/YYYY|GREG_DD/MON/YYYY'.

    eth_dob_raw: Ethiopian date in YYYY/MM/DD format extracted directly from the
    PDF (e.g. '2000/11/14').  When supplied it is used as-is so the card matches
    the source document exactly.  When absent the Ethiopian date is derived by
    converting the Gregorian date through gregorian_to_ethiopian().
    """
    try:
        parts = dob_str.replace("-", "/").split("/")
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            months_upper = ["JAN","FEB","MAR","APR","MAY","JUN",
                            "JUL","AUG","SEP","OCT","NOV","DEC"]
            greg_str = f"{d:02d}/{months_upper[m-1]}/{y}"

            # Use the pre-extracted Ethiopian date when available
            if eth_dob_raw:
                eth_parts = eth_dob_raw.replace("-", "/").split("/")
                if len(eth_parts) == 3:
                    ey2, em2, ed2 = int(eth_parts[0]), int(eth_parts[1]), int(eth_parts[2])
                    eth_str = f"{ed2:02d}/{em2:02d}/{ey2}"
                else:
                    ey, em, ed = gregorian_to_ethiopian(y, m, d)
                    eth_str = f"{ed:02d}/{em:02d}/{ey}"
            else:
                ey, em, ed = gregorian_to_ethiopian(y, m, d)
                eth_str = f"{ed:02d}/{em:02d}/{ey}"

            return f"{eth_str}|{greg_str}"
    except Exception:
        pass
    return dob_str


def format_dob_qr(dob_str: str) -> str:
    return dob_str.replace("-", "/")


def _build_sign_field(data: dict) -> str:
    JWT_HEADER = "eyJhbGciOiJSUzI1NiJ9"
    qr_sign = data.get("qr_sign", "").strip()
    if qr_sign:
        if ".." in qr_sign:
            return qr_sign
        parts = qr_sign.split(".")
        if len(parts) == 3 and parts[2]:
            return f"{parts[0]}..{parts[2]}"
        return qr_sign
    qr_jwt = data.get("qr_jwt", "").strip()
    if qr_jwt:
        parts = qr_jwt.split(".")
        if len(parts) == 3 and parts[2]:
            return f"{parts[0]}..{parts[2]}"
    sha256_hex = data.get("signature", "").strip()
    if sha256_hex:
        try:
            sig_b64url = base64.urlsafe_b64encode(
                bytes.fromhex(sha256_hex)).decode().rstrip("=")
            return f"{JWT_HEADER}..{sig_b64url}"
        except Exception:
            pass
    return f"{JWT_HEADER}.."


def generate_qr_bytes(data: dict) -> bytes:
    name      = data.get("fullName", {}).get("eng", "")
    dob       = data.get("dateOfBirth", "")
    gender    = data.get("gender", {}).get("eng", "").lower()
    uin       = str(data.get("uin", ""))
    photo_b64 = data.get("photo", "")
    fan       = str(data.get("uniqueId", uin))

    gender_code = "F" if "female" in gender else "M"
    dob_qr      = format_dob_qr(dob)
    photo_part  = _encode_photo_webp(photo_b64) if photo_b64 else ""
    sign_field  = _build_sign_field(data)

    def _payload(with_photo: bool) -> str:
        photo = photo_part if (with_photo and photo_part) else ""
        return (f"{photo}:DLT:{name}:V:4:G:{gender_code}"
                f":A:{fan}:D:{dob_qr}:SIGN:{sign_field}")

    payload = _payload(with_photo=True)
    if len(payload.encode("utf-8")) > 2900:
        payload = _payload(with_photo=False)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10, border=2,
    )
    qr.add_data(payload.encode("utf-8"))
    qr.make(fit=True)

    img   = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    alpha = Image.new("L", img.size, 255)
    img.putalpha(alpha)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def create_barcode_img(fan: str) -> Image.Image:
    code128   = barcode.get("code128", fan, writer=ImageWriter())
    raw_bytes = io.BytesIO()
    code128.write(raw_bytes,
        options={"module_width": 0.3, "module_height": 25,
                 "quiet_zone": 2, "write_text": False})
    raw_bytes.seek(0)
    raw = Image.open(raw_bytes).convert("RGBA").resize((350, 58), Image.LANCZOS)

    final = Image.new("RGBA", (350, 98), (255, 255, 255, 0))
    draw  = ImageDraw.Draw(final)
    draw.rectangle([(0, 0), (350, 40)], fill="white")
    font  = _font("arial.ttf", 34)
    bbox  = draw.textbbox((0, 0), fan, font=font)
    w, h  = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((350 - w) / 2, (40 - h) / 2), fan, fill="black", font=font)
    final.paste(raw, (0, 40), raw)
    return final


def create_fin_img(fin_digits: str) -> Image.Image:
    # Group every 4 digits regardless of total length (12 or 16 digits both valid)
    fin_formatted = " ".join(
        [fin_digits[i:i+4] for i in range(0, len(fin_digits), 4)])
    img  = Image.new("RGBA", (372, 57), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_label = _font("NotoSansEthiopic-Regular.ttf", 24)
    font_value = _font("arial.ttf", 28)
    draw.text((5,  2), "ፋይዳ",     fill=BLACK, font=font_label)
    draw.text((5, 25), "ልዩ ቁጥር", fill=BLACK, font=font_label)
    draw.line([(100, 2), (100, 48)], fill=BLACK, width=5)
    fin_str = f"FIN {fin_formatted}"
    bbox    = draw.textbbox((0, 0), fin_str, font=font_value)
    fw, fh  = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((110, (57 - fh) // 2), fin_str, fill=BLACK, font=font_value)
    return img


def draw_strong_text(draw, position, text, font, color):
    x, y = position
    for dx, dy in [(0,0),(1,0),(0,1),(1,1)]:
        draw.text((x+dx, y+dy), text, font=font, fill=color)


def draw_rotated_date(base_img, text, x, y_top, y_bottom, font):
    temp = Image.new("RGBA", (800, 200), (255, 255, 255, 0))
    d    = ImageDraw.Draw(temp)
    d.text((0, 0), text, fill=BLACK, font=font)
    bbox = temp.getbbox()
    if not bbox:
        return
    temp    = temp.crop(bbox)
    rotated = temp.rotate(90, expand=True)
    ratio     = (y_bottom - y_top) / rotated.height
    new_width = max(1, int(rotated.width * ratio))
    rotated   = rotated.resize((new_width, y_bottom - y_top), Image.LANCZOS)
    base_img.paste(rotated, (x, y_top), rotated)


def get_template_sample(template_id: int) -> bytes:
    try:
        cfg         = TEMPLATES.get(template_id, TEMPLATES[1])
        sample_path = cfg.get("sample", f"static/template/sample{template_id}.png")
        if os.path.exists(sample_path):
            img = Image.open(sample_path).convert("RGB")
        else:
            img = _load_template(template_id).convert("RGB")
        w, h = img.size
        if w > 900:
            new_h = int(h * 900 / w)
            img   = img.resize((900, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        print(f"[warn] template sample {template_id}: {e}")
        return b""


# ── CORE RENDER ───────────────────────────────────────────────────────────────

def _render_one(api_data: dict, unique_id: str,
                template_id: int, output_mode: str) -> Image.Image:
    cfg = TEMPLATES[template_id]
    sx, sy = cfg["shift"]

    GREG_TOP    = 190 + sy
    GREG_BOTTOM = 330 + sy
    ETH_TOP     = 466 + sy
    ETH_BOTTOM  = 590 + sy
    DATE_X      = 115 + sx

    tmpl = _load_template(template_id)
    draw = ImageDraw.Draw(tmpl)

    font_value        = _font("NotoSansEthiopic-Regular.ttf", 32)
    font_value_yellow = _font("NotoSansEthiopic-Regular.ttf", 24)
    font_date         = _font("arial.ttf", 24)
    font_sn           = _font("arial.ttf", 18)

    full_name_eng   = api_data.get("fullName", {}).get("eng", "")
    full_name_amh   = api_data.get("fullName", {}).get("amh", "")
    dob             = api_data.get("dateOfBirth", "")
    dob_eth_raw     = api_data.get("dateOfBirthEthiopian", "")  # YYYY/MM/DD from PDF
    gender_eng      = api_data.get("gender",  {}).get("eng", "")
    gender_amh      = api_data.get("gender",  {}).get("amh", "")
    nationality_eng = api_data.get("residenceStatus", {}).get("eng", "Ethiopian")
    nationality_amh = api_data.get("residenceStatus", {}).get("amh", "ኢትዮጵያዊ")
    phone           = api_data.get("phone", "")
    region_eng      = api_data.get("region",  {}).get("eng", "")
    region_amh      = api_data.get("region",  {}).get("amh", "")
    zone_eng        = api_data.get("zone",    {}).get("eng", "")
    zone_amh        = api_data.get("zone",    {}).get("amh", "")
    woreda_eng      = api_data.get("woreda",  {}).get("eng", "")
    woreda_amh      = api_data.get("woreda",  {}).get("amh", "")

    # ── FRONT TEXT ────────────────────────────────────────────────────────────
    x, y = cfg["text_front_pos"]
    draw.text((x, y), "ሙሉ ስም | Full Name", fill=ORANGE_RED, font=font_value_yellow)
    y += LINE_SPACING_FRONT
    draw_strong_text(draw, (x, y), full_name_amh, font_value, BLACK)
    y += LINE_SPACING_FRONT
    draw_strong_text(draw, (x, y), full_name_eng, font_value, BLACK)
    y += LINE_SPACING_FRONT

    draw.text((x, y), "የትውልድ ቀን | Date of Birth", fill=ORANGE_RED, font=font_value_yellow)
    y += LINE_SPACING_FRONT
    draw_strong_text(draw, (x, y), format_dob_dual(dob, dob_eth_raw), font_value, BLACK)
    y += LINE_SPACING_FRONT

    draw.text((x, y), "ጾታ | Sex", fill=ORANGE_RED, font=font_value_yellow)
    y += LINE_SPACING_FRONT
    sex_str = f"{gender_amh} | {gender_eng}" if gender_amh else gender_eng
    draw_strong_text(draw, (x, y), sex_str, font_value, BLACK)
    y += LINE_SPACING_FRONT

    draw.text((x, y), "የሚያበቃበት ቀን | Date of Expiry", fill=ORANGE_RED, font=font_value_yellow)
    y += LINE_SPACING_FRONT
    try:
        today      = datetime.now()
        EXPIRY_YEARS = 8
        exp_year   = today.year + EXPIRY_YEARS
        exp_month  = today.month
        exp_day    = today.day
        try:
            exp_greg = datetime(exp_year, exp_month, exp_day)
        except ValueError:
            # Handles e.g. Feb 29 -> non-leap target year
            exp_greg = datetime(exp_year, exp_month, 28)
        months_u = ["JAN","FEB","MAR","APR","MAY","JUN",
                    "JUL","AUG","SEP","OCT","NOV","DEC"]
        greg_exp_str = f"{exp_greg.day:02d}/{months_u[exp_greg.month-1]}/{exp_greg.year}"
        ey, em, ed   = gregorian_to_ethiopian(exp_greg.year, exp_greg.month, exp_greg.day)
        eth_exp_str  = f"{ed:02d}/{em:02d}/{ey}"
        expiry       = f"{eth_exp_str}|{greg_exp_str}"
    except Exception:
        fallback_year = datetime.now().year + 8
        expiry = f"31/DEC/{fallback_year}"
    draw_strong_text(draw, (x, y), expiry, font_value, BLACK)

    # ── ROTATED DATES ─────────────────────────────────────────────────────────
    today = datetime.today()
    draw_rotated_date(tmpl, today.strftime("%Y/%m/%d"),
                      DATE_X, GREG_TOP, GREG_BOTTOM, font_date)
    ey, em, ed = gregorian_to_ethiopian(today.year, today.month, today.day)
    draw_rotated_date(tmpl, f"{ey}/{em}/{ed}",
                      DATE_X, ETH_TOP, ETH_BOTTOM, font_date)

    # ── BACK TEXT ─────────────────────────────────────────────────────────────
    x_b, y_b = cfg["text_back_pos"]
    draw.text((x_b, y_b), "ስልክ | Phone Number", fill=ORANGE_RED, font=font_value_yellow)
    y_b += LINE_SPACING_BACK
    draw_strong_text(draw, (x_b, y_b), phone, font_value, BLACK)
    y_b += LINE_SPACING_BACK

    draw.text((x_b, y_b), "ዜግነት | Nationality", fill=ORANGE_RED, font=font_value_yellow)
    y_b += LINE_SPACING_BACK
    draw.text((x_b, y_b), "(በተገለጸው መሰረት | Self declared)", fill=ORANGE_RED, font=font_value_yellow)
    y_b += LINE_SPACING_BACK
    draw_strong_text(draw, (x_b, y_b), f"{nationality_amh} | {nationality_eng}", font_value, BLACK)
    y_b += LINE_SPACING_BACK

    draw.text((x_b, y_b), "አድራሻ | Address", fill=ORANGE_RED, font=font_value_yellow)
    y_b += LINE_SPACING_BACK
    for line in [region_amh, region_eng, zone_amh, zone_eng, woreda_amh, woreda_eng]:
        if line:
            draw_strong_text(draw, (x_b, y_b), line, font_value, BLACK)
            y_b += LINE_SPACING_BACK

    # ── PHOTO ─────────────────────────────────────────────────────────────────
    photo_b64 = api_data.get("photo", "")
    photos    = prepare_photos(photo_b64, cfg)
    lk = f"{output_mode}_large"
    sk = f"{output_mode}_small"
    if lk not in photos:
        lk = next((k for k in photos if k.endswith("_large")), None)
    if sk not in photos:
        sk = next((k for k in photos if k.endswith("_small")), None)
    if lk and lk in photos:
        tmpl.paste(photos[lk], cfg["photo_large_pos"], photos[lk])
    if sk and sk in photos:
        tmpl.paste(photos[sk], cfg["photo_small_pos"], photos[sk])

    # ── BARCODE ───────────────────────────────────────────────────────────────
    uin    = str(api_data.get("uin", ""))
    reg_id = api_data.get("regId", "")
    fan    = unique_id if unique_id else (reg_id if reg_id else uin)
    if fan:
        try:
            bc_img = create_barcode_img(fan)
            tmpl.paste(bc_img, cfg["barcode_pos"], bc_img)
        except Exception as e:
            print(f"[warn] barcode: {e}")

    # ── FIN ───────────────────────────────────────────────────────────────────
    # Priority: api_data["fin"] (OCR from PDF / OTP response)
    # OTP flow:  api_data["fin"] = uin  (set in bot.py _api_validate_otp)
    # PDF flow:  api_data["fin"] = OCR result from card back (pdf_extractor.py)
    # No FCN-derived guessing — wrong FIN is worse than no FIN.
    fin_value = api_data.get("fin", "").strip()
    if fin_value:
        fin_img = create_fin_img(fin_value)
        tmpl.paste(fin_img, cfg["fin_pos"], fin_img)
    else:
        print("[warn] FIN not available — FIN strip will not be rendered")

    # ── QR ────────────────────────────────────────────────────────────────────
    # Paste api_data["qr_crop"] (coordinate-cropped QR from card-back, base64
    # JPEG) directly onto the template — no decoding, no regeneration.
    # Falls back to a freshly generated QR only when qr_crop is missing.
    qr_crop_b64 = api_data.get("qr_crop", "")
    qr_size = cfg.get("qr_size", (487, 487))
    qr_img = None
    if qr_crop_b64:
        try:
            qr_bytes = base64.b64decode(qr_crop_b64)
            qr_img = Image.open(io.BytesIO(qr_bytes)).convert("RGBA")
            print("[qr] Using coordinate-cropped QR from card back (original, unmodified)")
        except Exception as e:
            print(f"[warn] qr_crop decode failed, falling back to generated QR: {e}")
            qr_img = None

    if qr_img is None:
        print("[qr] qr_crop missing — generating QR from JWT/identity data")
        api_data["uniqueId"] = unique_id
        qr_bytes = generate_qr_bytes(api_data)
        qr_img   = Image.open(io.BytesIO(qr_bytes)).convert("RGBA")

    alpha   = Image.new("L", qr_img.size, 255)
    qr_img.putalpha(alpha)
    qr_img  = qr_img.resize(qr_size, Image.LANCZOS)
    tmpl.paste(qr_img, cfg["qr_back_pos"], qr_img)

    # ── SN ────────────────────────────────────────────────────────────────────
    sn = fan[-12:] if len(fan) >= 12 else fan
    draw.text(cfg["sn_pos"], sn, fill=BLACK, font=font_sn)

    return tmpl


# ── PUBLIC RENDER API ─────────────────────────────────────────────────────────

def merge_to_template(api_data: dict, unique_id: str,
                      template_id: int = 1,
                      output_mode: str = "color") -> bytes:
    if template_id not in TEMPLATES:
        template_id = 1
    cfg = TEMPLATES[template_id]
    if output_mode not in cfg["outputs"]:
        output_mode = cfg["outputs"][0]
    img = _render_one(api_data, unique_id, template_id, output_mode)
    out = io.BytesIO()
    img.save(out, format="PNG", dpi=(DPI, DPI))
    return out.getvalue()


def merge_to_template_all_outputs(api_data: dict, unique_id: str,
                                   template_id: int = 1) -> dict:
    if template_id not in TEMPLATES:
        template_id = 1
    cfg     = TEMPLATES[template_id]
    results = {}
    for mode in cfg["outputs"]:
        results[mode] = merge_to_template(api_data, unique_id, template_id, mode)
    return results


# ── FLASK ROUTES ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    try:
        return render_template_string(open("templates/index.html", encoding="utf-8").read())
    except FileNotFoundError:
        return "<h1>Fayda ID Bot API running</h1>", 200


@app.route("/api/config", methods=["GET"])
def route_config():
    key = get_recaptcha_site_key()
    return jsonify({"recaptchaSiteKey": key, "ready": bool(key)})


@app.route("/api/templates", methods=["GET"])
def route_templates():
    return jsonify({
        "templates": [
            {"id": tid, "name": cfg["name"], "outputs": cfg["outputs"]}
            for tid, cfg in TEMPLATES.items()
        ]
    })


@app.route("/api/template-sample/<int:template_id>", methods=["GET"])
def route_template_sample(template_id):
    if template_id not in TEMPLATES:
        return jsonify({"error": "Template not found"}), 404
    data = get_template_sample(template_id)
    if not data:
        return jsonify({"error": "Could not generate sample"}), 500
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/verify", methods=["POST"])
def route_verify():
    body      = request.get_json(force=True)
    id_number = body.get("idNumber", "").strip()
    if not id_number:
        return jsonify({"error": "idNumber required"}), 400
    try:
        token = api_verify(id_number)
        return jsonify({"token": token, "message": "OTP sent to registered phone"})
    except requests.HTTPError as e:
        return jsonify({"error": str(e), "status": e.response.status_code}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/validate", methods=["POST"])
def route_validate():
    body      = request.get_json(force=True)
    otp       = body.get("otp", "").strip()
    unique_id = body.get("uniqueId", "").strip()
    token     = body.get("token", "").strip()
    if not all([otp, unique_id, token]):
        return jsonify({"error": "otp, uniqueId and token required"}), 400
    try:
        data    = api_validate_otp(otp, unique_id, token)
        preview = {k: data.get(k, {}) for k in
                   ["fullName","dateOfBirth","gender","phone","uin",
                    "region","zone","woreda","residenceStatus"]}
        app._id_cache = {"data": data, "uniqueId": unique_id, "jwt": token}
        return jsonify({"success": True, "preview": preview})
    except requests.HTTPError as e:
        return jsonify({"error": str(e), "status": e.response.status_code}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-id", methods=["POST"])
def route_generate():
    cache = getattr(app, "_id_cache", None)
    if not cache:
        return jsonify({"error": "No authenticated session. Please verify OTP first."}), 400
    body        = request.get_json(force=True, silent=True) or {}
    template_id = int(body.get("templateId", 1))
    output_mode = body.get("outputMode", "color")
    try:
        cache["data"]["qr_jwt"] = cache.get("jwt", "")
        png_bytes = merge_to_template(cache["data"], cache["uniqueId"],
                                      template_id, output_mode)
        return send_file(io.BytesIO(png_bytes), mimetype="image/png",
                         as_attachment=False, download_name="fayda_id.png")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-id-all", methods=["POST"])
def route_generate_all():
    cache = getattr(app, "_id_cache", None)
    if not cache:
        return jsonify({"error": "No authenticated session."}), 400
    body        = request.get_json(force=True, silent=True) or {}
    template_id = int(body.get("templateId", 1))
    try:
        cache["data"]["qr_jwt"] = cache.get("jwt", "")
        results = merge_to_template_all_outputs(cache["data"], cache["uniqueId"], template_id)
        return jsonify({
            mode: base64.b64encode(data).decode()
            for mode, data in results.items()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-qr", methods=["POST"])
def route_generate_qr():
    cache = getattr(app, "_id_cache", None)
    if not cache:
        return jsonify({"error": "No authenticated session."}), 400
    try:
        cache["data"]["qr_jwt"] = cache.get("jwt", "")
        qr_bytes = generate_qr_bytes(cache["data"])
        return send_file(io.BytesIO(qr_bytes), mimetype="image/png")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download-photo", methods=["POST"])
def route_download_photo():
    # ── Admin-only endpoint ───────────────────────────────────────────────────
    if ADMIN_TOKEN:
        auth = request.headers.get("Authorization", "")
        provided = auth.removeprefix("Bearer ").strip()
        if provided != ADMIN_TOKEN:
            return jsonify({"error": "Forbidden: admin access only."}), 403

    cache = getattr(app, "_id_cache", None)
    if not cache:
        return jsonify({"error": "No authenticated session. Please verify OTP first."}), 400
    photo_b64 = cache["data"].get("photo", "")
    if not photo_b64:
        return jsonify({"error": "No photo found in identity data."}), 404
    try:
        body = request.get_json(force=True, silent=True) or {}
        size = body.get("size", [600, 700])
        w, h = int(size[0]), int(size[1])
        photo_img = decode_photo_b64(photo_b64)
        if not photo_img:
            return jsonify({"error": "Failed to decode photo."}), 500
        photo_rgb     = photo_img.convert("RGB")
        photo_resized = photo_rgb.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        photo_resized.save(buf, format="PNG", dpi=(300, 300))
        buf.seek(0)
        full_name = (
            cache["data"].get("fullName", {}).get("eng", "photo").replace(" ", "_")
        )
        return send_file(
            buf,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"{full_name}_photo.png",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # No model pre-warm needed — BG removal is pure Pillow/NumPy (zero startup RAM)
    preload_templates()
    app.run(debug=True, port=5000)
