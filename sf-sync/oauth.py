"""
oauth.py — Salesforce OAuth helpers for the sf-sync sidecar.

Supported modes:
  - client_credentials  Server-to-server (Connected App key/secret → token as
                        the assigned integration user). No browser login.
  - oauth               Authorization Code (each TAM logs in via browser).
  - password            Username + password + security token (legacy / CLI).
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Optional

import requests

DEFAULT_SCOPES = "api refresh_token"


def oauth_configured() -> bool:
    return bool(os.environ.get("SF_CONSUMER_KEY") and os.environ.get("SF_CONSUMER_SECRET"))


def password_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")
    )


def client_credentials_configured() -> bool:
    """Client Credentials needs key/secret plus a My Domain (not login/test)."""
    if not oauth_configured():
        return False
    domain = (os.environ.get("SF_DOMAIN") or "").strip()
    return bool(domain) and domain not in ("login", "test")


def auth_mode() -> str:
    """Return 'client_credentials', 'oauth', 'password', or 'none'."""
    mode = os.environ.get("SF_AUTH_MODE", "auto").lower().replace("-", "_")
    if mode in ("client_credentials", "client_credential", "cc"):
        return "client_credentials" if client_credentials_configured() else "none"
    if mode == "oauth":
        return "oauth" if oauth_configured() else "none"
    if mode == "password":
        return "password" if password_configured() else "none"
    # auto: prefer client_credentials when My Domain is set (IT sandbox style),
    # else browser OAuth when keys exist, else password.
    if client_credentials_configured():
        return "client_credentials"
    if oauth_configured():
        return "oauth"
    if password_configured():
        return "password"
    return "none"


def login_host() -> str:
    """Host for authorize/token endpoints.

    Client Credentials / My Domain orgs:
        SF_DOMAIN=mirantis--mkeops.sandbox.my
        → https://mirantis--mkeops.sandbox.my.salesforce.com

    Classic login/test:
        SF_DOMAIN=login|test → https://login.salesforce.com
    """
    domain = (os.environ.get("SF_DOMAIN") or "login").strip()
    # Allow full host paste: mirantis--mkeops.sandbox.my.salesforce.com
    if domain.endswith(".salesforce.com"):
        return f"https://{domain}"
    # Allow full URL paste
    if domain.startswith("https://") or domain.startswith("http://"):
        return domain.rstrip("/")
    return f"https://{domain}.salesforce.com"


def my_domain_for_simple_salesforce() -> str:
    """Domain value for simple_salesforce (host without scheme/.salesforce.com)."""
    domain = (os.environ.get("SF_DOMAIN") or "").strip()
    if domain.startswith("https://") or domain.startswith("http://"):
        domain = domain.split("://", 1)[1]
    domain = domain.rstrip("/")
    if domain.endswith(".salesforce.com"):
        domain = domain[: -len(".salesforce.com")]
    return domain


def redirect_uri() -> str:
    return os.environ.get("SF_REDIRECT_URI", "http://localhost:8081/oauth/callback")


def configurator_url() -> str:
    return os.environ.get(
        "CONFIGURATOR_URL",
        "http://localhost:8080/QBR%20Configurator.dc.html",
    )


def authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": os.environ["SF_CONSUMER_KEY"],
        "redirect_uri": redirect_uri(),
        "scope": os.environ.get("SF_OAUTH_SCOPES", DEFAULT_SCOPES),
        "state": state,
    }
    return f"{login_host()}/services/oauth2/authorize?{urllib.parse.urlencode(params)}"


def _token_request(data: Dict[str, str]) -> Dict[str, Any]:
    resp = requests.post(
        f"{login_host()}/services/oauth2/token",
        data=data,
        timeout=30,
    )
    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Salesforce token response was not JSON: {resp.text[:200]}") from exc
    if resp.status_code != 200:
        err = body.get("error_description") or body.get("error") or resp.text
        raise RuntimeError(f"Salesforce OAuth error: {err}")
    return body


def exchange_code(code: str) -> Dict[str, Any]:
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": os.environ["SF_CONSUMER_KEY"],
            "client_secret": os.environ["SF_CONSUMER_SECRET"],
            "redirect_uri": redirect_uri(),
        }
    )


def client_credentials_token() -> Dict[str, Any]:
    """Exchange Connected App key/secret for an access token (integration user)."""
    return _token_request(
        {
            "grant_type": "client_credentials",
            "client_id": os.environ["SF_CONSUMER_KEY"],
            "client_secret": os.environ["SF_CONSUMER_SECRET"],
        }
    )


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    return _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.environ["SF_CONSUMER_KEY"],
            "client_secret": os.environ["SF_CONSUMER_SECRET"],
        }
    )


def fetch_identity(access_token: str, id_url: str) -> Dict[str, Any]:
    resp = requests.get(
        id_url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def session_tokens(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = session.get("sf_oauth")
    if not isinstance(data, dict):
        return None
    if not data.get("access_token") or not data.get("instance_url"):
        return None
    return data


def clear_session(session: Dict[str, Any]) -> None:
    session.pop("sf_oauth", None)
    session.pop("oauth_state", None)


def store_tokens(session: Dict[str, Any], token_body: Dict[str, Any]) -> Dict[str, Any]:
    """Persist token response in the Flask session; return the stored dict."""
    existing = session_tokens(session) or {}
    stored: Dict[str, Any] = {
        "access_token": token_body["access_token"],
        "instance_url": token_body["instance_url"],
    }
    refresh = token_body.get("refresh_token") or existing.get("refresh_token")
    if refresh:
        stored["refresh_token"] = refresh
    if token_body.get("id"):
        stored["id_url"] = token_body["id"]
    if existing.get("username"):
        stored["username"] = existing["username"]
    session["sf_oauth"] = stored
    return stored


def ensure_username(session: Dict[str, Any], stored: Dict[str, Any]) -> str:
    if stored.get("username"):
        return stored["username"]
    id_url = stored.get("id_url")
    if not id_url:
        return ""
    identity = fetch_identity(stored["access_token"], id_url)
    username = identity.get("username") or identity.get("email") or identity.get("user_id") or ""
    stored["username"] = username
    session["sf_oauth"] = stored
    return username


def refresh_session_tokens(session: Dict[str, Any]) -> Dict[str, Any]:
    stored = session_tokens(session)
    if not stored or not stored.get("refresh_token"):
        raise RuntimeError("No refresh token — reconnect to Salesforce.")
    token_body = refresh_access_token(stored["refresh_token"])
    return store_tokens(session, token_body)
