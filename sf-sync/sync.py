#!/usr/bin/env python3
"""
sync.py — Salesforce → qbr.data.json

CLI usage:
    python sync.py --account "Vertex Logistics" --quarter "Q3 FY26" \\
        --out /data/accounts/vertex-q3fy26.json

Auth (pick one — default auto detects):

    Client Credentials (server-to-server Connected App — recommended for sandbox):
        SF_CONSUMER_KEY         Connected App consumer key
        SF_CONSUMER_SECRET      Connected App consumer secret
        SF_DOMAIN               My Domain host, e.g. mirantis--mkeops.sandbox.my
                                (NOT login/test — required for this flow)
        SF_AUTH_MODE            client_credentials | auto | oauth | password

    OAuth Authorization Code (per-user login in the Configurator):
        SF_CONSUMER_KEY / SF_CONSUMER_SECRET
        SF_REDIRECT_URI         default http://localhost:8081/oauth/callback
        SF_DOMAIN               login | test | My Domain

    Password (CLI / legacy):
        SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN
        SF_DOMAIN               login | test

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
from typing import Any, Dict, List, Optional, Tuple

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


def connect_client_credentials() -> Salesforce:
    """OAuth 2.0 Client Credentials — token as the Connected App integration user."""
    import oauth  # local import avoids circular use at module load

    key = os.environ.get("SF_CONSUMER_KEY")
    secret = os.environ.get("SF_CONSUMER_SECRET")
    if not (key and secret):
        raise SystemExit("Missing SF_CONSUMER_KEY / SF_CONSUMER_SECRET.")
    domain = oauth.my_domain_for_simple_salesforce()
    if not domain or domain in ("login", "test"):
        raise SystemExit(
            "Client Credentials requires SF_DOMAIN set to your My Domain "
            "(e.g. mirantis--mkeops.sandbox.my), not login/test."
        )
    return Salesforce(consumer_key=key, consumer_secret=secret, domain=domain)


def connect_from_tokens(access_token: str, instance_url: str) -> Salesforce:
    """Build a Salesforce client from an OAuth access token (web session)."""
    return Salesforce(instance_url=instance_url, session_id=access_token)


def connect() -> Salesforce:
    """CLI entry point — prefers client_credentials, then password."""
    import oauth

    mode = oauth.auth_mode()
    if mode == "client_credentials":
        return connect_client_credentials()
    if mode == "password":
        return connect_password()
    if oauth.client_credentials_configured():
        return connect_client_credentials()
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

SOQL_ACCOUNT_SEARCH = """
SELECT Id, Name, Type, Industry, Owner.Name
FROM Account
WHERE Name LIKE '{pattern}'
ORDER BY Name ASC
LIMIT {limit}
"""

SOQL_ACCOUNT_RECENT = """
SELECT Id, Name, Type, Industry, Owner.Name
FROM Account
ORDER BY LastModifiedDate DESC
LIMIT {limit}
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


