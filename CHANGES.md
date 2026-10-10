# Speed changes

Behaviour is unchanged unless listed under "Behaviour changes".

## What got faster
- The photo cutout runs once per card, not once per output mode (it used to run twice).
- The cutout, QR and text layer are built in parallel. Cutouts are cached per photo, so a repeat generation skips the cutout work.
- The card QR is built from the module matrix, without a PNG round trip, and once per card instead of once per output.
- Both cards go out as one album. The admin copy is sent in the background, by file ID, so nothing is uploaded twice.
- The eSignet CSRF request starts when the user taps Send OTP (or when the QR arrives), so it overlaps typing or decoding.
- eSignet sessions share one HTTP connection pool. Resident calls reuse their session.
- uvloop event loop. Telegram connection pool 8 -> 64. Blocking work runs in a 64-thread pool.
- QR decoding runs off the event loop and tries a downscaled frame first.
- A4: no 300-DPI PNG files. JPEG previews and the PDF are sent together.
- Broadcast: parallel sends paced at 25/s, with RetryAfter handling (was about 14/s, one at a time).
- Template previews upload once and then reuse Telegram file IDs.
- Start command no longer searches /nix/store on every boot. zbar is not needed (the bot decodes QR with OpenCV).

## Background removal
- No external service is used. The remove.bg calls are gone, and REMOVE_BG_API_KEY is ignored.
- Cutouts are computed locally with Pillow + NumPy, using the same algorithm as the original fallback. The old flood fill never changed the mask, so it was dropped. The output is identical.
- Edges can differ from remove.bg, mainly on busy or uneven backgrounds. Check a few cards on real photos.

## Behaviour changes
- Cards are sent as JPEG (q92, 4:4:4). Telegram recompresses photos anyway. The Flask routes still return PNG.
- Transparent pixels in a template are composited onto white, because JPEG has no alpha.
- Both cards arrive as one album, with the caption on the first item.
- A4 page previews are JPEG. The PDF is the print file and is unchanged in layout. Nothing is written to output/ any more.
- The "30-60 seconds" wait message is now neutral text.
- The A4 instructions now say "print-ready PDF + page previews" instead of "PDF + PNG".
- The Flask portal's reCAPTCHA key is no longer pre-fetched at import. Set PREWARM_RECAPTCHA=1 to keep that behaviour.

## Deploy notes
- Run pip install -r requirements.txt (adds uvloop). Redeploy so nixpacks.toml takes effect.
- Delete REMOVE_BG_API_KEY from the Railway variables. Nothing reads it any more.
