"""Protection for a server that is reachable from the internet (behind a tunnel or a host's proxy).

PUBLIC_MODE=1 switches it on. Off (the default, for the room demo on the Wi-Fi) nothing here changes.

When on:
  * the cashier, the operator pages and the demo helpers are closed completely, even for requests that
    look like they come from this computer (a tunnel forwards from 127.0.0.1)
  * simulated purchases stay off unless ALLOW_PUBLIC_DEMO=1 (points only, no real money either way)
  * every client is limited by its real address (CF-Connecting-IP or the first X-Forwarded-For entry),
    with tighter limits on the endpoints that create wallets
  * a phone may hold only a few WebSocket connections, and the whole server only so many
  * the server should listen on 127.0.0.1 only, so just the proxy can reach it (start_public.bat does)
"""

import os
import threading
import time

from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray")

GENERAL_LIMIT = (900, 60.0)                   # requests per window, any path (phones polling are ~200/min)
STRICT_LIMITS = {                             # (method, path): (limit, window seconds)
    ("POST", "/api/sessions"): (12, 60.0),
    ("POST", "/api/crowd/join"): (20, 60.0),
    ("POST", "/api/crowd/bet"): (90, 60.0),
    ("POST", "/api/crowd/seed"): (30, 60.0),
    ("POST", "/api/wallet/purchase"): (20, 60.0),
}
MAX_BODY_BYTES = 16 * 1024
WS_PER_CLIENT = 4
WS_TOTAL = 250


def public_mode() -> bool:
    return os.getenv("PUBLIC_MODE", "").strip() == "1"


def allow_public_demo() -> bool:
    return os.getenv("ALLOW_PUBLIC_DEMO", "").strip() == "1"


def _hostname(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):                         # [::1]:8001
        return value[1:].split("]")[0]
    return value.split(":")[0]


def is_direct_local(request: Request) -> bool:
    """A request made on the server computer itself, not one a proxy or tunnel forwarded."""
    if request.client is None or request.client.host not in LOCAL_HOSTS:
        return False
    headers = request.headers
    if any(name in headers for name in PROXY_HEADERS):
        return False
    host = headers.get("host")
    return host is None or _hostname(host) in LOCAL_HOSTS


def is_local_request(request: Request) -> bool:
    """For the cashier and operator pages: never in public mode, even when it looks local."""
    return not public_mode() and is_direct_local(request)


def host_only(request: Request) -> None:
    """The game on the server computer is the crowd host. On a public server nobody else may take the seat."""
    if public_mode() and not is_direct_local(request):
        raise HTTPException(status_code=403, detail="Only the game on the server computer can host")


def local_only(request: Request) -> None:
    if not is_local_request(request):
        raise HTTPException(status_code=403, detail="Only available on the server computer")


def client_key(scope_client, headers) -> str:
    """The address to rate-limit by. Only in public mode do we believe the proxy's header."""
    if public_mode():
        cf = headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        forwarded = headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return scope_client[0] if scope_client else "unknown"


class _Windows:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits: dict[tuple, deque] = defaultdict(deque)
        self.sweep_at = 0.0

    def hit(self, key: tuple, limit: int, window: float, now: float) -> bool:
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if now > self.sweep_at:                   # forget clients that went quiet
                self.sweep_at = now + 120.0
                for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - 300.0]:
                    del self.hits[k]
            return True

    def clear(self):
        with self.lock:
            self.hits.clear()


_windows = _Windows()
_ws_lock = threading.Lock()
_ws_open: dict[str, int] = defaultdict(int)


def reset() -> None:
    """Forget every counter (tests)."""
    _windows.clear()
    with _ws_lock:
        _ws_open.clear()


def check_rate(method: str, path: str, key: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    strict = STRICT_LIMITS.get((method, path))
    if strict and not _windows.hit(("s", key, method, path), strict[0], strict[1], now):
        return False
    return _windows.hit(("g", key), GENERAL_LIMIT[0], GENERAL_LIMIT[1], now)


def ws_acquire(key: str) -> bool:
    if not public_mode():
        return True
    with _ws_lock:
        if _ws_open[key] >= WS_PER_CLIENT or sum(_ws_open.values()) >= WS_TOTAL:
            return False
        _ws_open[key] += 1
        return True


def ws_release(key: str) -> None:
    if not public_mode():
        return
    with _ws_lock:
        if _ws_open[key] > 0:
            _ws_open[key] -= 1
            if _ws_open[key] == 0:
                del _ws_open[key]


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


async def public_guard(request: Request, call_next):
    """Middleware: size cap and rate limits in public mode, safe headers always."""
    if public_mode():
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        key = client_key((request.client.host, request.client.port) if request.client else None, request.headers)
        if not check_rate(request.method, request.url.path, key):
            return JSONResponse({"detail": "Too many requests, slow down a little"}, status_code=429,
                                headers={"Retry-After": "10"})
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response
