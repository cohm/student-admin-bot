"""Configuration loader for KTH SSO / OIDC settings.

Sources & References:
- KTH Identity Management Overview:
  https://intra.kth.se/en/it/natverk/identitetshantering/identitetshantering-pa-kth-1.1029270
- KTH SAML/OIDC Configuration Info:
  https://intra.kth.se/en/it/natverk/identitetshantering/konfigurationsinformation-for-saml-openid-connect-1.1045571
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SSOConfig:
    enabled: bool = False
    issuer: str = "https://login.ug.kth.se/adfs"
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:8000/auth/kth/callback"
    scope: str = "openid"
    token_auth_method: str = "client_secret_basic"

    @classmethod
    def from_env(cls) -> SSOConfig:
        enabled_raw = os.environ.get("KTH_OIDC_ENABLED", "false").lower()
        enabled = enabled_raw in ("1", "true", "yes", "on")
        return cls(
            enabled=enabled,
            issuer=os.environ.get("KTH_OIDC_ISSUER", "https://login.ug.kth.se/adfs").rstrip("/"),
            client_id=os.environ.get("KTH_OIDC_CLIENT_ID", ""),
            client_secret=os.environ.get("KTH_OIDC_CLIENT_SECRET", ""),
            redirect_uri=os.environ.get(
                "KTH_OIDC_REDIRECT_URI", "http://localhost:8000/auth/kth/callback"
            ),
            scope=os.environ.get("KTH_OIDC_SCOPE", "openid"),
            token_auth_method=os.environ.get("KTH_OIDC_TOKEN_AUTH_METHOD", "client_secret_basic"),
        )
