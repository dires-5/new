#!/usr/bin/env python3
"""
QR Extractor — decode QR codes from an image and make sense of what's inside.

Handles plain text, URLs, WiFi credentials, vCards, calendar events, and
structured digital-credential QR codes that use Base45 + zlib + CBOR
(the format behind EU Digital COVID Certificates, Ethiopia's Fayda Digital
ID, and similar verifiable-credential systems).

Setup:
    sudo apt-get install -y libzbar0
    pip install pyzbar pillow base45 cbor2

Usage:
    python qr_extractor.py photo.jpg
"""

import sys
import json
import zlib

from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode

try:
    import base45
except ImportError:
    base45 = None

try:
    import cbor2
except ImportError:
    cbor2 = None


def read_qr_codes(image_path):
    """Return the decoded text of every QR code found in an image."""
    img = Image.open(image_path)
    results = zbar_decode(img)
    return [r.data.decode("utf-8", errors="replace") for r in results]


def try_base45_cbor(text):
    """Try the Base45 -> zlib -> CBOR chain used by digital credentials."""
    if not base45 or not cbor2:
        return None
    try:
        raw = base45.b45decode(text)
    except Exception:
        return None
    try:
        raw = zlib.decompress(raw)
    except zlib.error:
        pass  # not every payload is compressed
    try:
        return cbor2.loads(raw)
    except Exception:
        return None


def parse_wifi(s):
    fields = {}
    for part in s[5:].split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            fields[k] = v
    return fields


def classify_and_parse(text):
    """Best-effort identification of what kind of payload this is."""
    s = text.strip()
    upper = s.upper()

    if upper.startswith("WIFI:"):
        return "WiFi credentials", parse_wifi(s)
    if upper.startswith("BEGIN:VCARD"):
        return "Contact card (vCard)", s
    if upper.startswith("BEGIN:VEVENT"):
        return "Calendar event", s
    if s.lower().startswith(("http://", "https://")):
        return "URL", s
    if s.startswith("geo:"):
        return "Location", s

    credential = try_base45_cbor(s)
    if credential is not None:
        return "Structured digital credential (Base45/zlib/CBOR)", credential

    return "Plain text", s


def main():
    if len(sys.argv) != 2:
        print("Usage: python qr_extractor.py <image_path>")
        sys.exit(1)

    codes = read_qr_codes(sys.argv[1])
    if not codes:
        print("No QR code detected in that image.")
        return

    for i, raw in enumerate(codes, 1):
        print(f"\n=== QR code {i} ===")
        kind, parsed = classify_and_parse(raw)
        print(f"Type: {kind}")
        if isinstance(parsed, dict):
            print(json.dumps(parsed, indent=2, ensure_ascii=False, default=str))
        else:
            print(parsed)


if __name__ == "__main__":
    main()
