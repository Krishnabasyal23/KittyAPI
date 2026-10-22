"""Signed, single-use player QR codes for the cashier page.

A player's phone (or the PC game) asks for a QR code. The code carries a token signed with
HMAC-SHA256: the wallet id, a random nonce and an expiry time. The cashier page scans it (or types the
short counter code printed under it), sees who the player is, and the first top-up spends the token.
Single use is enforced by the qr_tokens table (migration 0007), not by the signature.

The signing key comes from QR_SECRET. Without it the server makes a random key at start-up, so codes
simply stop working after a restart (they only live a few minutes anyway).
"""

import base64
import hashlib
import hmac
import io
import json
import os
import secrets

from datetime import timedelta

QR_LIFETIME = timedelta(minutes=5)
TOKEN_PREFIX = "CQ1"
COUNTER_CODE_LENGTH = 8
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"     # Crockford base32: no I, L, O, U

_process_key = secrets.token_bytes(32)


def _key() -> bytes:
    configured = os.getenv("QR_SECRET", "").strip()
    return configured.encode("utf-8") if configured else _process_key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())


def new_counter_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(COUNTER_CODE_LENGTH))


def normalise_counter_code(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def make_token(wallet_id: str, nonce: str, expires_epoch: int) -> str:
    body = _b64(json.dumps({"w": wallet_id, "n": nonce, "e": expires_epoch}, separators=(",", ":")).encode())
    return f"{TOKEN_PREFIX}.{body}.{_sign(body)}"


def read_token(token: str) -> dict | None:
    """{'w', 'n', 'e'} if the signature is ours, else None. Expiry and single use are checked by the caller."""
    try:
        prefix, body, signature = token.strip().split(".")
    except ValueError:
        return None
    if prefix != TOKEN_PREFIX or not hmac.compare_digest(signature, _sign(body)):
        return None
    try:
        claims = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(claims, dict) or not {"w", "n", "e"} <= claims.keys():
        return None
    return claims


def qr_svg(text: str) -> str:
    """The QR code as an SVG string (segno; no image library needed)."""
    import segno

    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=6, border=2, xmldecl=False, svgns=True)
    return out.getvalue().decode("utf-8")


def qr_png_base64(text: str) -> str:
    """The same QR code as a base64 PNG, for the Unity client."""
    import segno

    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="png", scale=6, border=2)
    return base64.b64encode(out.getvalue()).decode("ascii")
