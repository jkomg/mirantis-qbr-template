"""
oauth.py — Salesforce OAuth 2.0 Authorization Code flow for the sf-sync sidecar.

Each TAM authenticates with their own Salesforce identity in the browser.
Tokens are stored in the Flask session (signed cookie) for that browser only.
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


def auth_mode() -> str:
    """Return 'oauth', 'password', or 'none'."""
    mode = os.environ.get("SF_AUTH_MODE", "auto").lower()
    if mode == "oauth":
        return "oauth" if oauth_configured() else "none"
    if mode == "password":
        return "password" if password_configured() else "none"
    # auto: prefer OAuth when Connected App creds are present
    if oauth_configured():
        return "oauth"
    if password_configured():
        return "password"
    return "none"


def login_host() -> str:
    domain = os.environ.get("SF_DOMAIN", "login")
    return f"https://{domain}.salesforce.com"


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
