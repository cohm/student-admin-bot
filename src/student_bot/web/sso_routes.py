"""FastAPI APIRouter for KTH SSO / OIDC authentication routes.

Sources & References:
- KTH Identity Management & Application Registration:
  https://intra.kth.se/en/it/natverk/identitetshantering/identitetshantering-pa-kth-1.1029270
- FastAPI Bigger Applications Architecture Pattern (APIRouter):
  https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

import html
import os
from pathlib import Path
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from student_bot.config import get_config
from student_bot.web.auth import load_users_file
from student_bot.web.kth_oidc import (
    build_authorization_url,
    exchange_code_for_identity,
)
from student_bot.web.sso_config import SSOConfig


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


def _access_denied_response(account: str) -> HTMLResponse:
    safe_account = html.escape(account)
    content = f"""<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Åtkomst nekad – Student-bot</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 1.5rem;
            box-sizing: border-box;
        }}
        .card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 2.5rem;
            max-width: 480px;
            width: 100%;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
            text-align: center;
        }}
        h1 {{
            font-size: 1.5rem;
            font-weight: 600;
            margin: 0 0 1rem;
            color: #f1f5f9;
        }}
        p {{
            font-size: 0.95rem;
            color: #94a3b8;
            line-height: 1.5;
            margin: 0 0 1.25rem;
        }}
        .user-tag {{
            display: inline-block;
            background: #0f172a;
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.25);
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.9rem;
            margin-bottom: 1.5rem;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Åtkomst nekad</h1>
        <p>Ditt KTH-konto har autentiserats, men saknar behörighet att använda denna chatbot under pilotfasen.</p>
        <div class="user-tag">{safe_account}</div>
        <p>Kontakta administratören om du behöver tillgång till boten.</p>
    </div>
</body>
</html>"""
    return HTMLResponse(content=content, status_code=403)


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

    # Whitelist authorization: verify user against web_users if users_file is configured
    cfg = getattr(request.app.state, "cfg", None) or get_config()
    username = identity.get("username") or ""
    kthid = identity.get("kthid") or ""
    base_path = _get_base_path(request)

    if cfg.web.users_file:
        users_path = cfg.absolute(Path(cfg.web.users_file))
        users = load_users_file(users_path)
        is_allowed = bool((username and username in users) or (kthid and kthid in users))
        if not is_allowed:
            request.session.pop("kth_user", None)
            request.session.pop("granted_at", None)
            account_display = username or kthid or "okänd användare"
            return _access_denied_response(account_display)

    # Store ONLY minimal identity (kthid and/or username) in session
    request.session["kth_user"] = identity
    request.session["granted_at"] = time.time()
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
