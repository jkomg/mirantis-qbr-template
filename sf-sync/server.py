#!/usr/bin/env python3
"""
server.py — small Flask wrapper around sync.py

Endpoints:
    GET  /health              sidecar status + auth mode
    GET  /accounts            search Accounts (?q=&limit=25)
    GET  /oauth/login         start Salesforce OAuth (browser redirect)
    GET  /oauth/callback      OAuth redirect target; stores tokens in session
    GET  /oauth/status        current user's Salesforce connection
    POST /oauth/logout        clear session tokens
    POST /pull                body: {"account": "...", "quarter": "..."}
    POST /review              body: {"payload": <qbr json>} — optional AI commentary

The Configurator calls these from http://localhost:8080 with credentials
included so each browser session keeps its own Salesforce login.
"""

import json
import os
import secrets
from datetime import datetime
from urllib.parse import quote

from flask import Flask, jsonify, redirect, request, session
from flask_cors import CORS
from simple_salesforce import SalesforceError

import oauth
import review
import sync

CONFIGURATOR_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]

app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "local-dev-only-set-FLASK_SECRET_KEY-for-oauth",
)
CORS(
    app,
    resources={r"/*": {"origins": CONFIGURATOR_ORIGINS, "supports_credentials": True}},
)


def _health_payload() -> dict:
    mode = oauth.auth_mode()
    connected = oauth.session_tokens(session) is not None
    username = ""
    if mode == "client_credentials":
        username = os.environ.get("SF_INTEGRATION_USER", "integration-user")
        connected = True
    elif connected:
        stored = oauth.session_tokens(session)
        assert stored is not None
        try:
            username = oauth.ensure_username(session, stored)
        except Exception:
            username = stored.get("username") or ""

    if mode == "oauth":
        ready = oauth.session_tokens(session) is not None
    elif mode == "client_credentials":
        ready = oauth.client_credentials_configured()
    elif mode == "password":
        ready = oauth.password_configured()
    else:
        ready = False

    return {
        "ok": True,
        "ready": ready,
        "authMode": mode,
        "oauthConfigured": oauth.oauth_configured(),
        "clientCredentialsConfigured": oauth.client_credentials_configured(),
        "passwordConfigured": oauth.password_configured(),
        "connected": connected if mode == "oauth" else ready,
        "username": username,
        "domain": os.environ.get("SF_DOMAIN", "login"),
        "schemaVersion": sync.SCHEMA_VERSION,
        "reviewAvailable": review.review_available(),
    }


def _is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("401", "session expired", "invalid_session", "authentication failure")
    )


def _connect_for_request():
    """Return a Salesforce client for the current request."""
    mode = oauth.auth_mode()
    if mode == "client_credentials":
        return sync.connect_client_credentials()
    if mode == "oauth":
        stored = oauth.session_tokens(session)
        if not stored:
            raise RuntimeError(
                "Not connected to Salesforce. Click Connect to Salesforce in the Configurator."
            )
        return sync.connect_from_tokens(stored["access_token"], stored["instance_url"])
    if mode == "password":
        return sync.connect_password()
    raise RuntimeError(
        "Salesforce is not configured. For sandbox Client Credentials set "
        "SF_CONSUMER_KEY, SF_CONSUMER_SECRET, and SF_DOMAIN=<my-domain>.my "
        "(e.g. mirantis--mkeops.sandbox.my). Or use browser OAuth / password auth."
    )


@app.get("/health")
def health():
    return jsonify(_health_payload())


@app.get("/oauth/login")
def oauth_login():
    if not oauth.oauth_configured():
        return jsonify({"error": "OAuth is not configured (missing SF_CONSUMER_KEY/SECRET)."}), 400
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    return redirect(oauth.authorize_url(state))


@app.get("/oauth/callback")
def oauth_callback():
    err = request.args.get("error")
    if err:
        desc = request.args.get("error_description") or err
        return redirect(f"{oauth.configurator_url()}?sf=error&msg={quote(desc)}")

    code = request.args.get("code")
    state = request.args.get("state")
    expected = session.pop("oauth_state", None)
    if not code:
        return jsonify({"error": "Missing authorization code."}), 400
    if not expected or state != expected:
        return jsonify({"error": "Invalid OAuth state — try connecting again."}), 400

    try:
        token_body = oauth.exchange_code(code)
        stored = oauth.store_tokens(session, token_body)
        oauth.ensure_username(session, stored)
    except Exception as e:
        app.logger.exception("OAuth callback failed")
        return redirect(f"{oauth.configurator_url()}?sf=error&msg={quote(str(e))}")

    return redirect(f"{oauth.configurator_url()}?sf=connected")


@app.get("/oauth/status")
def oauth_status():
    stored = oauth.session_tokens(session)
    if not stored:
        return jsonify({"connected": False, "username": ""})
    try:
        username = oauth.ensure_username(session, stored)
    except Exception:
        username = stored.get("username") or ""
    return jsonify({"connected": True, "username": username})


