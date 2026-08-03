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
    cfg = SSOConfig(
        enabled=True,
        client_id="test-client",
        redirect_uri="http://localhost:8000/auth/kth/callback",
    )
    url = build_authorization_url(cfg, state="random-state")
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


def test_sso_routes_disabled_by_default():
    app = create_app()
    client = TestClient(app)

    resp = client.get("/auth/kth/login")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"]


def test_sso_login_route_redirect(monkeypatch):
    monkeypatch.setenv("KTH_OIDC_ENABLED", "true")
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

    import student_bot.web.sso_routes as sso_routes

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
    """Verify exp, aud, and nonce validation for ID tokens."""
    import time
    from student_bot.web.kth_oidc import validate_id_token_claims

    cfg = SSOConfig(client_id="my-client-id")
    now = time.time()

    # Valid claims
    claims = {"aud": "my-client-id", "exp": now + 3600, "nonce": "nonce123"}
    assert validate_id_token_claims(claims, cfg, expected_nonce="nonce123") is True

    # Expired token
    expired_claims = {"aud": "my-client-id", "exp": now - 3600}
    assert validate_id_token_claims(expired_claims, cfg) is False

    # Audience mismatch
    bad_aud_claims = {"aud": "wrong-client", "exp": now + 3600}
    assert validate_id_token_claims(bad_aud_claims, cfg) is False

    # Nonce mismatch
    bad_nonce_claims = {"aud": "my-client-id", "exp": now + 3600, "nonce": "wrongnonce"}
    assert validate_id_token_claims(bad_nonce_claims, cfg, expected_nonce="nonce123") is False


