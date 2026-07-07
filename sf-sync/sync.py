#!/usr/bin/env python3
"""
sync.py — Salesforce → qbr.data.json

CLI usage:
    python sync.py --account "Vertex Logistics" --quarter "Q3 FY26" \\
        --out /data/accounts/vertex-q3fy26.json

Auth (pick one — server prefers OAuth when Connected App creds are set):

    OAuth (per-user login in the Configurator):
        SF_CONSUMER_KEY         Connected App consumer key
        SF_CONSUMER_SECRET      Connected App consumer secret
        SF_REDIRECT_URI         default http://localhost:8081/oauth/callback
        SF_AUTH_MODE            auto | oauth | password  (default: auto)

    Password (CLI / shared service account):
        SF_USERNAME             service account username
        SF_PASSWORD             account password (no token suffix)
        SF_SECURITY_TOKEN       security token
        SF_DOMAIN               'login' (production) or 'test' (sandbox)

Optional:
    OUTPUT_DIR              where to write JSON. Default /data/accounts

Schema:
    Output matches qbr.data.json (the deck's contract). Unmapped narrative
    sections — wins, risks, incidents, asks, training, roadmaps — are emitted
    as empty arrays. The TAM fills those in via the Configurator on top.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from simple_salesforce import Salesforce, SalesforceError

SCHEMA_VERSION = "qbr-2026.06"


# ---------------------------------------------------------------------------
# Salesforce connection
# ---------------------------------------------------------------------------
def connect_password() -> Salesforce:
    user = os.environ.get("SF_USERNAME")
    pwd = os.environ.get("SF_PASSWORD")
    token = os.environ.get("SF_SECURITY_TOKEN")
    domain = os.environ.get("SF_DOMAIN", "login")
    if not (user and pwd and token):
        raise SystemExit(
            "Missing env vars. Set SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN."
        )
    return Salesforce(username=user, password=pwd, security_token=token, domain=domain)


def connect_from_tokens(access_token: str, instance_url: str) -> Salesforce:
    """Build a Salesforce client from an OAuth access token (web session)."""
    return Salesforce(instance_url=instance_url, session_id=access_token)


def connect() -> Salesforce:
    """CLI entry point — always uses password auth from env."""
    return connect_password()


# ---------------------------------------------------------------------------
# SOQL queries — adjust field names to match the Mirantis SF org.
# Custom fields end in __c. If a field doesn't exist in your org, the query
# returns INVALID_FIELD and sync fails with a clear message.
# ---------------------------------------------------------------------------
SOQL_ACCOUNT = """
SELECT Id, Name, Type, Industry, NumberOfEmployees,
       AnnualRevenue,
       Owner.Name, Owner.Email
