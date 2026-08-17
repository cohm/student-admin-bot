"""FastAPI APIRouter for KTH SSO / OIDC authentication routes.

Sources & References:
- KTH Identity Management & Application Registration:
  https://intra.kth.se/en/it/natverk/identitetshantering/identitetshantering-pa-kth-1.1029270
- FastAPI Bigger Applications Architecture Pattern (APIRouter):
  https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

import secrets
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from student_bot.web.kth_oidc import (
    build_authorization_url,
    exchange_code_for_identity,
)
from student_bot.web.sso_config import SSOConfig

import os

def _get_base_path(request: Request) -> str:
    root_path = request.scope.get("root_path", "").rstrip("/")
    if root_path:
        return root_path
    env_base = os.environ.get("WEB_BASE_PATH", "").strip()
    if env_base:
        if not env_base.startswith("/"):
            env_base = "/" + env_base
        return env_base.rstrip("/")
    return ""


sso_router = APIRouter(prefix="/auth/kth", tags=["kth-sso"])


@sso_router.get("/login")
async def kth_sso_login(request: Request):
    """Redirect user to KTH ADFS login page."""
    config = SSOConfig.from_env()
    if not config.enabled:
        raise HTTPException(status_code=400, detail="KTH SSO is disabled")
    if not config.client_id:
        raise HTTPException(status_code=500, detail="KTH OIDC client_id is not configured")

    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce
    auth_url = await build_authorization_url(config, state, nonce=nonce)
    return RedirectResponse(auth_url)


@sso_router.get("/callback")
async def kth_sso_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    """Callback endpoint for KTH ADFS authentication code redirect."""
    config = SSOConfig.from_env()
    if not config.enabled:
        raise HTTPException(status_code=400, detail="KTH SSO is disabled")

    if error:
        raise HTTPException(status_code=400, detail=f"KTH authentication error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    saved_state = request.session.pop("oidc_state", None)
    if not saved_state or not state or not secrets.compare_digest(saved_state, state):
        raise HTTPException(status_code=400, detail="Invalid state parameter (CSRF protection)")

    expected_nonce = request.session.pop("oidc_nonce", None)
    identity = await exchange_code_for_identity(code, config, expected_nonce=expected_nonce)
    if not identity:
        raise HTTPException(status_code=401, detail="Failed to authenticate with KTH SSO")

    # Store ONLY minimal identity (kthid and/or username) in session
    request.session["kth_user"] = identity
    request.session["granted_at"] = time.time()
    base_path = _get_base_path(request)
    redirect_target = f"{base_path}/" if base_path else "/"
    return RedirectResponse(redirect_target, status_code=303)


@sso_router.get("/logout")
async def kth_sso_logout(request: Request):
    """Clear local KTH SSO session."""
    request.session.pop("kth_user", None)
    request.session.pop("granted_at", None)
    base_path = _get_base_path(request)
    redirect_target = f"{base_path}/" if base_path else "/"
    return RedirectResponse(redirect_target, status_code=303)