def query_records_optional(
    sf: Salesforce, soql: str, label: str
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run SOQL; on missing object/field return [] + warning instead of failing the pull."""
    try:
        return query_records(sf, soql), None
    except SalesforceError as e:
        text = str(e)
        soft = any(
            marker in text
            for marker in (
                "INVALID_TYPE",
                "INVALID_FIELD",
                "is not supported",
                "No such column",
            )
        )
        if soft:
            warn = f"{label} skipped — Salesforce returned: {text.splitlines()[-1] if text else e}"
            print(warn, file=sys.stderr)
            return [], warn
        raise


def search_accounts(
    sf: Salesforce, query: str = "", limit: int = 25
) -> List[Dict[str, Any]]:
    """Return Account rows matching a name fragment, or recently modified if blank."""
    limit = max(1, min(int(limit or 25), 50))
    q = (query or "").strip()
    if q:
        # SOQL LIKE: % is wildcard; escape \ and '
        pattern = soql_escape(q).replace("%", r"\%").replace("_", r"\_")
        soql = SOQL_ACCOUNT_SEARCH.format(pattern=f"%{pattern}%", limit=limit)
    else:
        soql = SOQL_ACCOUNT_RECENT.format(limit=limit)

    rows = []
    for rec in query_records(sf, soql):
        rows.append(
            {
                "id": rec.get("Id") or "",
                "name": rec.get("Name") or "",
                "type": rec.get("Type") or "",
                "industry": rec.get("Industry") or "",
                "owner": safe_get(rec, "Owner", "Name", default="") or "",
            }
        )
    return rows


def inspect_account(sf: Salesforce, account_name: str) -> Dict[str, Any]:
    """Return raw counts + samples so we can see what SF has without form hydrate."""
    import mirantis

    safe_name = soql_escape(account_name)
    accounts = query_records(sf, SOQL_ACCOUNT.format(name=safe_name))
    if not accounts:
        return {
            "found": False,
            "account": account_name,
            "error": f"No Account with exact Name={account_name!r}",
        }

    acct = accounts[0]
    account_id = acct["Id"]
    opps_open, w1 = query_records_optional(
        sf, SOQL_OPPS_OPEN.format(account_id=account_id), "Open Opportunities"
    )
    opps_recent, w2 = query_records_optional(
        sf, SOQL_OPPS_RECENT_CLOSED.format(account_id=account_id), "Recent closed Opportunities"
    )
    contacts, w3 = query_records_optional(
        sf, SOQL_CONTACTS.format(account_id=account_id), "Contacts"
    )
    bundle = mirantis.fetch_mirantis_bundle(sf, account_id)
    warnings = [w for w in (w1, w2, w3, *bundle.get("warnings", [])) if w]

    revenue = acct.get("AnnualRevenue")
    try:
        revenue_n = float(revenue) if revenue is not None else 0.0
    except (TypeError, ValueError):
        revenue_n = 0.0

    envs = bundle.get("environments") or []
    licenses = bundle.get("licenses") or []
    cases = bundle.get("cases") or []
    entitlements = bundle.get("entitlements") or []

    signals = {
        "hasRevenue": revenue_n > 0,
        "hasOwner": bool(safe_get(acct, "Owner", "Name", default="")),
        "hasOpenOpps": len(opps_open) > 0,
        "hasRecentClosedOpps": len(opps_recent) > 0,
        "hasContacts": len(contacts) > 0,
        "hasEnvironments": len(envs) > 0,
        "hasLicenses": len(licenses) > 0,
        "hasCases": len(cases) > 0,
        "hasEntitlements": len(entitlements) > 0,
    }
    score = sum(
        [
            3 if signals["hasRevenue"] else 0,
            3 if signals["hasEnvironments"] else 0,
            3 if signals["hasLicenses"] else 0,
            2 if signals["hasCases"] else 0,
            2 if signals["hasEntitlements"] else 0,
            2 if signals["hasOpenOpps"] else 0,
            1 if signals["hasRecentClosedOpps"] else 0,
            2 if signals["hasContacts"] else 0,
            1 if signals["hasOwner"] else 0,
        ]
    )
    usable = score >= 3

    return {
        "found": True,
        "usable": usable,
        "score": score,
        "signals": signals,
        "account": {
            "id": account_id,
            "name": acct.get("Name"),
            "type": acct.get("Type"),
            "industry": acct.get("Industry"),
            "annualRevenue": acct.get("AnnualRevenue"),
            "ownerName": safe_get(acct, "Owner", "Name", default=""),
            "ownerEmail": safe_get(acct, "Owner", "Email", default=""),
        },
        "counts": {
            "opportunitiesOpen": len(opps_open),
            "opportunitiesRecentClosed": len(opps_recent),
            "contacts": len(contacts),
            "environments": len(envs),
            "licenses": len(licenses),
            "cases": len(cases),
            "entitlements": len(entitlements),
        },
        "samples": {
            "environments": [
                {"name": e.get("Name"), "computes": e.get("of_Computes__c")} for e in envs[:5]
            ],
            "licenses": [
                {"name": e.get("Name"), "end": e.get("End_Date__c")} for e in licenses[:5]
            ],
            "cases": [
                {
                    "number": e.get("CaseNumber"),
                    "priority": e.get("Priority"),
                    "subject": e.get("Subject"),
                }
                for e in cases[:5]
            ],
            "opportunitiesOpen": [
                {"name": o.get("Name"), "amount": o.get("Amount"), "stage": o.get("StageName")}
                for o in opps_open[:5]
            ],
            "contacts": [
                {"name": c.get("Name"), "title": c.get("Title"), "email": c.get("Email")}
                for c in contacts[:5]
            ],
        },
        "warnings": warnings,
        "note": (
            "Mirantis QBR depth comes from Environment__c, License__c, Case, and Entitlement — "
            "not standard Asset/AnnualRevenue."
        ),
    }


def scan_accounts(sf: Salesforce, limit: int = 25, query: str = "") -> Dict[str, Any]:
    """Inspect many accounts and rank which have usable QBR-related data."""
    limit = max(1, min(int(limit or 25), 50))
    listed = search_accounts(sf, query=query, limit=limit)
    inspected = []
    for row in listed:
        name = row.get("name") or ""
        if not name:
            continue
        detail = inspect_account(sf, name)
        if not detail.get("found"):
            continue
        inspected.append(
            {
                "name": detail["account"]["name"],
                "id": detail["account"]["id"],
                "usable": detail["usable"],
                "score": detail["score"],
                "signals": detail["signals"],
                "counts": detail["counts"],
                "annualRevenue": detail["account"].get("annualRevenue"),
                "ownerName": detail["account"].get("ownerName"),
                "warnings": detail.get("warnings") or [],
            }
        )

    inspected.sort(key=lambda r: (-r["score"], (r["name"] or "").lower()))
    usable_rows = [r for r in inspected if r["usable"]]
    return {
        "queried": len(listed),
        "inspected": len(inspected),
        "usableCount": len(usable_rows),
        "usable": usable_rows,
        "thin": [r for r in inspected if not r["usable"]],
        "all": inspected,
        "scoring": {
            "usableIfScoreAtLeast": 3,
            "weights": {
                "hasEnvironments": 3,
                "hasLicenses": 3,
                "hasRevenue": 3,
                "hasCases": 2,
                "hasEntitlements": 2,
                "hasOpenOpps": 2,
                "hasContacts": 2,
                "hasRecentClosedOpps": 1,
                "hasOwner": 1,
            },
        },
    }


def list_sobjects(
    sf: Salesforce, query: str = "", custom_only: bool = False
) -> Dict[str, Any]:
    """List queryable Salesforce objects (helps find Environments / Entitlements / etc.)."""
    desc = sf.describe()
    q = (query or "").strip().lower()
    keywords = [
        "asset",
        "entitlement",
        "environment",
        "product",
        "subscription",
        "license",
        "contract",
        "case",
        "health",
        "opportunity",
        "contact",
        "account",
        "order",
        "quote",
    ]
    rows = []
    for obj in desc.get("sobjects", []):
        name = obj.get("name") or ""
        label = obj.get("label") or ""
        if not obj.get("queryable"):
            continue
        if custom_only and not name.endswith("__c"):
            continue
        blob = f"{name} {label}".lower()
        if q and q not in blob:
            continue
        interesting = any(k in blob for k in keywords) or name.endswith("__c")
        if not q and not interesting:
            continue
        rows.append(
            {
                "name": name,
                "label": label,
                "custom": bool(obj.get("custom")),
                "keyPrefix": obj.get("keyPrefix"),
            }
        )
    rows.sort(key=lambda r: (not r["custom"], (r["label"] or "").lower(), r["name"]))
    return {
        "query": q,
        "count": len(rows),
        "objects": rows,
        "hint": (
            "UI tabs like Environments / Entitlements / Customer Health are often custom "
            "objects ending in __c. Pass ?q=environment or ?q=entitlement to filter. "
            "Then use /object-fields?object=Name__c to see fields."
        ),
    }


def describe_object_fields(sf: Salesforce, object_name: str) -> Dict[str, Any]:
    """Field list for one sObject (API name required, e.g. Entitlement__c)."""
    name = (object_name or "").strip()
    if not name:
        raise SystemExit("Pass object=ApiName (e.g. Entitlement__c)")
    meta = getattr(sf, name).describe()
    fields = []
    for f in meta.get("fields", []):
        fields.append(
            {
                "name": f.get("name"),
                "label": f.get("label"),
                "type": f.get("type"),
                "custom": bool(f.get("custom")),
                "updateable": bool(f.get("updateable")),
                "referenceTo": f.get("referenceTo") or [],
            }
        )
    fields.sort(key=lambda r: (not r["custom"], (r["label"] or "").lower()))
    return {
        "object": meta.get("name"),
        "label": meta.get("label"),
        "fieldCount": len(fields),
        "fields": fields,
    }


def count_for_account(
    sf: Salesforce, object_name: str, account_id: str, account_field_hints: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Try to count child rows for an account on a given object."""
    name = (object_name or "").strip()
    account_id = (account_id or "").strip()
    if not name or not account_id:
        raise SystemExit("Need object and accountId")

    meta = getattr(sf, name).describe()
    fields = {f.get("name"): f for f in meta.get("fields", [])}
    hints = account_field_hints or [
        "AccountId",
        "Account__c",
        "RelatedAccount__c",
        "Customer__c",
        "AccountId__c",
    ]
    account_field = None
    for h in hints:
        if h in fields:
            account_field = h
            break
    if not account_field:
        # any reference field pointing at Account
        for f in meta.get("fields", []):
            if "Account" in (f.get("referenceTo") or []) and f.get("name"):
                account_field = f["name"]
                break
    if not account_field:
        return {
            "object": name,
            "accountId": account_id,
            "count": None,
            "error": "No Account lookup field found on this object",
            "sampleFields": [f.get("name") for f in meta.get("fields", [])[:20]],
        }

    soql = (
        f"SELECT COUNT() FROM {name} WHERE {account_field} = '{soql_escape(account_id)}'"
    )
    try:
        result = sf.query(soql)
        return {
            "object": name,
            "accountId": account_id,
            "accountField": account_field,
            "count": result.get("totalSize", 0),
        }
    except SalesforceError as e:
        return {
            "object": name,
            "accountId": account_id,
            "accountField": account_field,
            "count": None,
            "error": str(e),
        }


def build_payload(
    sf: Salesforce, account_name: str, quarter: str
) -> Dict[str, Any]:
    import mirantis

    safe_name = soql_escape(account_name)
    accounts = query_records(sf, SOQL_ACCOUNT.format(name=safe_name))
    if not accounts:
        raise SystemExit(
            f"Account not found in Salesforce: {account_name!r}\n"
            "Check the name (case-sensitive in SOQL) or your service account's access."
        )
    acct = accounts[0]
    account_id = acct["Id"]

    opps_open, warn_opps = query_records_optional(
        sf, SOQL_OPPS_OPEN.format(account_id=account_id), "Open Opportunities"
    )
    opps_recent, warn_recent = query_records_optional(
        sf, SOQL_OPPS_RECENT_CLOSED.format(account_id=account_id), "Recent closed Opportunities"
    )
    contacts, warn_contacts = query_records_optional(
        sf, SOQL_CONTACTS.format(account_id=account_id), "Contacts"
    )
    bundle = mirantis.fetch_mirantis_bundle(sf, account_id)
    warnings = [w for w in (warn_opps, warn_recent, warn_contacts, *bundle.get("warnings", [])) if w]

    envs = bundle.get("environments") or []
    licenses = bundle.get("licenses") or []
    cases = bundle.get("cases") or []
    entitlements = bundle.get("entitlements") or []

    usage = mirantis.map_usage(envs)
    support = mirantis.map_support(cases, entitlements)
    products, product_mix = mirantis.map_products_from_licenses_and_envs(licenses, envs)
    incidents = mirantis.map_incidents_from_p1(cases)
    risks = mirantis.map_risks_from_signals(licenses, cases, envs)
    takeaways = mirantis.map_exec_takeaways(
        acct.get("Name") or account_name, usage, support, products, risks
    )

    # Commercial: AnnualRevenue often empty — fall back to open pipeline sum for projection
    arr_current = safe_get(acct, "AnnualRevenue", default=0) or 0
    try:
        arr_current = float(arr_current)
    except (TypeError, ValueError):
        arr_current = 0
    pipeline = sum((o.get("Amount") or 0) for o in opps_open if (o.get("Amount") or 0) > 0)
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

    # Next license end date as renewal signal
    renewal_date = ""
    for lic in licenses:
        end = lic.get("End_Date__c")
        if end and (not renewal_date or str(end) < renewal_date):
            renewal_date = str(end)[:10]

    # Planning stubs from live signals (TAM refines in Configurator)
    next_actions = []
    if risks:
        for i, r in enumerate(risks[:3], start=1):
            next_actions.append(
                {
                    "id": f"NA-{i:02d}",
                    "commitment": r.get("action") or r.get("title") or "Follow up",
                    "owner": r.get("owner") or "Joint",
                    "dueDate": r.get("dueDate") or "",
                    "successCriteria": r.get("title") or "",
                    "status": "not-started",
                    "kind": "risk",
                }
            )
    if pipeline:
        next_actions.append(
            {
                "id": f"NA-{len(next_actions)+1:02d}",
                "commitment": f"Advance open pipeline (${int(pipeline):,})",
                "owner": "Mirantis Sales + TAM",
                "dueDate": quarter,
                "successCriteria": "Next-stage movement on open Opportunities",
                "status": "on-track",
                "kind": "expansion",
            }
        )

    payload: Dict[str, Any] = {
        "_meta": {
            "schemaVersion": SCHEMA_VERSION,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "source": (
                f"sf-sync/{os.environ.get('SYNC_VERSION', 'v0.4')} "
                "(Environment__c + License__c + Case + Entitlement)"
            ),
            "accountId": account_id,
            "warnings": warnings,
            "counts": {
                "environments": len(envs),
                "licenses": len(licenses),
                "cases": len(cases),
                "entitlements": len(entitlements),
                "contacts": len(contacts),
                "opportunitiesOpen": len(opps_open),
            },
        },
        "customer": {
            "name": acct["Name"],
            "tier": acct.get("Type") or "",
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
                "prior": arr_current,
                "yoyPct": 0,
            },
            "pipelineUSD": pipeline,
            "renewalDate": renewal_date,
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
        "usage": usage,
        "support": support,
        "nps": {"score": 0, "industry": 30, "delta": 0},
        "products": products,
        "productMix": product_mix,
        "incidents": incidents,
        "incidentsPattern": (
            f"{support.get('p1Count', 0)} P1 cases in Salesforce Case history for this account. "
            "Validate RCAs and pattern narrative with Support before presenting."
            if incidents
            else ""
        ),
        "wins": [],
        "risks": risks,
        "mirantisRoadmap": [],
        "customerRoadmap": [],
        "training": {"delivered": [], "planned": [], "deliveredNote": "", "plannedNote": ""},
        "execSummaryTakeaways": takeaways,
        "asks": {"fromUs": [], "fromYou": []},
        "nextActions": next_actions,
        "previousActions": [],
        "sections": {
            k: True
            for k in [
                "execSummary",
                "accountHealth",
                "goalsRecap",
                "usage",
                "supportDeepDive",
                "incidents",
                "wins",
                "risks",
                "mirantisRoadmap",
                "customerRoadmap",
                "renewal",
                "training",
                "asks",
                "nextQuarter",
                "asksTracker",
                "appendix",
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
