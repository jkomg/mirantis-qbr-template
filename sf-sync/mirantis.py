"""
mirantis.py — Mirantis-org Salesforce objects → QBR + SLA report fields.

Uses Environment__c, License__c, Case (Severity_Level__c), CaseHistory,
CaseMilestone, Entitlement, SlaProcess (not standard Asset).
Field selection is describe-driven so sandbox/prod drift is tolerated.

Initial-response SLA is scored for all four severities against the contractual
table in CONTRACT_RESPONSE_MINS, which depends on the subscription tier derived
in derive_subscription_tier().
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from simple_salesforce import Salesforce, SalesforceError

# Preferred fields — included when present on the object describe.
ENV_PREFERRED = [
    "Id",
    "Name",
    "Account__c",
    "of_nodes__c",
    "of_Computes__c",
    "Number_of_Controllers__c",
    "of_Monitoring_Nodes__c",
    "of_Other_nodes__c",
    "of_Storage_nodes__c",
    "of_Telemetry_MongoDB_nodes__c",
    "Product__c",
    "Product_Name__c",
    "Status__c",
    "Environment_Status__c",
    "Version__c",
    "MCP_Version__c",
    "MKE_Version__c",
    "MOSK_Version__c",
    "Type__c",
    "Environment_Type__c",
    "Region__c",
    "Datacenter__c",
    "Cloud_Provider__c",
]

LICENSE_PREFERRED = [
    "Id",
    "Name",
    "Account__c",
    "End_Date__c",
    "Start_Date__c",
    "Duration_days__c",
    "Product__c",
    "Product_Name__c",
    "License_Type__c",
    "Type__c",
    "Status__c",
    "Nodes__c",
    "Number_of_Nodes__c",
    "Seats__c",
    "Cores__c",
    "Download_URL__c",
]

CASE_PREFERRED = [
    "Id",
    "CaseNumber",
    "Subject",
    "Status",
    # Mirantis uses Severity_Level__c (Sev 1–4); Priority is absent in MKE Ops sandbox
    "Severity_Level__c",
    "Priority",
    "Type",
    "Reason",
    "Origin",
    "Description",
    "CreatedDate",
    "ClosedDate",
    "IsClosed",
    "AccountId",
    "EntitlementId",
    "IsStopped",
    "StopStartDate",
    "BusinessHoursId",
    "ContactId",
    "OwnerId",
    "Resolution__c",
    "Resolution_time_minutes__c",
    "Resolution_Time_DDHHMM__c",
    "Age_Hours__c",
    "First_Reply_Time_in_Minutes__c",
    "Milestone_violated__c",
    "Resolution_Time_is_violated__c",
    "Resolution_Time_is_NOT_violated__c",
    "Next_Update_is_violated__c",
    "Sev1_Duration__c",
    "Cause_Code__c",
    "Closure_Class__c",
    "Product__c",
    "Technology_Product__c",
    "Ticket_Type__c",
    "Symptoms__c",
    "Cause__c",
    "Contact_Name__c",
]

CASE_MONTHS = 24
CASE_LIMIT = 500

ENTITLEMENT_PREFERRED = [
    "Id",
    "Name",
    "AccountId",
    "Status",
    "Type",
    "StartDate",
    "EndDate",
    "SlaProcessId",
    "AssetId",
    "ContractLineItemId",
    # Subscription-tier candidates. Describe-driven, so any of these that the org
    # does not have are dropped rather than breaking the query.
    "Service_Level__c",
    "Support_Level__c",
    "Support_Offering__c",
    "Subscription_Tier__c",
    "Tier__c",
    "Product__c",
]

# The Entitlement Process is what CaseMilestone rows are cut from, so its name is
# the most direct tier signal the org exposes.
SLA_PROCESS_PREFERRED = [
    "Id",
    "Name",
    "Description",
    "IsActive",
    "VersionNumber",
    "IsVersionDefault",
]

TIER_OPSCARE_PLUS = "OpsCare Plus"
TIER_OPSCARE = "OpsCare"
LEVEL_LABCARE = "LabCare"
# Still undocumented — label it, never invent targets.
LEVEL_CUSTOM = "Custom"

# Support hours per level. NOT uniform: LabCare is 8x5, so its minute targets are
# business minutes and are not wall-clock comparable to the 24x7 levels. The live
# CaseMilestone path is unaffected (Salesforce applies BusinessHoursId itself);
# this only matters for the contract fallback below.
SUPPORT_HOURS: Dict[str, str] = {
    TIER_OPSCARE_PLUS: "24x7",
    TIER_OPSCARE: "24x7",
    LEVEL_LABCARE: "8x5",
}

# Severity levels a support level is not entitled to open. A case at one of these
# is a data or process anomaly worth surfacing, not merely "unscored".
DISALLOWED_SEVERITIES: Dict[str, Tuple[str, ...]] = {
    LEVEL_LABCARE: ("Sev 1",),
}

# Contractual initial-response commitments, in minutes.
#
# OpsCare / OpsCare Plus: from the Confluence contract table (CONTRACT_REFERENCE).
# NOTE: SC-5980 (Daniil Gelfanov, Salesforce admin) quotes a single combined
# "OpsCare / OpsCare Plus" column carrying the *Plus* values (15/60/120/480),
# i.e. Salesforce may enforce Plus targets for both levels. The contract table
# keeps them distinct. We score against the contract and let the report's
# enforced-vs-contract panel surface any per-case disagreement, rather than
# silently tightening OpsCare and manufacturing breaches. Open question on the
# ticket — see MEMORY.md.
#
# LabCare: from SC-5980 (Daniil, 2026-07-30). Business minutes, 8x5.
CONTRACT_RESPONSE_MINS: Dict[str, Dict[str, int]] = {
    TIER_OPSCARE_PLUS: {"Sev 1": 15, "Sev 2": 60, "Sev 3": 120, "Sev 4": 480},
    TIER_OPSCARE: {"Sev 1": 30, "Sev 2": 120, "Sev 3": 240, "Sev 4": 480},
    LEVEL_LABCARE: {"Sev 2": 240, "Sev 3": 480, "Sev 4": 480},
}

# Next-update commitments, in minutes. Not yet scored — recorded so the report
# can show the committed cadence next to first response.
# OpsCare/Plus from the contract table; LabCare from SC-5980.
CONTRACT_NEXT_UPDATE_MINS: Dict[str, Dict[str, int]] = {
    TIER_OPSCARE_PLUS: {"Sev 1": 60, "Sev 2": 240, "Sev 3": 2880, "Sev 4": 4320},
    TIER_OPSCARE: {"Sev 1": 60, "Sev 2": 240, "Sev 3": 2880, "Sev 4": 5760},
    LEVEL_LABCARE: {"Sev 2": 480, "Sev 3": 1440, "Sev 4": 1920},
}
CONTRACT_REFERENCE = (
    "Confluence 'Service Level Management' (space 2S, page 884343127); "
    "LabCare windows from SC-5980"
)

SEV_LABELS = ("Sev 1", "Sev 2", "Sev 3", "Sev 4")
# Kept separable so the report can headline what customers actually challenge.
HEADLINE_SEVERITIES = ("Sev 1", "Sev 2")

TARGET_SOURCE_LIVE = "CaseMilestone.TargetResponseInMins"
TARGET_SOURCE_CONTRACT = "contract"

# Entitlement field names that plausibly name the purchased service level.
# Entitlement.Type is "Phone Support" in this org and is never used for tier.
_TIER_FIELD_HINTS = (
    "service_level",
    "servicelevel",
    "support_level",
    "supportlevel",
    "offering",
    "opscare",
    "labcare",
    "care_level",
    "subscription",
    "tier",
)

# Both carry OpsCare / LabCare / Custom, but they are not peers.
# Per SC-5980 (Daniil Gelfanov, Salesforce admin): Support_Level__c is the custom
# field where the support level is *chosen*; SlaProcess is a standard field set
# *automatically from* it. So Support_Level__c is the source of truth and
# SlaProcess.Name is a derived echo of it — prefer the former.
_TIER_SOURCE_CONFIDENCE = {
    "Entitlement.Support_Level__c": "high",
    "SlaProcess.Name": "high",
}


def _source_rank(source: str) -> int:
    """Lower wins. Entitlement.Type is excluded from the decision path entirely."""
    if source == "Entitlement.Support_Level__c":
        return 0
    if source == "SlaProcess.Name":
        return 1
    if source == "Entitlement.Type":
        return 99
    if source == "Entitlement.Name":
        return 3
    if source.startswith("Entitlement."):
        return 2
    return 9


def _soql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _num(val: Any) -> float:
    try:
        if val is None or val == "":
            return 0.0
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(val: Any) -> Optional[int]:
    """Minutes fields arrive as int, float or string depending on the API path."""
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def _contract_target_mins(sev_label: str, tier: Optional[str]) -> Optional[int]:
    """Documented initial-response target, or None when the tier is unknown.

    Also None for a severity the level cannot open (LabCare Sev 1) — see
    _severity_not_entitled, which distinguishes that from a plain lookup miss.
    """
    if not tier:
        return None
    return CONTRACT_RESPONSE_MINS.get(tier, {}).get(sev_label)


def _severity_not_entitled(sev_label: str, level: Optional[str]) -> bool:
    """True when this support level is not entitled to open this severity.

    LabCare cannot open Sev 1. A case that exists anyway is an anomaly — either
    mis-set severity or a process gap — and is worth surfacing rather than
    silently reporting as unscored.
    """
    if not level or not sev_label:
        return False
    return sev_label in DISALLOWED_SEVERITIES.get(level, ())


def _pick_account_field(meta: Dict[str, Any]) -> Optional[str]:
    fields = {f.get("name"): f for f in meta.get("fields", [])}
    for h in ("AccountId", "Account__c", "Account_Name__c", "RelatedAccount__c", "Customer__c"):
        if h in fields:
            return h
    for f in meta.get("fields", []):
        if "Account" in (f.get("referenceTo") or []) and f.get("name"):
            return f["name"]
    return None


def _select_fields(meta: Dict[str, Any], preferred: List[str], extra_keywords: Optional[List[str]] = None) -> List[str]:
    available = {f.get("name") for f in meta.get("fields", []) if f.get("name")}
    selected: List[str] = []
    for name in preferred:
        if name in available and name not in selected:
            selected.append(name)
    keywords = [k.lower() for k in (extra_keywords or [])]
    if keywords:
        for f in meta.get("fields", []):
            name = f.get("name") or ""
            label = (f.get("label") or "").lower()
            blob = f"{name} {label}".lower()
            if name in selected or name in ("IsDeleted", "SystemModstamp"):
                continue
            if any(k in blob for k in keywords) and f.get("type") not in ("base64", "address", "location"):
                selected.append(name)
            if len(selected) >= 40:
                break
    if "Id" in available and "Id" not in selected:
        selected.insert(0, "Id")
    if "Name" in available and "Name" not in selected:
        selected.insert(1, "Name")
    return selected


def _query(sf: Salesforce, soql: str) -> List[Dict[str, Any]]:
    return sf.query(soql).get("records", [])


def query_related(
    sf: Salesforce,
    object_name: str,
    account_id: str,
    preferred: List[str],
    limit: int = 50,
    extra_keywords: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Return (records, account_field, warning)."""
    try:
        meta = getattr(sf, object_name).describe()
    except Exception as e:
        return [], None, f"{object_name} describe failed: {e}"

    account_field = _pick_account_field(meta)
    if not account_field:
        return [], None, f"{object_name}: no Account lookup field"

    fields = _select_fields(meta, preferred, extra_keywords=extra_keywords)
    if not fields:
        return [], account_field, f"{object_name}: no selectable fields"

    soql = (
        f"SELECT {', '.join(fields)} FROM {object_name} "
        f"WHERE {account_field} = '{_soql_escape(account_id)}' "
        f"ORDER BY LastModifiedDate DESC NULLS LAST "
        f"LIMIT {limit}"
    )
    try:
        return _query(sf, soql), account_field, None
    except SalesforceError as e:
        # Retry without ORDER BY if LastModifiedDate missing
        soql2 = (
            f"SELECT {', '.join(fields)} FROM {object_name} "
            f"WHERE {account_field} = '{_soql_escape(account_id)}' "
            f"LIMIT {limit}"
        )
        try:
            return _query(sf, soql2), account_field, None
        except SalesforceError as e2:
            return [], account_field, f"{object_name} query failed: {e2}"


