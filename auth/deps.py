"""
FastAPI dependencies: who is asking, and are they allowed.

    @router.post("/thing", dependencies=[Depends(require_role("curator"))])
    def make_thing(user: CurrentUser = Depends(current_user)): ...

With login off every request is the built-in ``local`` admin, so these pass and
the handler still gets a real user to attribute work to.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from auth import config, service
from auth.models import ROLE_RANK
from auth.service import CurrentUser


async def current_user(request: Request) -> CurrentUser:
    # The middleware resolves the cookie once per request and leaves the
    # answer on request.state; reuse it rather than querying again.
    user = getattr(request.state, "user", None)
    if user is not None:
        return user
    cfg = config.load()
    if not cfg.enabled:
        user = await run_in_threadpool(service.local_user)
    else:
        user = await run_in_threadpool(service.resolve_token, request.cookies.get(cfg.cookie_name))
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
    request.state.user = user
    return user


async def optional_user(request: Request) -> CurrentUser | None:
    try:
        return await current_user(request)
    except HTTPException:
        return None


def require_role(role: str) -> Callable:
    if role not in ROLE_RANK:
        raise ValueError(f"Unknown role {role!r}")

    async def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.has_role(role):
            raise HTTPException(status_code=403, detail=f"This needs the {role} role or higher")
        return user

    return dependency


def require_role_from(get_role: Callable[[], str]) -> Callable:
    """Like :func:`require_role`, with the role read from a setting on each request."""

    async def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        role = get_role()
        if not user.has_role(role):
            raise HTTPException(status_code=403, detail=f"This needs the {role} role or higher")
        return user

    return dependency


require_viewer = require_role("viewer")
require_curator = require_role("curator")
require_lead = require_role("lead")
require_admin = require_role("admin")
