"""KTH OpenID Connect (OIDC) client helper module.

Handles dynamic OIDC metadata discovery, JWKS public key fetching,
cryptographic RS256 signature verification via standard security libraries (joserfc/authlib),
and strict minimal claim extraction (kthid and username ONLY).

Sources & References:
- KTH OIDC Configuration & Attribute Names:
  https://intra.kth.se/en/it/natverk/identitetshantering/konfigurationsinformation-for-saml-openid-connect-1.1045571
- OpenID Connect Core 1.0 Specification (IETF RFC 6749):
  https://openid.net/specs/openid-connect-core-1_0.html
- joserfc Package (PyPI):
  https://pypi.org/project/joserfc/
- Authlib Documentation:
  https://docs.authlib.org/
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt
from joserfc.jwk import KeySet

from student_bot.web.sso_config import SSOConfig

log = logging.getLogger("student_bot")

# Simple in-memory cache for OIDC discovery metadata and JWKS key sets
_METADATA_CACHE: dict[str, tuple[float, dict]] = {}
_JWKS_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 3600  # 1 hour cache TTL for OIDC discovery and JWKS metadata


async def fetch_oidc_metadata(config: SSOConfig) -> dict | None:
    """Fetch OIDC discovery metadata from {config.issuer}/.well-known/openid-configuration."""
    issuer = config.issuer.rstrip("/")
    cache_entry = _METADATA_CACHE.get(issuer)
    now = time.time()

    if cache_entry and (now - cache_entry[0]) < CACHE_TTL:
        return cache_entry[1]

    discovery_url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(discovery_url)
            if resp.status_code == 200:
                data = resp.json()
                _METADATA_CACHE[issuer] = (now, data)
                return data
            log.warning("OIDC discovery metadata fetch returned status %s", resp.status_code)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to fetch OIDC discovery metadata from %s: %s", discovery_url, e)
    return None


async def fetch_jwks(jwks_uri: str) -> dict | None:
    """Fetch JWKS public key set from provider's jwks_uri."""
    cache_entry = _JWKS_CACHE.get(jwks_uri)
    now = time.time()

    if cache_entry and (now - cache_entry[0]) < CACHE_TTL:
        return cache_entry[1]

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(jwks_uri)
            if resp.status_code == 200:
                data = resp.json()
                _JWKS_CACHE[jwks_uri] = (now, data)
                return data
            log.warning("JWKS fetch returned status %s", resp.status_code)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to fetch JWKS from %s: %s", jwks_uri, e)
    return None


def build_authorization_url(config: SSOConfig, state: str, nonce: str | None = None) -> str:
    """Build the redirect URL to KTH ADFS login page via Authlib's AsyncOAuth2Client."""
    authorize_endpoint = f"{config.issuer.rstrip('/')}/oauth2/authorize"
    client = AsyncOAuth2Client(
        client_id=config.client_id,
        redirect_uri=config.redirect_uri,
        scope=config.scope,
    )
    extra_params = {}
    if nonce:
        extra_params["nonce"] = nonce
    url, _ = client.create_authorization_url(
        authorize_endpoint,
        state=state,
        **extra_params,
    )
    return url


