"""
App-wide login gate, active only when ``OCEANFRAME_AUTH`` is on.

A plain ASGI middleware rather than Starlette's BaseHTTPMiddleware, which buffers
in ways that interfere with the SSE job streams.

* No valid session: page requests are redirected to ``/login?next=…``; API and
  everything else get a 401.
* Thumbnails (``<img src>``) and ``EventSource`` cannot send headers, which is
  why the session rides in a cookie — the browser attaches it to those too.
* State-changing API calls must come from this site: if the browser sent an
  ``Origin``, its host has to match the host the request was addressed to. The
  cookie is ``SameSite=Lax`` already; this closes the gaps Lax leaves.
"""
from __future__ import annotations

import json
import logging
from http.cookies import SimpleCookie
from urllib.parse import quote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from auth import config, service

logger = logging.getLogger("oceanframe.auth")

EXEMPT_PREFIXES = ("/static/", "/healthz", "/login", "/api/auth/login", "/api/auth/signup", "/favicon.ico")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 — a malformed cookie header is just "no session"
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def origin_allowed(headers: dict[str, str]) -> bool:
    origin = headers.get("origin")
    if not origin or origin == "null":
        # Non-browser clients send none; browsers always do for cross-site POSTs.
        return origin is None
    origin_host = urlsplit(origin).netloc.lower()
    # Behind the Cloud Workstations gateway the request may arrive addressed to
    # the container, with the public host in X-Forwarded-Host.
    hosts = {headers.get("host", "").lower()}
    hosts.update(h.strip().lower() for h in headers.get("x-forwarded-host", "").split(",") if h.strip())
    return origin_host in hosts


async def _send_json(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


async def _redirect(send: Send, location: str) -> None:
    await send({"type": "http.response.start", "status": 303, "headers": [
        (b"location", location.encode("latin-1")), (b"content-length", b"0"),
    ]})
    await send({"type": "http.response.body", "body": b""})


class AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cfg = config.load()
        if not cfg.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        headers = _headers(scope)

        if cfg.origin_check and method in UNSAFE_METHODS and path.startswith("/api/") \
                and not origin_allowed(headers):
            # Logged in full: behind a proxy that rewrites Host without sending
            # X-Forwarded-Host this would refuse every write, and this line is
            # how that gets diagnosed (fix: OCEANFRAME_ORIGIN_CHECK=off).
            logger.warning(
                "Cross-site request refused: %s %s origin=%r host=%r x-forwarded-host=%r",
                method, path, headers.get("origin"), headers.get("host"), headers.get("x-forwarded-host"),
            )
            await _send_json(send, 403, "Cross-site request refused")
            return

        if path.startswith(EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        user = await run_in_threadpool(service.resolve_token, _cookie(headers, cfg.cookie_name))
        if user is None:
            if method == "GET" and not path.startswith("/api/"):
                query = scope.get("query_string", b"").decode("latin-1")
                target = path + (f"?{query}" if query else "")
                await _redirect(send, f"/login?next={quote(target, safe='')}")
            else:
                await _send_json(send, 401, "Authentication required")
            return

        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


def safe_next(target: str | None) -> str:
    """Only same-site paths are valid post-login destinations (no open redirect)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
