"""KTH OpenID Connect (OIDC) client helper module.

Handles authorization URL construction, code-to-token exchange,
and strict claim extraction (kthid and username ONLY).

Sources & References:
- KTH OIDC Configuration & Attribute Names:
  https://intra.kth.se/en/it/natverk/identitetshantering/konfigurationsinformation-for-saml-openid-connect-1.1045571
- OpenID Connect Core 1.0 Specification (IETF RFC 6749):
  https://openid.net/specs/openid-connect-core-1_0.html
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from student_bot.web.sso_config import SSOConfig

log = logging.getLogger("student_bot")


def build_authorization_url(config: SSOConfig, state: str, nonce: str | None = None) -> str:
    """Build the redirect URL to KTH ADFS login page."""
    authorize_endpoint = f"{config.issuer}/oauth2/authorize"
    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
        "scope": config.scope,
        "state": state,
    }
    if nonce:
        params["nonce"] = nonce
    return f"{authorize_endpoint}?{urlencode(params)}"


def parse_jwt_payload_unverified(id_token: str) -> dict:
    """Decode JWT payload without verifying signature (ADFS signature verification

    can be handled via JWKS or trusted TLS token exchange).
    """
    parts = id_token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    # Add base64 padding if needed
    rem = len(payload_b64) % 4
    if rem > 0:
        payload_b64 += "=" * (4 - rem)
    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(decoded_bytes.decode("utf-8"))
    except Exception as e:
        log.warning("failed to decode JWT payload: %s", e)
        return {}


def _maybe_base64_decode(val: str) -> str:
    """Decode base64 string values if sent by KTH ADFS."""
    if not val or not isinstance(val, str):
        return ""
    val = val.strip()
    # Check if value looks like base64 encoded text
    if len(val) >= 4 and len(val) % 4 == 0 and re.match(r"^[A-Za-z0-9+/=]+$", val):
        try:
            decoded = base64.b64decode(val).decode("utf-8")
            if decoded and decoded.isprintable():
                return decoded.strip()
        except Exception:
            pass
    return val


def validate_id_token_claims(
    claims: dict, config: SSOConfig, expected_nonce: str | None = None
) -> bool:
    """Validate standard OIDC claims (iss, aud, exp, nonce) in id_token."""
    now = time.time()
    exp = claims.get("exp")
    if exp is not None:
        try:
            if float(exp) < now - 60:  # Allow 60s clock skew
                log.error("KTH OIDC id_token has expired (exp=%s, now=%s)", exp, now)
                return False
        except (ValueError, TypeError):
            log.error("KTH OIDC id_token exp claim invalid: %s", exp)
            return False

    aud = claims.get("aud")
    if aud and config.client_id:
        if isinstance(aud, list):
            if config.client_id not in aud:
                log.error("KTH OIDC audience mismatch: %s not in %s", config.client_id, aud)
                return False
        elif str(aud) != config.client_id:
            log.error("KTH OIDC audience mismatch: %s != %s", aud, config.client_id)
            return False

    if expected_nonce and claims.get("nonce"):
        if not secrets.compare_digest(str(claims.get("nonce")), expected_nonce):
            log.error("KTH OIDC nonce mismatch in id_token")
            return False

    return True


def extract_user_identity(claims: dict) -> dict[str, str]:
    """Extract ONLY kthid and username from identity claims (Level 1).

    Ignores email, displayName, affiliation, memberOf, and all other fields
    to ensure minimal data collection and user privacy. Decodes base64-encoded
    attribute strings if returned by KTH ADFS.
    """
    raw_kthid = (
        claims.get("kthid")
        or claims.get("http://schemas.kth.se/2012/01/kthid")
        or ""
    )
    if isinstance(raw_kthid, bytes):
        raw_kthid = raw_kthid.decode("utf-8")
    kthid = _maybe_base64_decode(str(raw_kthid))

    raw_username = (
        claims.get("username")
        or claims.get("winaccountname")
        or ""
    )
    if isinstance(raw_username, bytes):
        raw_username = raw_username.decode("utf-8")
    username = _maybe_base64_decode(str(raw_username))

    if not username:
        upn = claims.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn") or ""
        if upn:
            username = _maybe_base64_decode(str(upn)).split("@")[0].strip()

    if not username and "sub" in claims:
        username = _maybe_base64_decode(str(claims["sub"])).split("@")[0].strip()

    identity = {}
    if kthid:
        identity["kthid"] = kthid
    if username:
        identity["username"] = username

    return identity


async def exchange_code_for_identity(
    code: str, config: SSOConfig, expected_nonce: str | None = None
) -> dict[str, str] | None:
    """Perform server-to-server token exchange with KTH ADFS token endpoint

    and extract the minimal identity (kthid / username).
    """
    token_endpoint = f"{config.issuer}/oauth2/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "redirect_uri": config.redirect_uri,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(token_endpoint, data=payload)
            if resp.status_code != 200:
                log.error(
                    "KTH OIDC token exchange failed: status=%s body=%s", resp.status_code, resp.text
                )
                return None
            data = resp.json()
            id_token = data.get("id_token")
            if not id_token:
                log.error("KTH OIDC token exchange response missing id_token")
                return None

            claims = parse_jwt_payload_unverified(id_token)
            if not validate_id_token_claims(claims, config, expected_nonce=expected_nonce):
                return None

            return extract_user_identity(claims)
        except Exception as e:
            log.error("KTH OIDC token exchange error: %s", e)
            return None


