#!/usr/bin/env python3
"""
server.py — small Flask wrapper around sync.py

Exposes one endpoint:
    POST /pull
        body: {"account": "Vertex Logistics", "quarter": "Q3 FY26"}
        → returns the qbr.data.json payload (also writes it to OUTPUT_DIR)

The Configurator's "Pull from Salesforce" button calls this. CORS is
permissive — this container is only ever reachable on the docker bridge
network or localhost, so cross-origin from http://localhost:8080 (deck)
to http://localhost:8081 (sync) is intended.
"""

import json
import os
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS

import sync

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})  # local-only container


@app.get("/health")
def health():
    has_creds = all(os.environ.get(k) for k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN"))
    return jsonify({
        "ok": True,
        "ready": has_creds,
        "domain": os.environ.get("SF_DOMAIN", "login"),
        "schemaVersion": sync.SCHEMA_VERSION,
    })


@app.post("/pull")
def pull():
    body = request.get_json(silent=True) or {}
    account = (body.get("account") or "").strip()
    quarter = (body.get("quarter") or sync.fmt_quarter(datetime.now())).strip()
    if not account:
        return jsonify({"error": "Missing 'account' in request body"}), 400

    try:
        sf = sync.connect()
        payload = sync.build_payload(sf, account, quarter)
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception("Salesforce pull failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    # Also write to disk for the deck's dataFile tweak
    out_dir = os.environ.get("OUTPUT_DIR", "/data/accounts")
    os.makedirs(out_dir, exist_ok=True)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in account).strip("-")
    qslug = quarter.lower().replace(" ", "-")
    out_path = os.path.join(out_dir, f"{slug}-{qslug}.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    return jsonify({"payload": payload, "savedTo": out_path})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
