"""Unit tests for KTH SSO / OIDC authentication module."""

import base64

from fastapi.testclient import TestClient

from student_bot.web.app import create_app
from student_bot.web.kth_oidc import (
    build_authorization_url,
    extract_user_identity,
    parse_jwt_payload_unverified,
)
from student_bot.web.sso_config import SSOConfig


def test_sso_config_from_env(monkeypatch):
    monkeypatch.setenv("KTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("KTH_OIDC_CLIENT_ID", "my-app-id")
    monkeypatch.setenv("KTH_OIDC_CLIENT_SECRET", "secret-key")
    monkeypatch.delenv("KTH_OIDC_ISSUER", raising=False)

    cfg = SSOConfig.from_env()
    assert cfg.enabled is True
    assert cfg.client_id == "my-app-id"
    assert cfg.client_secret == "secret-key"
    assert cfg.issuer == "https://login.ug.kth.se/adfs"
    assert cfg.scope == "openid"


def test_extract_user_identity_privacy_scope():
    """Verify that ONLY kthid and username are extracted, ignoring all personal data."""
    raw_claims = {
        "kthid": "u100001",
        "username": "studentuser",
        "email": "studentuser@kth.se",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name": "Student User",
        "affiliation": "student",
        "memberOf": ["group1", "group2"],
    }

    identity = extract_user_identity(raw_claims)
    assert identity == {"kthid": "u100001", "username": "studentuser"}
    assert "email" not in identity
    assert "displayName" not in identity
    assert "affiliation" not in identity
    assert "memberOf" not in identity


def test_extract_user_identity_fallback_sub():
    raw_claims = {"sub": "u100002"}
    identity = extract_user_identity(raw_claims)
    assert identity == {"username": "u100002"}


def test_build_authorization_url():
    import asyncio

    cfg = SSOConfig(
        enabled=True,
        client_id="test-client",
        redirect_uri="http://localhost:8000/auth/kth/callback",
    )
    url = asyncio.run(build_authorization_url(cfg, state="random-state"))
    assert "https://login.ug.kth.se/adfs/oauth2/authorize" in url
    assert "client_id=test-client" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fauth%2Fkth%2Fcallback" in url
    assert "state=random-state" in url


def test_parse_jwt_payload_unverified():
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("utf-8").rstrip("=")
    payload = (
        base64.urlsafe_b64encode(b'{"kthid":"u999","username":"testuser"}')
        .decode("utf-8")
        .rstrip("=")
    )
    jwt = f"{header}.{payload}.signature"

    claims = parse_jwt_payload_unverified(jwt)
    assert claims == {"kthid": "u999", "username": "testuser"}


def test_sso_routes_disabled_by_default(monkeypatch):
    monkeypatch.setenv("KTH_OIDC_ENABLED", "false")
    app = create_app()
    client = TestClient(app)

    resp = client.get("/auth/kth/login")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"]


def test_sso_login_route_redirect(monkeypatch):
    monkeypatch.setenv("KTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("KTH_OIDC_ISSUER", "https://login.ug.kth.se/adfs")
    monkeypatch.setenv("KTH_OIDC_CLIENT_ID", "my-client")

    app = create_app()
    client = TestClient(app)

    resp = client.get("/auth/kth/login", follow_redirects=False)
    assert resp.status_code == 307
    assert "https://login.ug.kth.se/adfs/oauth2/authorize" in resp.headers["location"]


def test_extract_user_identity_adfs_xml_schema_and_upn():
    raw_claims = {
        "http://schemas.kth.se/2012/01/kthid": "u100003",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn": "studentuser@kth.se",
    }
    identity = extract_user_identity(raw_claims)
    assert identity == {"kthid": "u100003", "username": "studentuser"}


def test_callback_csrf_protection_rejects_missing_or_mismatched_state(monkeypatch):
    monkeypatch.setenv("KTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("KTH_OIDC_CLIENT_ID", "my-client")

    app = create_app()
    client = TestClient(app)

    # 1. Missing authorization code
    resp = client.get("/auth/kth/callback?state=somestate")
    assert resp.status_code == 400
    assert "code" in resp.json()["detail"].lower()

    # 2. Missing state parameter
    resp = client.get("/auth/kth/callback?code=somecode")
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()

    # 3. State mismatch (saved vs query)
    client.get("/auth/kth/login", follow_redirects=False)
    resp = client.get("/auth/kth/callback?code=somecode&state=wrongstate")
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()


def test_sso_callback_grants_access_and_session(monkeypatch):
    monkeypatch.setenv("KTH_OIDC_ENABLED", "true")
    monkeypatch.setenv("KTH_OIDC_CLIENT_ID", "my-client")
    monkeypatch.setenv("WEB_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "secretaccess")

    from student_bot.web import sso_routes

    async def mock_exchange(code, config, expected_nonce=None):
        return {"kthid": "u100001", "username": "teststudent"}

    monkeypatch.setattr(sso_routes, "exchange_code_for_identity", mock_exchange)

    app = create_app()
    client = TestClient(app)

    # Trigger login to set oidc_state in session
    login_resp = client.get("/auth/kth/login", follow_redirects=False)
    assert login_resp.status_code == 307
    location = login_resp.headers["location"]
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    state = qs["state"][0]

    # Callback with valid code and matching state
    cb_resp = client.get(f"/auth/kth/callback?code=testcode&state={state}", follow_redirects=False)
    assert cb_resp.status_code == 303

    # Verify protected endpoint accepts the SSO session without Basic Auth or ?access=
    health_resp = client.get("/api/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"


def test_extract_user_identity_base64_encoded_attributes():
    """Verify that base64-encoded claim values from KTH ADFS are correctly decoded."""
    # "u100001" in base64 is "dTEwMDAwMQ=="
    raw_claims = {
        "kthid": base64.b64encode(b"u100001").decode("utf-8"),
        "username": base64.b64encode(b"studentuser").decode("utf-8"),
    }
    identity = extract_user_identity(raw_claims)
    assert identity == {"kthid": "u100001", "username": "studentuser"}


def test_validate_id_token_claims():
    """Verify exp, aud, iss, and nonce validation for ID tokens."""
    import time

    from student_bot.web.kth_oidc import validate_id_token_claims

    cfg = SSOConfig(client_id="my-client-id", issuer="https://login.ug.kth.se/adfs")
    now = time.time()

    # Valid claims
    claims = {
        "aud": "my-client-id",
        "iss": "https://login.ug.kth.se/adfs",
        "exp": now + 3600,
        "nonce": "nonce123",
    }
    assert validate_id_token_claims(claims, cfg, expected_nonce="nonce123") is True

    # Expired token
    expired_claims = {"aud": "my-client-id", "iss": "https://login.ug.kth.se/adfs", "exp": now - 3600}
    assert validate_id_token_claims(expired_claims, cfg) is False

    # Audience mismatch
    bad_aud_claims = {"aud": "wrong-client", "iss": "https://login.ug.kth.se/adfs", "exp": now + 3600}
    assert validate_id_token_claims(bad_aud_claims, cfg) is False

    # Nonce mismatch
    bad_nonce_claims = {
        "aud": "my-client-id",
        "iss": "https://login.ug.kth.se/adfs",
        "exp": now + 3600,
        "nonce": "wrongnonce",
    }
    assert validate_id_token_claims(bad_nonce_claims, cfg, expected_nonce="nonce123") is False

    # Missing nonce when expected_nonce is required
    missing_nonce_claims = {
        "aud": "my-client-id",
        "iss": "https://login.ug.kth.se/adfs",
        "exp": now + 3600,
    }
    assert validate_id_token_claims(missing_nonce_claims, cfg, expected_nonce="nonce123") is False

    # Missing exp claim (OIDC Core 1.0 §2: exp is mandatory)
    no_exp_claims = {"aud": "my-client-id", "iss": "https://login.ug.kth.se/adfs"}
    assert validate_id_token_claims(no_exp_claims, cfg) is False

    # Missing aud claim (OIDC Core 1.0 §2: aud is mandatory)
    no_aud_claims = {"iss": "https://login.ug.kth.se/adfs", "exp": now + 3600}
    assert validate_id_token_claims(no_aud_claims, cfg) is False

    # Missing iss claim (OIDC Core 1.0 §2: iss is mandatory)
    no_iss_claims = {"aud": "my-client-id", "exp": now + 3600}
    assert validate_id_token_claims(no_iss_claims, cfg) is False


def test_decode_and_verify_id_token_rs256_signature():
    """Verify RS256 signature verification with standard joserfc JWKS."""
    import time

    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from student_bot.web.kth_oidc import decode_and_verify_id_token

    cfg = SSOConfig(client_id="test-client", issuer="https://login.ug.kth.se/adfs")

    # Generate RSA Key Pair
    priv_key = RSAKey.generate_key(2048, {"kid": "key1"})
    pub_jwk = priv_key.as_dict()
    jwks_data = {"keys": [pub_jwk]}

    now = int(time.time())
    payload = {
        "iss": "https://login.ug.kth.se/adfs",
        "aud": "test-client",
        "exp": now + 3600,
        "kthid": "u100099",
        "username": "teststudent",
    }
    signed_jwt = jwt.encode({"alg": "RS256", "kid": "key1"}, payload, priv_key)

    # 1. Verification with matching JWKS key set succeeds
    verified_claims = decode_and_verify_id_token(signed_jwt, cfg, jwks_data=jwks_data)
    assert verified_claims is not None
    assert verified_claims["kthid"] == "u100099"
    assert verified_claims["username"] == "teststudent"

    # 2. Forged signature (tampered payload or wrong key) is strictly rejected
    other_priv_key = RSAKey.generate_key(2048, {"kid": "key1"})
    forged_jwt = jwt.encode({"alg": "RS256", "kid": "key1"}, payload, other_priv_key)
    rejected_claims = decode_and_verify_id_token(forged_jwt, cfg, jwks_data=jwks_data)
    assert rejected_claims is None

    # 3. Missing or invalid JWKS data is fail-closed (returns None)
    assert decode_and_verify_id_token(signed_jwt, cfg, jwks_data=None) is None
    assert decode_and_verify_id_token(signed_jwt, cfg, jwks_data={}) is None


def test_algorithm_confusion_rejected_by_keyset():
    """Prove that joserfc rejects algorithm confusion attacks via key type enforcement.

    Background: A classic JWT attack is to forge a token with alg:"none" (skip
    verification) or alg:"HS256" (use the RSA public key as an HMAC secret).
    Naive implementations that trust the JWT header's `alg` field are vulnerable.

    joserfc's KeySet.import_key_set + jwt.decode is NOT vulnerable because it
    selects the verification algorithm based on the *key material* (RSA keys
    can only verify RSA algorithms), not the attacker-controlled JWT header.
    This test proves that behaviour, making manual header-parsing unnecessary.
    """
    import base64
    import hashlib
    import hmac
    import json
    import time

    from joserfc.jwk import RSAKey

    from student_bot.web.kth_oidc import decode_and_verify_id_token

    cfg = SSOConfig(client_id="test-client", issuer="https://login.ug.kth.se/adfs")

    # Set up a legitimate RSA key pair and JWKS
    priv_key = RSAKey.generate_key(2048, {"kid": "key1"})
    pub_jwk = priv_key.as_dict()
    jwks_data = {"keys": [pub_jwk]}

    now = int(time.time())
    payload = {
        "iss": "https://login.ug.kth.se/adfs",
        "aud": "test-client",
        "exp": now + 3600,
        "kthid": "u100099",
        "username": "attacker",
    }
    payload_json = json.dumps(payload, separators=(",", ":"))

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    # --- Attack 1: alg:"none" ---
    # An attacker crafts a token claiming no signature is needed.
    none_header = b64url(json.dumps({"alg": "none", "kid": "key1"}).encode())
    none_payload = b64url(payload_json.encode())
    none_token = f"{none_header}.{none_payload}."

    result = decode_and_verify_id_token(none_token, cfg, jwks_data=jwks_data)
    assert result is None, "alg:none token must be rejected"

    # --- Attack 2: alg:"HS256" with RSA public key as HMAC secret ---
    # The attacker takes the public RSA key (available via JWKS) and uses it
    # as the shared secret for HMAC-SHA256, hoping the verifier will trust
    # the alg header and verify with the same public key bytes.
    hs256_header = b64url(json.dumps({"alg": "HS256", "kid": "key1"}).encode())
    hs256_payload = b64url(payload_json.encode())
    signing_input = f"{hs256_header}.{hs256_payload}".encode()

    # Use the raw public key JSON as the HMAC secret (standard confusion attack)
    pub_key_bytes = json.dumps(pub_jwk, separators=(",", ":")).encode()
    sig = hmac.new(pub_key_bytes, signing_input, hashlib.sha256).digest()
    hs256_token = f"{hs256_header}.{hs256_payload}.{b64url(sig)}"

    result = decode_and_verify_id_token(hs256_token, cfg, jwks_data=jwks_data)
    assert result is None, "alg:HS256 token forged with RSA public key must be rejected"

    # --- Attack 3: alg:"HS256" with empty signature ---
    empty_sig_token = f"{hs256_header}.{hs256_payload}.{b64url(b'')}"

    result = decode_and_verify_id_token(empty_sig_token, cfg, jwks_data=jwks_data)
    assert result is None, "alg:HS256 token with empty signature must be rejected"


def test_exchange_code_for_identity_authlib_client(monkeypatch):
    """Verify exchange_code_for_identity using Authlib AsyncOAuth2Client."""
    import asyncio
    import time

    from authlib.integrations.httpx_client import AsyncOAuth2Client
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from student_bot.web import kth_oidc

    cfg = SSOConfig(
        enabled=True,
        client_id="test-client",
        client_secret="test-secret",
        redirect_uri="http://localhost:8000/auth/kth/callback",
        issuer="https://login.ug.kth.se/adfs",
    )

    priv_key = RSAKey.generate_key(2048, {"kid": "key1"})
    pub_jwk = priv_key.as_dict()
    jwks_data = {"keys": [pub_jwk]}

    now = int(time.time())
    payload = {
        "iss": "https://login.ug.kth.se/adfs",
        "aud": "test-client",
        "exp": now + 3600,
        "kthid": "u100088",
        "username": "authlibuser",
    }
    id_token = jwt.encode({"alg": "RS256", "kid": "key1"}, payload, priv_key)

    async def mock_fetch_metadata(c):
        return {
            "token_endpoint": "https://login.ug.kth.se/adfs/oauth2/token",
            "jwks_uri": "https://login.ug.kth.se/adfs/discovery/keys",
        }

    async def mock_fetch_jwks(uri):
        return jwks_data

    monkeypatch.setattr(kth_oidc, "fetch_oidc_metadata", mock_fetch_metadata)
    monkeypatch.setattr(kth_oidc, "fetch_jwks", mock_fetch_jwks)

    async def mock_fetch_token(self, url, code=None, grant_type=None, **kwargs):
        return {"id_token": id_token, "access_token": "acc123", "token_type": "Bearer"}

    monkeypatch.setattr(AsyncOAuth2Client, "fetch_token", mock_fetch_token)

    identity = asyncio.run(kth_oidc.exchange_code_for_identity("testcode", cfg))
    assert identity == {"kthid": "u100088", "username": "authlibuser"}