def _first_str(rec: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            v = v.get("Name") or v.get("name")
        s = str(v).strip()
        if s and s != "None":
            return s
    return ""


def _sum_nodes(env: Dict[str, Any]) -> float:
    # RevOps: of_nodes__c is the SoR total; component fields are a fallback.
    total = _num(env.get("of_nodes__c"))
    if total > 0:
        return total
    return sum(
        _num(env.get(k))
        for k in (
            "of_Computes__c",
            "Number_of_Controllers__c",
            "of_Monitoring_Nodes__c",
            "of_Other_nodes__c",
            "of_Storage_nodes__c",
            "of_Telemetry_MongoDB_nodes__c",
        )
    )


def map_usage(environments: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_nodes = int(sum(_sum_nodes(e) for e in environments))
    computes = int(sum(_num(e.get("of_Computes__c")) for e in environments))
    controllers = int(sum(_num(e.get("Number_of_Controllers__c")) for e in environments))
    return {
        "clusters": len(environments),
        "clustersDelta": 0,
        "nodes": total_nodes,
        "nodesDelta": 0,
        "workloads": 0,
        "workloadsDelta": 0,
        "environments": len(environments),
        "uptime": 0,
        "_environmentDetail": [
            {
                "name": _first_str(e, "Name"),
                "product": _first_str(e, "Product_Name__c", "Product__c", "Type__c", "Environment_Type__c"),
                "status": _first_str(e, "Status__c", "Environment_Status__c"),
                "version": _first_str(e, "MKE_Version__c", "MOSK_Version__c", "MCP_Version__c", "Version__c"),
                "region": _first_str(e, "Region__c", "Datacenter__c", "Cloud_Provider__c"),
                "computeNodes": int(_num(e.get("of_Computes__c"))),
                "controllers": int(_num(e.get("Number_of_Controllers__c"))),
                "totalNodes": int(_sum_nodes(e)),
            }
            for e in environments
        ],
        "_nodeBreakdown": {
            "compute": computes,
            "controllers": controllers,
            "monitoring": int(sum(_num(e.get("of_Monitoring_Nodes__c")) for e in environments)),
            "storage": int(sum(_num(e.get("of_Storage_nodes__c")) for e in environments)),
            "other": int(sum(_num(e.get("of_Other_nodes__c")) for e in environments)),
            "telemetry": int(sum(_num(e.get("of_Telemetry_MongoDB_nodes__c")) for e in environments)),
            "of_nodes": int(sum(_num(e.get("of_nodes__c")) for e in environments)),
        },
    }


def map_products_from_licenses_and_envs(
    licenses: List[Dict[str, Any]], environments: List[Dict[str, Any]]
) -> Tuple[List[str], List[Dict[str, Any]]]:
    product_mix: List[Dict[str, Any]] = []
    products: List[str] = []

    for lic in licenses:
        product = _first_str(lic, "Product_Name__c", "Product__c", "License_Type__c", "Type__c", "Name")
        if not product:
            continue
        entitlement_bits = []
        for label, key in (
            ("nodes", "Nodes__c"),
            ("nodes", "Number_of_Nodes__c"),
            ("seats", "Seats__c"),
            ("cores", "Cores__c"),
        ):
            n = _num(lic.get(key))
            if n:
                entitlement_bits.append(f"{int(n)} {label}")
        end = _first_str(lic, "End_Date__c")
        status = _first_str(lic, "Status__c")
        if end:
            entitlement_bits.append(f"ends {end}")
        if status:
            entitlement_bits.append(status)
        product_mix.append(
            {
                "product": product,
                "entitlement": " · ".join(entitlement_bits) or "See License__c",
                "inUse": "",
                "utilizationPct": 0,
                "trend": "— from SF License",
            }
        )
        if product not in products:
            products.append(product)

    # Environments often name the product stack even when licenses are sparse
    for env in environments:
        product = _first_str(env, "Product_Name__c", "Product__c", "Type__c", "Environment_Type__c")
        if product and product not in products:
            products.append(product)
            nodes = int(_sum_nodes(env))
            product_mix.append(
                {
                    "product": product,
                    "entitlement": f"{nodes} nodes across envs" if nodes else "From Environment__c",
                    "inUse": str(nodes) if nodes else "",
                    "utilizationPct": 0,
                    "trend": "— from SF Environment",
                }
            )

    return products, product_mix


def _case_severity_raw(case: Dict[str, Any]) -> str:
    return _first_str(case, "Severity_Level__c", "Priority", "Priority__c")


def _normalize_sev_label(raw: str) -> str:
    """Return 'Sev N' for perf-report sevKey(), or '' if unknown."""
    if not raw:
        return ""
    m = re.search(r"([1-4])", str(raw))
    return f"Sev {m.group(1)}" if m else ""


def _priority_bucket(priority: str) -> str:
    label = _normalize_sev_label(priority)
    if label:
        return f"p{label[-1]}"
    p = (priority or "").strip().lower()
    if p == "critical" or "p1" in p:
        return "p1"
    if p == "high" or "p2" in p:
        return "p2"
    if p == "medium" or "p3" in p:
        return "p3"
    if p == "low" or "p4" in p:
        return "p4"
    return "other"


def _parse_sf_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _format_duration(hours: Optional[float], is_closed: bool) -> str:
    """Formats for perf-report parseDurationHours (Nd Nh | Nh | Nm)."""
    if not is_closed:
        return "Open"
    if hours is None:
        return ""
    if hours < 0:
        hours = 0.0
    mins = int(round(hours * 60))
    if mins < 60:
        return f"{max(mins, 1)}m"
    whole_hours = int(round(hours))
    days = whole_hours // 24
    rem = whole_hours % 24
    if days > 0:
        return f"{days}d {rem}h"
    return f"{whole_hours}h"


def _case_resolution_hours(case: Dict[str, Any]) -> Optional[float]:
    mins = _num(case.get("Resolution_time_minutes__c"))
    if mins > 0:
        return mins / 60.0
    age = _num(case.get("Age_Hours__c"))
    if age > 0 and case.get("IsClosed"):
        return age
    c0 = _parse_sf_dt(case.get("CreatedDate"))
    c1 = _parse_sf_dt(case.get("ClosedDate"))
    if c0 and c1:
        return max((c1 - c0).total_seconds() / 3600.0, 0.0)
    return None


def _chunked(items: List[str], size: int = 100) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _opened_and_final_severity(
    case: Dict[str, Any], history: List[Dict[str, Any]]
) -> Tuple[str, str]:
    current = _normalize_sev_label(_case_severity_raw(case))
    rows = [h for h in history if h.get("CaseId") == case.get("Id")]
    if not rows:
        return current, current
    rows = sorted(rows, key=lambda h: str(h.get("CreatedDate") or ""))
    opened = _normalize_sev_label(str(rows[0].get("OldValue") or "")) or current
    final = current
    if not final:
        for h in reversed(rows):
            nv = _normalize_sev_label(str(h.get("NewValue") or ""))
            if nv:
                final = nv
                break
        if not final:
            for h in reversed(rows):
                ov = _normalize_sev_label(str(h.get("OldValue") or ""))
                if ov:
                    final = ov
                    break
    if not opened:
        opened = final
    return opened, final


def query_sla_processes(
    sf: Salesforce, sla_process_ids: List[str]
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Entitlement Processes referenced by this account's Entitlements."""
    ids = [i for i in dict.fromkeys(sla_process_ids) if i]
    if not ids:
        return [], None
    try:
        meta = getattr(sf, "SlaProcess").describe()
    except Exception as e:
        return [], f"SlaProcess describe failed: {e}"

    fields = _select_fields(meta, SLA_PROCESS_PREFERRED)
    if not fields:
        return [], "SlaProcess: no selectable fields"

    rows: List[Dict[str, Any]] = []
    for chunk in _chunked(ids, 80):
        id_list = ",".join(f"'{_soql_escape(i)}'" for i in chunk)
        soql = (
            f"SELECT {', '.join(fields)} FROM SlaProcess "
            f"WHERE Id IN ({id_list}) LIMIT 200"
        )
        try:
            rows.extend(_query(sf, soql))
        except SalesforceError as e:
            return rows, f"SlaProcess query failed: {e}"
    return rows, None


def _support_level_from_text(text: str) -> Optional[str]:
    """Map a free-text value onto a known support level label, or None.

    OpsCare Plus is only returned on an explicit plus marker — guessing upward
    would apply the tighter target and over-report breaches.
    LabCare now has published targets (SC-5980); Custom still has none and is
    recognized as a label only.
    """
    squashed = re.sub(r"[^a-z0-9+]", "", (text or "").lower())
    if not squashed:
        return None
    if "opscare" in squashed:
        if "plus" in squashed or "+" in squashed:
            return TIER_OPSCARE_PLUS
        return TIER_OPSCARE
    if "labcare" in squashed:
        return LEVEL_LABCARE
    if squashed == "custom":
        return LEVEL_CUSTOM
    return None


def _contract_tier(level: Optional[str]) -> Optional[str]:
    """Levels with documented response windows: OpsCare, OpsCare Plus, LabCare."""
    if level in CONTRACT_RESPONSE_MINS:
        return level
    return None


def _add_tier_candidate(out: List[Dict[str, Any]], source: str, value: str) -> None:
    text = (value or "").strip()
    if not text:
        return
    if any(c["source"] == source and c["value"] == text for c in out):
        return
    level = _support_level_from_text(text)
    out.append({
        "source": source,
        "value": text,
        "level": level,
        "tier": _contract_tier(level),
    })


def derive_subscription_tier(
    entitlements: List[Dict[str, Any]],
    sla_processes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Derive the support level (OpsCare / OpsCare Plus / LabCare / Custom).

    Decision order — per SC-5980, Support_Level__c is where the level is chosen
    and SlaProcess is set automatically from it, so the custom field leads:
      1. Entitlement.Support_Level__c
      2. SlaProcess.Name  (derived echo of the above)
      3. other Entitlement.* level-ish fields
      4. Entitlement.Name
    Entitlement.Type is recorded in candidates but never decides — in this org
    it is always "Phone Support".

    `tier` is set when the level has documented targets — now OpsCare, OpsCare
    Plus and LabCare. Custom sets `supportLevel` with `tier: null` so the report
    can show the label without inventing response windows.
    """
    entitlements = entitlements or []
    processes = {p.get("Id"): p for p in (sla_processes or []) if p.get("Id")}
    warnings: List[str] = []

    # A lapsed OpsCare row alongside a live OpsCare Plus row is a normal upgrade
    # history, not a conflict — score the active ones when there are any.
    active = [e for e in entitlements if _first_str(e, "Status").lower() == "active"]
    pool = active or entitlements
    if active:
        scope = "active entitlements"
    elif entitlements:
        scope = "all entitlements (none marked Active)"
    else:
        scope = "none"

    candidates: List[Dict[str, Any]] = []
    for e in pool:
        proc = processes.get(_first_str(e, "SlaProcessId"))
        if proc:
            _add_tier_candidate(candidates, "SlaProcess.Name", _first_str(proc, "Name"))
    # Type is informational only — see docstring.
    for e in pool:
        _add_tier_candidate(candidates, "Entitlement.Type", _first_str(e, "Type"))
    for e in pool:
        for key, val in e.items():
            if key == "attributes" or not isinstance(val, str):
                continue
            if any(hint in key.lower() for hint in _TIER_FIELD_HINTS):
                _add_tier_candidate(candidates, f"Entitlement.{key}", val)
    for e in pool:
        _add_tier_candidate(candidates, "Entitlement.Name", _first_str(e, "Name"))

    # Decision path ignores Entitlement.Type and anything with no recognized level.
    decision = [
        c for c in candidates
        if c.get("level") and c["source"] != "Entitlement.Type"
    ]
    tier: Optional[str] = None
    support_level: Optional[str] = None
    source: Optional[str] = None
    confidence = "unknown"
    conflict = False

    if not entitlements:
        warnings.append(
            "Subscription tier unknown: no Entitlement records for this account. "
            "Response targets are reported exactly as Salesforce enforced them "
            "and are not validated against the contract table."
        )
    elif not decision:
        seen = "; ".join(f"{c['source']}={c['value']!r}" for c in candidates[:6])
        warnings.append(
            "Subscription tier unknown: nothing on the Entitlement or its SLA "
            f"process matched a known support level "
            f"({TIER_OPSCARE}, {TIER_OPSCARE_PLUS}, {LEVEL_LABCARE}, {LEVEL_CUSTOM}) "
            f"({seen or 'no readable values'}). Targets are unvalidated."
        )
    else:
        best_rank = min(_source_rank(c["source"]) for c in decision)
        best = [c for c in decision if _source_rank(c["source"]) == best_rank]
        distinct_levels = sorted({c["level"] for c in best if c["level"]})
        distinct_tiers = sorted({c["tier"] for c in best if c["tier"]})
        conflict = len(distinct_levels) > 1

        if conflict:
            detail = "; ".join(f"{c['source']}={c['value']!r}" for c in best[:4])
            warnings.append(
                f"Subscription tier unknown: conflicting signals at preferred "
                f"source rank across {scope} ({' and '.join(distinct_levels)}) "
                f"from {detail}. Not guessing — assuming the higher tier would "
                "hide breaches."
            )
        else:
            support_level = distinct_levels[0]
            source = best[0]["source"]
            confidence = _TIER_SOURCE_CONFIDENCE.get(source, "low")
            tier = distinct_tiers[0] if len(distinct_tiers) == 1 else None

            if tier is None and support_level in (LEVEL_LABCARE, LEVEL_CUSTOM):
                warnings.append(
                    f"Support level {support_level!r} from {source} "
                    f"({best[0]['value']!r}) has no published initial-response "
                    f"windows on {CONTRACT_REFERENCE}. Adherence is measured "
                    "against whatever Salesforce enforced and is not validated "
                    "against the OpsCare / OpsCare Plus contract table."
                )
            elif confidence == "low":
                warnings.append(
                    f"Subscription tier {support_level!r} inferred from {source} "
                    f"({best[0]['value']!r}) — low confidence. Verify before "
                    "presenting adherence."
                )

    return {
        "tier": tier,
        "supportLevel": support_level,
        "confidence": confidence,
        "source": source,
        "conflict": conflict,
        "scope": scope,
        "entitlementsConsidered": len(pool),
        "candidates": candidates[:12],
        "documentedTargetMins": dict(CONTRACT_RESPONSE_MINS[tier]) if tier else None,
        "reference": CONTRACT_REFERENCE,
        "warnings": warnings,
    }


def _milestone_type_name(m: Dict[str, Any]) -> str:
    mt = m.get("MilestoneType")
    if isinstance(mt, dict):
        return (mt.get("Name") or "").strip()
    return _first_str(m, "MilestoneType")


def _is_first_response_milestone(name: str) -> bool:
    return "first response" in (name or "").strip().lower()


def _first_response_milestones(
    case: Dict[str, Any], milestones: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    return [
        m
        for m in milestones
        if m.get("CaseId") == case.get("Id")
        and _is_first_response_milestone(_milestone_type_name(m))
    ]


def _case_sla_adherence(
    case: Dict[str, Any],
    milestones: List[Dict[str, Any]],
    sla_sev_label: str,
    tier: Optional[str] = None,
) -> Dict[str, Any]:
    """
    All four severities carry an initial-response commitment, so every case with
    a determinable severity at open is SLA-bound. Severity changes after open are
    normal workflow — the commitment that applied at open is the one scored.

    A case with no usable First Response milestone stays slaBreach None and is
    excluded from the denominator: absent evidence is not adherence.

    The live milestone target wins, because it is what Salesforce enforced. The
    documented target rides alongside it so a disagreement can be surfaced
    instead of one source being silently preferred.
    """
    label = _normalize_sev_label(sla_sev_label)
    bucket = _priority_bucket(sla_sev_label)
    contract_mins = _contract_target_mins(label, tier)
    out: Dict[str, Any] = {
        "slaSeverity": label or None,
        "slaBound": bool(label),
        "slaHeadline": bucket in ("p1", "p2"),
        "slaBreach": None,
        "slaTargetMins": None,
        "slaTargetSource": None,
        "slaContractTargetMins": contract_mins,
        "slaTargetMismatch": None,
        "slaActualMins": None,
        "slaMilestone": None,
        # LabCare cannot open Sev 1 — flag rather than silently leaving unscored.
        "slaSeverityNotEntitled": _severity_not_entitled(label, tier),
        # LabCare is 8x5, so its target minutes are business minutes and are not
        # wall-clock comparable to the 24x7 levels. Verdicts still come from
        # Salesforce (which applies BusinessHoursId), so this is presentational.
        "slaSupportHours": SUPPORT_HOURS.get(tier) if tier else None,
    }
    if not label:
        # Severity at open is undeterminable — nothing to hold anyone to.
        return out

    fr = _first_response_milestones(case, milestones)
    if not fr:
        # No First Response milestone — leave unknown (don't use WatchDog/Next
        # Update, and don't present the documented target as if it were enforced)
        return out

    # Prefer a completed or violated row; else the latest by StartDate
    fr_sorted = sorted(
        fr,
        key=lambda m: (
            0 if m.get("IsViolated") else 1,
            0 if m.get("IsCompleted") else 1,
            str(m.get("StartDate") or ""),
        ),
    )
    m = fr_sorted[0]
    out["slaMilestone"] = "First Response"
    live_mins = _int_or_none(m.get("TargetResponseInMins"))
    if live_mins is not None:
        out["slaTargetMins"] = live_mins
        out["slaTargetSource"] = TARGET_SOURCE_LIVE
        if contract_mins is not None:
            out["slaTargetMismatch"] = live_mins != contract_mins
    elif contract_mins is not None:
        # Milestone exists but carries no target — fall back to the documented
        # value, labelled as such. Verdict below still comes from Salesforce.
        out["slaTargetMins"] = contract_mins
        out["slaTargetSource"] = TARGET_SOURCE_CONTRACT
    out["slaActualMins"] = _int_or_none(m.get("ActualElapsedTimeInMins"))
    if m.get("IsViolated") is True:
        out["slaBreach"] = True
    elif m.get("IsCompleted") is True:
        out["slaBreach"] = False
    else:
        # Still in window — treat as met-so-far (not breached)
        out["slaBreach"] = False
    return out


def _case_sla_breach(
    case: Dict[str, Any],
    milestones: List[Dict[str, Any]],
    sla_sev_label: str = "",
    tier: Optional[str] = None,
) -> Optional[bool]:
    """Back-compat wrapper — prefer _case_sla_adherence."""
    return _case_sla_adherence(case, milestones, sla_sev_label, tier).get("slaBreach")


def query_cases(
    sf: Salesforce,
    account_id: str,
    months: int = CASE_MONTHS,
    limit: int = CASE_LIMIT,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """24-month Case pull with Mirantis severity/SLA fields."""
    try:
        meta = sf.Case.describe()
    except Exception as e:
        return [], f"Case describe failed: {e}"

    fields = _select_fields(
        meta,
        CASE_PREFERRED,
        extra_keywords=[
            "severity",
            "resolut",
            "milestone",
            "cause",
            "closure",
            "product",
            "age",
            "reply",
            "symptom",
            "ticket",
        ],
    )
    # Keep SOQL under control
    if len(fields) > 40:
        preferred_set = set(CASE_PREFERRED)
        fields = [f for f in fields if f in preferred_set] + [
            f for f in fields if f not in preferred_set
        ]
        fields = fields[:40]

    select = list(fields)
    available = {f.get("name") for f in meta.get("fields", [])}
    if "ContactId" in available and "Contact.Name" not in select:
        select.append("Contact.Name")

    since = (datetime.now(timezone.utc) - timedelta(days=int(months * 30.44))).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    soql = (
        f"SELECT {', '.join(select)} FROM Case "
        f"WHERE AccountId = '{_soql_escape(account_id)}' "
        f"AND CreatedDate >= {since} "
        f"ORDER BY CreatedDate DESC "
        f"LIMIT {limit}"
    )
    try:
        return _query(sf, soql), None
    except SalesforceError as e:
        # Retry without relationship field / CreatedDate filter if needed
        select2 = [f for f in select if not f.startswith("Contact.")]
        soql2 = (
            f"SELECT {', '.join(select2)} FROM Case "
            f"WHERE AccountId = '{_soql_escape(account_id)}' "
            f"ORDER BY CreatedDate DESC NULLS LAST "
            f"LIMIT {min(limit, 200)}"
        )
        try:
            return _query(sf, soql2), f"Case query fell back (strict 24mo filter failed): {e}"
        except SalesforceError as e2:
            return [], f"Case query failed: {e2}"


def query_case_severity_history(
    sf: Salesforce, case_ids: List[str]
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not case_ids:
        return [], None
    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for chunk in _chunked(case_ids, 80):
        id_list = ",".join(f"'{i}'" for i in chunk)
        for field in ("Severity_Level__c", "Priority"):
            soql = (
                f"SELECT CaseId, Field, OldValue, NewValue, CreatedDate "
                f"FROM CaseHistory "
                f"WHERE CaseId IN ({id_list}) AND Field = '{field}' "
                f"ORDER BY CreatedDate ASC "
                f"LIMIT 2000"
            )
            try:
                rows.extend(_query(sf, soql))
            except SalesforceError as e:
                if field == "Severity_Level__c":
                    warnings.append(f"CaseHistory({field}): {e}")
    return rows, ("; ".join(warnings) if warnings else None)


def query_case_milestones(
    sf: Salesforce, case_ids: List[str]
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not case_ids:
        return [], None
    rows: List[Dict[str, Any]] = []
    for chunk in _chunked(case_ids, 80):
        id_list = ",".join(f"'{i}'" for i in chunk)
        soql = (
            f"SELECT Id, CaseId, MilestoneType.Name, IsViolated, IsCompleted, "
            f"TargetDate, CompletionDate, TargetResponseInMins, ActualElapsedTimeInMins, StartDate "
            f"FROM CaseMilestone "
            f"WHERE CaseId IN ({id_list}) "
            f"ORDER BY TargetDate DESC NULLS LAST "
            f"LIMIT 2000"
        )
        try:
            rows.extend(_query(sf, soql))
        except SalesforceError as e:
            return rows, f"CaseMilestone query failed: {e}"
    return rows, None


def _sla_severity_label(case: Dict[str, Any], history: List[Dict[str, Any]]) -> str:
    """Severity the commitment was made under — at open, per CaseHistory."""
    opened, final = _opened_and_final_severity(case, history)
    return _normalize_sev_label(opened or final or _case_severity_raw(case))


def map_sla_scoring(
    cases: List[Dict[str, Any]],
    milestones: Optional[List[Dict[str, Any]]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    tier_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Initial-response adherence for all four severities, plus target validation.

    Sev 3/4 are scored, not dropped: a 2-to-8-hour commitment is still a
    commitment, and those severities are most of the queue. Sev 1/2 stay rolled
    up separately so the report can keep them as the headline.
    """
    cases = cases or []
    milestones = milestones or []
    history = history or []
    if tier_info is None:
        tier_info = derive_subscription_tier([], [])
    tier = tier_info.get("tier")

    per_sev: Dict[str, Dict[str, Any]] = {
        label: {
            "total": 0,
            "bound": 0,
            "scored": 0,
            "met": 0,
            "breached": 0,
            "noMilestone": 0,
            "pct": None,
            "contractTargetMins": _contract_target_mins(label, tier),
            "liveTargetMinsObserved": [],
        }
        for label in SEV_LABELS
    }
    live_seen: Dict[str, set] = {label: set() for label in SEV_LABELS}
    mismatches: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    unknown_sev = 0

    for case in cases:
        label = _sla_severity_label(case, history)
        if label not in per_sev:
            unknown_sev += 1
            continue
        row = per_sev[label]
        row["total"] += 1
        adh = _case_sla_adherence(case, milestones, label, tier=tier)
        if adh["slaBound"]:
            row["bound"] += 1
        if adh["slaMilestone"] is None:
            row["noMilestone"] += 1

        live_mins = (
            adh["slaTargetMins"]
            if adh["slaTargetSource"] == TARGET_SOURCE_LIVE
            else None
        )
        doc_mins = adh["slaContractTargetMins"]
        if live_mins is not None:
            live_seen[label].add(live_mins)
            if adh["slaTargetMismatch"] and doc_mins is not None:
                entry = mismatches.setdefault(
                    (label, live_mins, doc_mins),
                    {
                        "severity": label,
                        "tier": tier,
                        "liveTargetMins": live_mins,
                        "documentedTargetMins": doc_mins,
                        "cases": 0,
                        "sampleCaseNumbers": [],
                    },
                )
                entry["cases"] += 1
                number = _first_str(case, "CaseNumber")
                if number and len(entry["sampleCaseNumbers"]) < 3:
                    entry["sampleCaseNumbers"].append(number)

        if adh["slaBreach"] is None:
            continue
        row["scored"] += 1
        if adh["slaBreach"]:
            row["breached"] += 1
        else:
            row["met"] += 1

    for label in SEV_LABELS:
        row = per_sev[label]
        row["liveTargetMinsObserved"] = sorted(live_seen[label])
        row["pct"] = (
            round(100.0 * row["met"] / row["scored"]) if row["scored"] else None
        )

    def _rollup(labels: Iterable[str]) -> Dict[str, Any]:
        labels = list(labels)
        agg: Dict[str, Any] = {
            key: sum(per_sev[l][key] for l in labels)
            for key in ("total", "bound", "scored", "met", "breached", "noMilestone")
        }
        agg["pct"] = round(100.0 * agg["met"] / agg["scored"]) if agg["scored"] else None
        return agg

    overall = _rollup(SEV_LABELS)
    overall["total"] += unknown_sev
    headline = _rollup(HEADLINE_SEVERITIES)

    mismatch_rows = sorted(
        mismatches.values(), key=lambda m: (m["severity"], m["liveTargetMins"])
    )

    warnings: List[str] = list(tier_info.get("warnings") or [])
    for entry in mismatch_rows:
        sample = (
            f" (e.g. {', '.join(entry['sampleCaseNumbers'])})"
            if entry["sampleCaseNumbers"]
            else ""
        )
        warnings.append(
            f"SLA target mismatch on {entry['severity']}: Salesforce enforced "
            f"{entry['liveTargetMins']} min on {entry['cases']} case(s) but the "
            f"{entry['tier']} contract documents "
            f"{entry['documentedTargetMins']} min{sample}. Either the Salesforce "
            f"SLA process is misconfigured or {CONTRACT_REFERENCE} is stale — "
            "resolve before this reaches a customer."
        )
    for label in SEV_LABELS:
        row = per_sev[label]
        if row["bound"] and not row["scored"]:
            warnings.append(
                f"{label}: {row['bound']} case(s) are SLA-bound on initial "
                "response but none carry a usable First Response milestone. "
                "Excluded from adherence rather than counted as met."
            )
    if unknown_sev:
        warnings.append(
            f"{unknown_sev} case(s) have no determinable severity at open and are "
            "excluded from SLA scoring."
        )
    if not tier and overall["scored"]:
        warnings.append(
            "Targets shown are Salesforce-enforced only: the subscription tier "
            "could not be determined, so no comparison against the contractual "
            "table was possible."
        )

    subscription = {k: v for k, v in tier_info.items() if k != "warnings"}
    return {
        "basis": "initial response",
        "scope": "all severities (Sev 1–4)",
        "headlineScope": "Sev 1–2",
        "subscription": subscription,
        "bySeverity": per_sev,
        "overall": overall,
        "headline": headline,
        "unknownSeverity": unknown_sev,
        "targetMismatches": mismatch_rows,
        "warnings": warnings,
    }


def map_support(
    cases: List[Dict[str, Any]],
    entitlements: List[Dict[str, Any]],
    milestones: Optional[List[Dict[str, Any]]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    tier_info: Optional[Dict[str, Any]] = None,
    sla_scoring: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    milestones = milestones or []
    history = history or []
    if sla_scoring is None:
        sla_scoring = map_sla_scoring(cases, milestones, history, tier_info)

    # Bucket by SLA severity = severity at open (history), not current/final
    opened_labels = []
    for c in cases:
        opened, _final = _opened_and_final_severity(c, history)
        opened_labels.append(opened or _normalize_sev_label(_case_severity_raw(c)))
    buckets = Counter(_priority_bucket(lbl) for lbl in opened_labels)

    open_cases = [c for c in cases if not c.get("IsClosed")]
    closed = [c for c in cases if c.get("IsClosed")]

    # P1 MTTR from cases that opened as Sev 1
    mttr_hours = 0.0
    mttr_n = 0
    for c, opened in zip(cases, opened_labels):
        if not c.get("IsClosed") or _priority_bucket(opened) != "p1":
            continue
        hrs = _case_resolution_hours(c)
        if hrs is None:
            continue
        mttr_hours += hrs
        mttr_n += 1
    avg_mttr = round(mttr_hours / mttr_n, 1) if mttr_n else 0.0

    # slaMetPct stays the P1/P2 first-response headline the deck already renders.
    # All-severity adherence is additive, under _slaAllSeverities.
    headline = sla_scoring.get("headline") or {}
    overall = sla_scoring.get("overall") or {}
    sla_total = headline.get("scored") or 0
    sla_met = headline.get("pct") or 0

    themes = Counter()
    for c in cases:
        reason = (
            _first_str(
                c,
                "Cause_Code__c",
                "Closure_Class__c",
                "Product__c",
                "Technology_Product__c",
                "Reason",
                "Type",
                "Ticket_Type__c",
            )
            or "Other"
        )
        themes[reason] += 1
    total = max(sum(themes.values()), 1)
    time_spent = [
        {"pct": round(100 * n / total), "label": f"{label} — {n} cases"}
        for label, n in themes.most_common(4)
    ]

    active_ents = [
        {
            "name": _first_str(e, "Name"),
            "type": _first_str(e, "Type"),
            "status": _first_str(e, "Status"),
            "start": _first_str(e, "StartDate"),
            "end": _first_str(e, "EndDate"),
            # Kept so the tier signal is auditable from a written payload.
            "slaProcessId": _first_str(e, "SlaProcessId") or None,
        }
        for e in entitlements
    ]

    return {
        "ticketsTotal": len(cases),
        "p1Count": buckets.get("p1", 0),
        "p1Delta": 0,
        "slaMetPct": sla_met,
        "slaDeltaPp": 0,
        "p1MttrHours": avg_mttr,
        "p1MttrTargetHours": 3.0,
        "csat": 0,
        "ticketsBySeverity": {
            "p1": buckets.get("p1", 0),
            "p2": buckets.get("p2", 0),
            "p3": buckets.get("p3", 0),
            "p4": buckets.get("p4", 0) + buckets.get("other", 0),
        },
        "timeSpent": time_spent,
        "_openCases": len(open_cases),
        "_closedCases": len(closed),
        "_slaSampleSize": sla_total,
        "_slaAllSeverities": {
            "scored": overall.get("scored", 0),
            "met": overall.get("met", 0),
            "breached": overall.get("breached", 0),
            "pct": overall.get("pct"),
        },
        "_subscriptionTier": (sla_scoring.get("subscription") or {}).get("tier"),
        "_recentCases": [
            {
                "id": _first_str(c, "CaseNumber", "Id"),
                "subject": _first_str(c, "Subject"),
                "priority": opened_labels[i] or _normalize_sev_label(_case_severity_raw(c)),
                "status": _first_str(c, "Status"),
                "created": _first_str(c, "CreatedDate"),
            }
            for i, c in enumerate(cases[:8])
        ],
        "_entitlements": active_ents,
    }


def map_incidents_from_p1(
    cases: List[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    history = history or []
    incidents = []
    for i, c in enumerate(cases, start=1):
        opened, _final = _opened_and_final_severity(c, history)
        if _priority_bucket(opened or _case_severity_raw(c)) != "p1":
            continue
        created = _first_str(c, "CreatedDate")[:10]
        hrs = _case_resolution_hours(c)
        incidents.append(
            {
                "id": _first_str(c, "CaseNumber") or f"INC-{i:02d}",
                "date": created,
                "severity": "P1",
                "summary": _first_str(c, "Subject") or "P1 case",
                "duration": _format_duration(hrs, bool(c.get("IsClosed"))),
                "rcaStatus": "CLOSED" if c.get("IsClosed") else "OPEN",
                "action": _first_str(c, "Resolution__c", "Cause__c")[:240],
            }
        )
        if len(incidents) >= 6:
            break
    return incidents


def map_source_review(
    cases: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    milestones: List[Dict[str, Any]],
    months: int = CASE_MONTHS,
    tier_info: Optional[Dict[str, Any]] = None,
    sla_scoring: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Shape consumed by perf-report.html (sourceReview.ticketDetail)."""
    if sla_scoring is None:
        sla_scoring = map_sla_scoring(cases, milestones, history, tier_info)
    tier = (sla_scoring.get("subscription") or {}).get("tier")
    open_n = sum(1 for c in cases if not c.get("IsClosed"))
    closed_n = len(cases) - open_n
    sev_counts = Counter()
    ticket_detail = []

    for c in cases:
        opened, final = _opened_and_final_severity(c, history)
        # SLA clock uses severity at open, whichever severity that was. Final
        # severity is kept for reference only — progression is normal workflow.
        sla_sev = opened or final
        if sla_sev:
            sev_counts[sla_sev] += 1
        hrs = _case_resolution_hours(c)
        is_closed = bool(c.get("IsClosed"))
        status = "Closed" if is_closed else "Open"
        contact = ""
        contact_rel = c.get("Contact")
        if isinstance(contact_rel, dict):
            contact = (contact_rel.get("Name") or "").strip()
        if not contact:
            contact = _first_str(c, "Contact_Name__c")

        issue = _first_str(c, "Description", "Symptoms__c", "Cause__c")
        resolution = _first_str(c, "Resolution__c", "Cause__c")
        adh = _case_sla_adherence(c, milestones, sla_sev or "", tier=tier)
        ticket_detail.append(
            {
                "caseNumber": _first_str(c, "CaseNumber"),
                "contact": contact or None,
                "openedAs": opened or final or "Sev 4",
                "severity": sla_sev or "Sev 4",  # SLA severity (at open)
                "status": status,
                "created": _first_str(c, "CreatedDate")[:10],
                "duration": _format_duration(hrs, is_closed),
                "subject": _first_str(c, "Subject"),
                "issue": issue[:2000] if issue else "",
                "resolution": resolution[:2000] if resolution else "",
                "owner": None,
                "slaBound": adh["slaBound"],
                "slaHeadline": adh["slaHeadline"],
                "slaBreach": adh["slaBreach"],
                "slaTargetMins": adh["slaTargetMins"],
                "slaTargetSource": adh["slaTargetSource"],
                "slaContractTargetMins": adh["slaContractTargetMins"],
                "slaTargetMismatch": adh["slaTargetMismatch"],
                "slaActualMins": adh["slaActualMins"],
                "slaMilestone": adh["slaMilestone"],
            }
        )

    theme_counter = Counter()
    for c in cases:
        for key in ("Cause_Code__c", "Closure_Class__c", "Product__c", "Technology_Product__c", "Ticket_Type__c"):
            val = _first_str(c, key)
            if val:
                theme_counter[f"{key.replace('__c','').replace('_',' ')}: {val}"] += 1
    recurring = [f"{label} ({n})" for label, n in theme_counter.most_common(8)]

    def sev_n(label: str) -> int:
        return sev_counts.get(label, 0)

    overall = sla_scoring.get("overall") or {}
    adherence_text = (
        f"{overall.get('pct')}% initial-response adherence across all four "
        f"severities ({overall.get('met', 0)}/{overall.get('scored', 0)} scored)"
        if overall.get("pct") is not None
        else "no case carried a usable First Response milestone, so adherence is unscored"
    )

    return {
        "period": f"past {months} months",
        "generated": datetime.now(timezone.utc).isoformat(),
        "ticketCounts": {"total": len(cases), "open": open_n, "closed": closed_n},
        "executiveSummary": None,
        "overview": (
            f"Salesforce Case history for the past {months} months: {len(cases)} tickets "
            f"({open_n} open, {closed_n} closed). "
            f"SLA severity at open: Sev 1={sev_n('Sev 1')}, Sev 2={sev_n('Sev 2')}, "
            f"Sev 3={sev_n('Sev 3')}, Sev 4={sev_n('Sev 4')}. "
            f"Subscription tier: {tier or 'unknown'} — {adherence_text}."
        ),
        "ticketSummary": {
            "total": len(cases),
            "p1": sev_n("Sev 1"),
            "p2": sev_n("Sev 2"),
            "p3": sev_n("Sev 3"),
            "p4": sev_n("Sev 4"),
        },
        "recurringThemes": recurring,
        "notableIncidents": [
            {
                "caseNumber": t["caseNumber"],
                "text": f"[{t['caseNumber']}] {t['subject']}",
            }
            for t in ticket_detail
            if t.get("severity") in ("Sev 1", "Sev 2") and t.get("slaBreach") is True
        ][:5],
        "healthTrajectory": None,
        "recommendations": [],
        "contacts": sorted(
            {t["contact"] for t in ticket_detail if t.get("contact")}
        ),
        "slaScoring": sla_scoring,
        "ticketDetail": ticket_detail,
    }


def map_health_from_cases(cases: List[Dict[str, Any]], support: Dict[str, Any]) -> Dict[str, Any]:
    open_p1 = sum(
        1
        for c in cases
        if not c.get("IsClosed") and _priority_bucket(_case_severity_raw(c)) == "p1"
    )
    total = len(cases)
    if open_p1:
        status = "At Risk"
    elif total <= 2:
        status = "Low Activity"
    else:
        status = "Healthy"
    return {
        "composite": None,
        "compositeDelta": None,
        "status": status,
        "adoption": None,
        "adoptionDelta": None,
        "support": support.get("slaMetPct"),
        "supportDelta": None,
        "sentiment": None,
        "sentimentDelta": None,
        "commercial": None,
        "commercialDelta": None,
        "history": [],
    }


def map_risks_from_signals(
    licenses: List[Dict[str, Any]],
    cases: List[Dict[str, Any]],
    environments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    risks: List[Dict[str, Any]] = []
    today = datetime.now(timezone.utc).date()

    # License expiry within 120 days
    for lic in licenses:
        end = _first_str(lic, "End_Date__c")
        if not end:
            continue
        try:
            end_d = datetime.fromisoformat(end[:10]).date()
        except Exception:
            continue
        days = (end_d - today).days
        if 0 <= days <= 120:
            product = _first_str(lic, "Product_Name__c", "Product__c", "Name")
            risks.append(
                {
                    "id": f"R-LIC-{len(risks)+1:02d}",
                    "level": "high" if days <= 45 else "med",
                    "title": f"License expiry — {product or 'license'}",
                    "body": f"License ends {end_d.isoformat()} ({days} days). Confirm renewal / expansion path in this QBR.",
                    "owner": "Joint",
                    "dueDate": end_d.isoformat(),
                    "action": "Confirm renewal sponsor + proposal date",
                }
            )

    p1_open = []
    for c in cases:
        opened, _ = _opened_and_final_severity(c, [])  # current-only if no history passed
        # Prefer live Severity_Level when open; fall back to raw
        sev = _normalize_sev_label(_case_severity_raw(c)) or opened
        if _priority_bucket(sev) == "p1" and not c.get("IsClosed"):
            p1_open.append(c)
    if p1_open:
        risks.append(
            {
                "id": f"R-P1-{len(risks)+1:02d}",
                "level": "high",
                "title": f"{len(p1_open)} open P1 case(s)",
                "body": "; ".join(_first_str(c, "Subject") for c in p1_open[:3]),
                "owner": "Mirantis Support",
                "dueDate": "This quarter",
                "action": "Drive to RCA + permanent fix before renewal narrative",
            }
        )

    if environments and not any(_sum_nodes(e) for e in environments):
        risks.append(
            {
                "id": f"R-ENV-{len(risks)+1:02d}",
                "level": "med",
                "title": "Environment node counts incomplete",
                "body": f"{len(environments)} Environment__c records found but node fields are empty — validate CMDB/SF sync.",
                "owner": "TAM + Customer",
                "dueDate": "Before next QBR",
                "action": "Backfill Environment__c compute/controller counts",
            }
        )

    return risks[:5]


def map_exec_takeaways(
    acct_name: str,
    usage: Dict[str, Any],
    support: Dict[str, Any],
    products: List[str],
    risks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    takeaways = [
        {
            "kind": "ADOPTION",
            "label": "01 · FOOTPRINT",
            "headline": f"{usage.get('environments', 0)} environments · {usage.get('nodes', 0)} nodes",
            "body": f"Pulled from Environment__c for {acct_name}. Review node mix and version skew in Product mix.",
        },
        {
            "kind": "SUPPORT",
            "label": "02 · SUPPORT",
            "headline": f"{support.get('ticketsTotal', 0)} cases · {support.get('p1Count', 0)} P1 · {support.get('slaMetPct', 0)}% SLA",
            "body": f"Open cases: {support.get('_openCases', 0)}. P1 MTTR (closed): {support.get('p1MttrHours', 0)}h.",
        },
    ]
    if products:
        takeaways.append(
            {
                "kind": "COMMERCIAL",
                "label": "03 · STACK",
                "headline": " · ".join(products[:4]),
                "body": "From License__c / Environment__c — confirm entitlement vs in-use with the customer.",
            }
        )
    if risks:
        takeaways.append(
            {
                "kind": "RISK",
                "label": "04 · WATCH",
                "headline": risks[0].get("title") or "Risk flagged",
                "body": risks[0].get("body") or "",
            }
        )
    return takeaways


def fetch_mirantis_bundle(sf: Salesforce, account_id: str) -> Dict[str, Any]:
    """Pull Mirantis-centric related data for one Account Id."""
    warnings: List[str] = []
    envs, _, w = query_related(
        sf, "Environment__c", account_id, ENV_PREFERRED,
        extra_keywords=["node", "version", "product", "status", "region", "mke", "mosk"],
    )
    if w:
        warnings.append(w)
    licenses, _, w = query_related(
        sf, "License__c", account_id, LICENSE_PREFERRED,
        extra_keywords=["product", "node", "seat", "core", "date", "status", "type"],
    )
    if w:
        warnings.append(w)
    cases, w = query_cases(sf, account_id)
    if w:
        warnings.append(w)
    entitlements, _, w = query_related(
        sf, "Entitlement", account_id, ENTITLEMENT_PREFERRED,
        extra_keywords=["service level", "support level", "tier", "offering", "opscare"],
    )
    if w:
        warnings.append(w)
    # SlaProcess.Name is the leading candidate for the subscription tier.
    sla_processes, w = query_sla_processes(
        sf, [_first_str(e, "SlaProcessId") for e in entitlements]
    )
    if w:
        warnings.append(w)

    case_ids = [c["Id"] for c in cases if c.get("Id")]
    history, w = query_case_severity_history(sf, case_ids)
    if w:
        warnings.append(w)
    milestones, w = query_case_milestones(sf, case_ids)
    if w:
        warnings.append(w)

    return {
        "environments": envs,
        "licenses": licenses,
        "cases": cases,
        "entitlements": entitlements,
        "slaProcesses": sla_processes,
        "caseHistory": history,
        "caseMilestones": milestones,
        "warnings": warnings,
    }