FROM Account
WHERE Name = '{name}'
LIMIT 1
"""

SOQL_OPPS_OPEN = """
SELECT Id, Name, Amount, CloseDate, StageName, Type, Probability
FROM Opportunity
WHERE AccountId = '{account_id}' AND IsClosed = false
ORDER BY CloseDate ASC
LIMIT 25
"""

SOQL_OPPS_RECENT_CLOSED = """
SELECT Id, Name, Amount, CloseDate, StageName, IsWon
FROM Opportunity
WHERE AccountId = '{account_id}' AND IsClosed = true AND CloseDate = LAST_N_DAYS:120
ORDER BY CloseDate DESC
LIMIT 10
"""

SOQL_ASSETS = """
SELECT Id, Name, Quantity, Status, Product2.Name, Product2.Family
FROM Asset
WHERE AccountId = '{account_id}' AND Status = 'Installed'
LIMIT 50
"""

SOQL_CONTACTS = """
SELECT Id, Name, Title, Email
FROM Contact
WHERE AccountId = '{account_id}'
ORDER BY LastModifiedDate DESC
LIMIT 5
"""


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def fmt_quarter(d: datetime) -> str:
    return f"Q{(d.month - 1) // 3 + 1} FY{d.year}"


def quarter_from_iso(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return fmt_quarter(datetime.fromisoformat(iso.replace("Z", "+00:00")))
    except Exception:
        return ""


def safe_get(rec: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    cur: Any = rec
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def soql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def query_records(sf: Salesforce, soql: str) -> List[Dict[str, Any]]:
    result = sf.query(soql)
    return result.get("records", [])


def build_payload(
    sf: Salesforce, account_name: str, quarter: str
) -> Dict[str, Any]:
    safe_name = soql_escape(account_name)
    accounts = query_records(sf, SOQL_ACCOUNT.format(name=safe_name))
    if not accounts:
        raise SystemExit(
            f"Account not found in Salesforce: {account_name!r}\n"
            "Check the name (case-sensitive in SOQL) or your service account's access."
        )
    acct = accounts[0]
    account_id = acct["Id"]

    opps_open = query_records(sf, SOQL_OPPS_OPEN.format(account_id=account_id))
    opps_recent = query_records(sf, SOQL_OPPS_RECENT_CLOSED.format(account_id=account_id))
    assets = query_records(sf, SOQL_ASSETS.format(account_id=account_id))
    contacts = query_records(sf, SOQL_CONTACTS.format(account_id=account_id))

    # ---- Customer + commercial ----
    arr_current = safe_get(acct, "AnnualRevenue", default=0) or 0
    arr_prior = arr_current  # SF Account doesn't expose prior ARR directly; tweak in Configurator
    expansions = [
        {
            "name": o["Name"],
            "valueUSD": o.get("Amount") or 0,
            "quarter": quarter_from_iso(o.get("CloseDate")),
            "stage": o.get("StageName") or "",
            "probability": o.get("Probability") or 0,
        }
        for o in opps_open
        if (o.get("Amount") or 0) > 0
    ]

    payload: Dict[str, Any] = {
        "_meta": {
            "schemaVersion": SCHEMA_VERSION,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "source": f"sf-sync/{os.environ.get('SYNC_VERSION', 'v0.1')} (simple-salesforce)",
            "accountId": account_id,
        },
        "customer": {
            "name": acct["Name"],
            "tier": acct.get("Type") or "Strategic",
            "industry": acct.get("Industry") or "",
            "stakeholders": [
                {"name": c["Name"], "title": c.get("Title") or ""}
                for c in contacts
                if c.get("Name")
            ],
        },
        "quarter": quarter,
        "preparedBy": safe_get(acct, "Owner", "Name", default=""),
        "preparedByEmail": safe_get(acct, "Owner", "Email", default=""),
        "presentationDate": datetime.now().date().isoformat(),
        "nextQbr": {"label": "", "date": ""},
        "commercial": {
            "arr": {
                "current": arr_current,
                "prior": arr_prior,
                "yoyPct": 0,
            },
            "renewalDate": "",     # not standard on Account; add a custom field if needed
            "renewalSponsor": "",
            "expansions": expansions,
            "_recentClosed": [
                {
                    "name": o["Name"],
                    "amount": o.get("Amount") or 0,
                    "closeDate": o.get("CloseDate") or "",
                    "won": bool(o.get("IsWon")),
                }
                for o in opps_recent
            ],
        },
        # Telemetry / support / NPS — NOT in Salesforce. Empty arrays so the
        # Configurator's defaults / TAM's edits fill these in.
        "usage": {
            "clusters": 0, "clustersDelta": 0,
            "nodes": 0, "nodesDelta": 0,
            "workloads": 0, "workloadsDelta": 0,
            "environments": 0, "uptime": 0,
        },
        "support": {
            "ticketsTotal": 0, "p1Count": 0, "p1Delta": 0,
            "slaMetPct": 0, "slaDeltaPp": 0,
            "p1MttrHours": 0, "p1MttrTargetHours": 3.0, "csat": 0,
        },
        "nps": {"score": 0, "industry": 30, "delta": 0},

        # Products from active Assets
        "products": sorted({
            safe_get(a, "Product2", "Name", default="")
            for a in assets
            if safe_get(a, "Product2", "Name")
        }),
        "productMix": [
            {
                "product": safe_get(a, "Product2", "Name", default=""),
                "entitlement": f"{a.get('Quantity') or 0}",
                "inUse": "",
                "utilizationPct": 0,
                "trend": "— from SF Asset",
            }
            for a in assets
            if safe_get(a, "Product2", "Name")
        ],

        # Narrative — TAM-curated, always empty from SF
        "incidents": [],
        "wins": [],
        "risks": [],
        "mirantisRoadmap": [],
        "customerRoadmap": [],
        "training": {"delivered": [], "planned": [], "deliveredNote": "", "plannedNote": ""},
        "execSummaryTakeaways": [],
        "asks": {"fromUs": [], "fromYou": []},
        "nextActions": [],
        "previousActions": [],

        # Section toggles default to all-on
        "sections": {
            k: True for k in [
                "execSummary", "accountHealth", "goalsRecap", "usage",
                "supportDeepDive", "incidents", "wins", "risks",
                "mirantisRoadmap", "customerRoadmap", "renewal", "training",
                "asks", "nextQuarter", "asksTracker", "appendix",
            ]
        },
    }

    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Pull a Salesforce account into qbr.data.json")
    p.add_argument("--account", required=True, help='Salesforce Account name (exact match)')
    p.add_argument("--quarter", default=fmt_quarter(datetime.now()),
                   help='Quarter label (e.g. "Q3 FY26"). Default: current calendar quarter.')
    p.add_argument("--out", default=None,
                   help='Output path. Default: $OUTPUT_DIR/{slug}-{quarter}.json')
    p.add_argument("--stdout", action="store_true", help="Print JSON to stdout instead of writing a file")
    args = p.parse_args()

    try:
        sf = connect()
        payload = build_payload(sf, args.account, args.quarter)
    except SalesforceError as e:
        print(f"Salesforce error: {e}", file=sys.stderr)
        return 2

    body = json.dumps(payload, indent=2, default=str)

    if args.stdout:
        print(body)
        return 0

    if args.out:
        out_path = Path(args.out)
    else:
        out_dir = Path(os.environ.get("OUTPUT_DIR", "/data/accounts"))
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = "".join(c.lower() if c.isalnum() else "-" for c in args.account).strip("-")
        qslug = args.quarter.lower().replace(" ", "-")
        out_path = out_dir / f"{slug}-{qslug}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body)
    print(f"✓ Wrote {out_path}")
    print(f"  {len(payload['products'])} products · {len(payload['commercial']['expansions'])} open opps · "
          f"{len(payload['customer']['stakeholders'])} contacts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