def parse_jwt_payload_unverified(id_token: str) -> dict:
    """Decode JWT payload without verifying signature.

    Kept for fallback/testing purposes. Production flow uses decode_and_verify_id_token.
    """
    parts = id_token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    rem = len(payload_b64) % 4
    if rem > 0:
        payload_b64 += "=" * (4 - rem)
    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(decoded_bytes.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("failed to decode JWT payload: %s", e)
        return {}


def _maybe_base64_decode(val: str) -> str:
    """Decode base64 string values if sent by KTH ADFS."""
    if not val or not isinstance(val, str):
        return ""
    val = val.strip()
    if len(val) >= 4 and len(val) % 4 == 0 and re.match(r"^[A-Za-z0-9+/=]+$", val):
        try:
            decoded = base64.b64decode(val).decode("utf-8")
            if decoded and decoded.isprintable():
                return decoded.strip()
        except Exception:  # noqa: BLE001, S110
            pass
    return val


def validate_id_token_claims(
    claims: dict, config: SSOConfig, expected_nonce: str | None = None
) -> bool:
    """Validate standard OIDC claims (iss, aud, exp, nonce) in id_token."""
    now = time.time()
    exp = claims.get("exp")
    if exp is None:
        log.error("KTH OIDC id_token missing required exp claim")
        return False
    try:
        if float(exp) < now - 60:  # Allow 60s clock skew
            log.error("KTH OIDC id_token has expired (exp=%s, now=%s)", exp, now)
            return False
    except (ValueError, TypeError):
        log.error("KTH OIDC id_token exp claim invalid: %s", exp)
        return False

    aud = claims.get("aud")
    if not aud:
        log.error("KTH OIDC id_token missing required aud claim")
        return False
    if config.client_id:
        if isinstance(aud, list):
            if config.client_id not in aud:
                log.error("KTH OIDC audience mismatch: %s not in %s", config.client_id, aud)
                return False
        elif str(aud) != config.client_id:
            log.error("KTH OIDC audience mismatch: %s != %s", aud, config.client_id)
            return False

    iss = claims.get("iss")
    if iss and config.issuer:
        expected_iss = config.issuer.rstrip("/")
        actual_iss = str(iss).rstrip("/")
        if actual_iss != expected_iss:
            log.error("KTH OIDC issuer mismatch: %s != %s", actual_iss, expected_iss)
            return False

    if expected_nonce:
        actual_nonce = claims.get("nonce")
        if not actual_nonce or not secrets.compare_digest(str(actual_nonce), expected_nonce):
            log.error("KTH OIDC nonce mismatch or missing nonce in id_token")
            return False

    return True


def decode_and_verify_id_token(
    id_token: str,
    config: SSOConfig,
    jwks_data: dict | None = None,
    expected_nonce: str | None = None,
) -> dict | None:
    """Cryptographically verify the id_token signature via JWKS and validate claims."""
    if not id_token:
        return None

    if not jwks_data or not isinstance(jwks_data, dict) or "keys" not in jwks_data:
        log.error("KTH OIDC verification failed: No valid JWKS key set provided")
        return None

    try:
        keyset = KeySet.import_key_set(jwks_data)
        token_obj = jwt.decode(id_token, keyset)
        claims = dict(token_obj.claims)
    except Exception as e:  # noqa: BLE001
        log.error("KTH OIDC id_token signature verification failed: %s", e)
        return None

    if not claims:
        log.error("Failed to extract claims from id_token")
        return None

    if not validate_id_token_claims(claims, config, expected_nonce=expected_nonce):
        return None

    return claims


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

    and extract the minimal identity (kthid / username) with signature validation via Authlib.
    """
    metadata = await fetch_oidc_metadata(config)
    token_endpoint = (
        metadata.get("token_endpoint") if metadata else f"{config.issuer.rstrip('/')}/oauth2/token"
    )
    jwks_uri = (
        metadata.get("jwks_uri") if metadata else f"{config.issuer.rstrip('/')}/discovery/keys"
    )

    async with AsyncOAuth2Client(
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri=config.redirect_uri,
        timeout=10.0,
    ) as client:
        try:
            token_data = await client.fetch_token(
                token_endpoint,
                code=code,
                grant_type="authorization_code",
            )
            id_token = token_data.get("id_token")
            if not id_token:
                log.error("KTH OIDC token exchange response missing id_token")
                return None

            jwks_data = await fetch_jwks(jwks_uri)
            claims = decode_and_verify_id_token(
                id_token, config, jwks_data=jwks_data, expected_nonce=expected_nonce
            )
            if not claims:
                return None

            return extract_user_identity(claims)
        except Exception as e:  # noqa: BLE001
            log.error("KTH OIDC token exchange error: %s", e)
            return None