@app.post("/oauth/logout")
def oauth_logout():
    oauth.clear_session(session)
    return jsonify({"ok": True})


@app.get("/accounts")
def accounts():
    """Search Accounts by name fragment, or list recently modified if q is empty."""
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or 25)
    except ValueError:
        limit = 25

    try:
        sf = _connect_for_request()
        results = sync.search_accounts(sf, q, limit=limit)
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        app.logger.exception("Account search failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    return jsonify({"query": q, "count": len(results), "accounts": results})


@app.get("/inspect")
def inspect():
    """Raw SF counts/samples for an account — bypasses Configurator hydrate."""
    account = (request.args.get("account") or "").strip()
    if not account:
        return jsonify({"error": "Pass ?account=Exact Account Name"}), 400
    try:
        sf = _connect_for_request()
        return jsonify(sync.inspect_account(sf, account))
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        app.logger.exception("Inspect failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/scan")
def scan():
    """Rank recent/matching accounts by usable QBR-related Salesforce data."""
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or 25)
    except ValueError:
        limit = 25
    try:
        sf = _connect_for_request()
        return jsonify(sync.scan_accounts(sf, limit=limit, query=q))
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        app.logger.exception("Scan failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/objects")
def objects():
    """Discover queryable sObjects (Environments, Entitlements, custom __c, etc.)."""
    q = (request.args.get("q") or "").strip()
    custom_only = (request.args.get("custom") or "").lower() in ("1", "true", "yes")
    try:
        sf = _connect_for_request()
        return jsonify(sync.list_sobjects(sf, query=q, custom_only=custom_only))
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        app.logger.exception("Object list failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/object-fields")
def object_fields():
    """Describe fields for one object (?object=Entitlement__c)."""
    obj = (request.args.get("object") or "").strip()
    try:
        sf = _connect_for_request()
        return jsonify(sync.describe_object_fields(sf, obj))
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        app.logger.exception("Object describe failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/object-count")
def object_count():
    """Count rows for an account on an object (?object=X&accountId=001...)."""
    obj = (request.args.get("object") or "").strip()
    account_id = (request.args.get("accountId") or "").strip()
    try:
        sf = _connect_for_request()
        return jsonify(sync.count_for_account(sf, obj, account_id))
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        app.logger.exception("Object count failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.post("/pull")
def pull():
    body = request.get_json(silent=True) or {}
    account = (body.get("account") or "").strip()
    quarter = (body.get("quarter") or sync.fmt_quarter(datetime.now())).strip()
    if not account:
        return jsonify({"error": "Missing 'account' in request body"}), 400

    try:
        sf = _connect_for_request()
        payload = sync.build_payload(sf, account, quarter)
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    except SalesforceError as e:
        mode = oauth.auth_mode()
        if mode == "client_credentials" and _is_auth_error(e):
            # No refresh tokens in this flow — mint a fresh access token and retry once.
            try:
                sf = sync.connect_client_credentials()
                payload = sync.build_payload(sf, account, quarter)
            except Exception as retry_err:
                app.logger.exception("Salesforce pull failed after client_credentials retry")
                return jsonify({"error": f"{type(retry_err).__name__}: {retry_err}"}), 502
        elif mode == "oauth" and _is_auth_error(e):
            try:
                oauth.refresh_session_tokens(session)
                sf = _connect_for_request()
                payload = sync.build_payload(sf, account, quarter)
            except Exception as retry_err:
                app.logger.exception("Salesforce pull failed after token refresh")
                return jsonify({
                    "error": f"Session expired — reconnect to Salesforce. {retry_err}",
                }), 401
        else:
            app.logger.exception("Salesforce pull failed")
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    except Exception as e:
        app.logger.exception("Salesforce pull failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    out_dir = os.environ.get("OUTPUT_DIR", "/data/accounts")
    os.makedirs(out_dir, exist_ok=True)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in account).strip("-")
    qslug = quarter.lower().replace(" ", "-")
    out_path = os.path.join(out_dir, f"{slug}-{qslug}.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    return jsonify({"payload": payload, "savedTo": out_path})


@app.post("/review")
def review_account():
    """Optional AI color commentary from the current QBR payload."""
    body = request.get_json(silent=True) or {}
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return jsonify({"error": "Missing 'payload' object in request body"}), 400
    if not review.review_available():
        return jsonify({
            "error": (
                "AI review is not configured. Add OPENAI_API_KEY or ANTHROPIC_API_KEY "
                "to .env and restart sf-sync."
            ),
            "reviewAvailable": False,
        }), 503
    try:
        result = review.generate_review(payload)
    except Exception as e:
        app.logger.exception("AI review failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    return jsonify({"review": result, "reviewAvailable": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
