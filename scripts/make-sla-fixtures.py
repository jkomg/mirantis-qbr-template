#!/usr/bin/env python3
"""Deterministic synthetic account fixtures for the SLA & Service Performance report.

The Salesforce sandbox has too few cases (and almost no Sev 1/2) to exercise the
first-response SLA logic in `perf-report.html`. This writes plausible
`accounts/demo-*.json` payloads at realistic volume instead.

Payload shape is derived from, and must stay aligned with:
  * `build_payload()`                      in `sf-sync/sync.py`
  * `map_source_review()` / `map_support()` in `sf-sync/mirantis.py`
  * `computeAccountStats()` / `computePortfolio()` in `perf-report.html`

Edge cases these fixtures deliberately cover, from contract gaps found while
deriving the shape above (both since fixed in `perf-report.html`):
  * SLA severity precedence. `computeAccountStats()` now reads `openedAs`
    before `severity`, and boundness additionally requires a P1/P2 bucket.
    Fixtures exercise the fallback (no `severity` key), the disagreement
    (`openedAs` and `severity` differ), and a P3/P4 row that nonetheless
    declares `slaBound: true`.
  * `sourceReview.executiveSummary`, `healthTrajectory` and `recommendations`
    are rendered by the report but the sidecar always emits None/[]. Curated
    dumps such as `demo/meridian-financial-solutions.json` do populate them, so
    one fixture fills them to keep those branches exercised.

Safety: this script only ever writes or deletes files whose name starts with
`demo-` and ends with `.json`, directly inside the target accounts directory.
`accounts/` holds real customer data; every filesystem call goes through
`assert_demo_target()`.

Determinism: no wall-clock reads. `--as-of` defaults to a pinned date so that
repeated runs with the same seed are byte-identical. Only `random.Random`
methods with stable implementations (`random`, `uniform`, `choice`, `choices`,
`randint`) are used — `gauss`/`lognormvariate` are avoided because their
internals have changed between CPython releases.

Usage:
    python3 scripts/make-sla-fixtures.py
    python3 scripts/make-sla-fixtures.py --seed 7 --out-dir accounts
    python3 scripts/make-sla-fixtures.py --dry-run
    python3 scripts/make-sla-fixtures.py --clean
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Contract constants (mirrored from the authorities named in the docstring)
# --------------------------------------------------------------------------
SCHEMA_VERSION = "qbr-2026.06"          # sync.py SCHEMA_VERSION
CASE_MONTHS = 24                        # mirantis.py CASE_MONTHS
DEMO_PREFIX = "demo-"
DEFAULT_SEED = 20260701
DEFAULT_AS_OF = "2026-06-30"

SEV_LABELS = ("Sev 1", "Sev 2", "Sev 3", "Sev 4")

# perf-report.html SLA_BOUND — P1/P2 First Response only.
SLA_BOUND = {"Sev 1": True, "Sev 2": True, "Sev 3": False, "Sev 4": False}

# Stands in for Entitlement milestone TargetResponseInMins.
FIRST_RESPONSE_TARGET_MINS = {"Sev 1": 30, "Sev 2": 120}

# perf-report.html parseDurationHours() accepts exactly these three forms.
DURATION_PATTERNS = (
    re.compile(r"^(\d+)d (\d+)h$"),
    re.compile(r"^(\d+(?:\.\d+)?)h$"),
    re.compile(r"^(\d+(?:\.\d+)?)m$"),
)

# Wall-clock hours to closure, as (low, high, weight) buckets per severity.
RESOLUTION_BUCKETS = {
    "Sev 1": [(1, 6, 0.35), (6, 24, 0.35), (24, 96, 0.22), (96, 336, 0.08)],
    "Sev 2": [(2, 12, 0.25), (12, 48, 0.35), (48, 168, 0.28), (168, 720, 0.12)],
    "Sev 3": [(4, 24, 0.18), (24, 120, 0.34), (120, 480, 0.33), (480, 1800, 0.15)],
    "Sev 4": [(1, 12, 0.20), (12, 96, 0.30), (96, 600, 0.30), (600, 2600, 0.20)],
}

# Support queues are not flat across the year: upgrade windows in spring, change
# freezes and year-end pushes in autumn, quiet holidays.
SEASONAL_WEIGHT = {
    1: 0.60, 2: 0.80, 3: 1.35, 4: 1.40, 5: 1.10, 6: 0.95,
    7: 0.80, 8: 0.85, 9: 1.15, 10: 1.45, 11: 1.30, 12: 0.55,
}

# Case open time of day, UTC — weighted toward NA/EU business hours.
HOUR_WEIGHTS = [
    0.4, 0.3, 0.3, 0.3, 0.4, 0.6, 0.9, 1.2, 1.6, 1.9, 2.0, 2.0,
    2.1, 2.4, 2.5, 2.4, 2.2, 1.9, 1.5, 1.1, 0.9, 0.7, 0.6, 0.5,
]

ENVIRONMENTS = [
    "prod-us-east-1", "prod-us-west-2", "prod-eu-central-1", "prod-apac-1",
    "stage-us-east-1", "dr-us-east-2", "lab-01",
]
VERSIONS = [
    "MKE 3.6.8", "MKE 3.7.2", "MKE 3.7.5", "MSR 3.0.14",
    "MOSK 23.2", "MCR 23.0.9", "k0rdent 0.9",
]

# Cause codes carry the keywords perf-report.html buckets recurringThemes on.
CAUSE_CODES: Dict[str, Dict[str, List[str]]] = {
    "Upgrade / Migration": {
        "subjects": [
            "{ver} upgrade stalls during manager reconcile on {env}",
            "Rolling node upgrade leaves workers NotReady in {env}",
            "Migration off {ver} blocked by etcd quorum precheck in {env}",
        ],
        "issue": [
            "Upgrade orchestration halted partway through the manager pool; cluster left on mixed versions.",
            "Worker nodes rejoined after upgrade but stayed NotReady, dropping scheduling capacity.",
        ],
        "resolution": [
            "Drained and re-ran the upgrade on the stuck node, then completed the pool in sequence.",
            "Cleared the stale reconcile lock and resumed the upgrade during the next change window.",
        ],
    },
    "License Fulfillment": {
        "subjects": [
            "License file rejected after renewal on {env}",
            "Node count exceeds licensed entitlement in {env}",
            "License expiry banner persists after new key applied to {env}",
        ],
        "issue": [
            "New license artifact failed validation, leaving the cluster in an unlicensed warning state.",
            "Licensed node ceiling reached, blocking further worker registration.",
        ],
        "resolution": [
            "Reissued the license with the correct entitlement scope and confirmed acceptance.",
            "Right-sized the entitlement with Sales and reapplied the key.",
        ],
    },
    "Cluster Stability": {
        "subjects": [
            "Intermittent API server 5xx under load in {env}",
            "Manager node crash loop after resource exhaustion in {env}",
            "Stability regression after patch rollout to {env}",
        ],
        "issue": [
            "Control plane returned intermittent 5xx during peak load, degrading deployments.",
            "Manager node entered a crash loop once memory headroom was exhausted.",
        ],
        "resolution": [
            "Raised manager resource reservations and tuned request limits on the noisy workload.",
            "Rebalanced control plane placement and added headroom alerts.",
        ],
    },
    "Storage / Disk": {
        "subjects": [
            "Persistent volume stuck Terminating on {env}",
            "Disk pressure evictions across the worker pool in {env}",
            "Filesystem permission denied on mounted volume in {env}",
        ],
        "issue": [
            "Volume detach never completed, so the replacement pod could not bind.",
            "Node-level disk pressure triggered repeated evictions of stateful workloads.",
        ],
        "resolution": [
            "Cleared the dangling attachment and restored the volume binding.",
            "Expanded the data partition and added a disk usage alert threshold.",
        ],
    },
    "Access Management": {
        "subjects": [
            "Access management: LDAP group sync drops team membership on {env}",
            "SSO login loop against the identity provider on {env}",
            "Onboarding admin personnel for {env}",
        ],
        "issue": [
            "Group synchronisation removed grants, locking operators out of their namespaces.",
            "Identity provider assertions failed validation and the login redirect looped.",
        ],
        "resolution": [
            "Corrected the group mapping filter and re-ran synchronisation.",
            "Realigned the assertion signing certificate and confirmed sign-in.",
        ],
    },
    "Networking": {
        "subjects": [
            "Overlay network partition between worker subnets in {env}",
            "Interlock ingress returns 503 for newly published services in {env}",
            "Node integration fails DNS resolution in {env}",
        ],
        "issue": [
            "Cross-subnet overlay traffic dropped, partitioning workloads mid-request.",
            "Ingress did not pick up new service labels, so published routes returned 503.",
        ],
        "resolution": [
            "Repaired the MTU mismatch on the overlay path and verified east-west traffic.",
            "Restarted the ingress controllers and corrected the label selector.",
        ],
    },
    "Certificate Management": {
        "subjects": [
            "Certificate rotation fails on manager nodes in {env}",
            "Expired client certificate blocks kubectl access to {env}",
            "Certificate chain mismatch after renewal in {env}",
        ],
        "issue": [
            "Automated rotation left one manager serving an expired certificate.",
            "Client bundle expired without renewal, cutting off operator access.",
        ],
        "resolution": [
            "Rotated the certificate manually and re-enabled the scheduled renewal job.",
            "Rebuilt the trust chain with the current intermediate and redistributed bundles.",
        ],
    },
    "Lifecycle Automation": {
        "subjects": [
            "Lifecycle hook timeout during scheduled patch of {env}",
            "Version drift remediation job fails on {env}",
        ],
        "issue": [
            "Lifecycle automation timed out mid-patch, leaving the run half-applied.",
            "Drift remediation could not converge the declared version.",
        ],
        "resolution": [
            "Extended the hook timeout and re-ran the patch cleanly.",
            "Pinned the target version and let remediation converge.",
        ],
    },
    "Observability": {
        "subjects": [
            "Metrics gaps in the Prometheus scrape for {env}",
            "Alert routing silently dropped for {env}",
        ],
        "issue": [
            "Scrape targets flapped, leaving gaps in the retained metric series.",
            "Alert routing dropped notifications without surfacing an error.",
        ],
        "resolution": [
            "Corrected the scrape interval and relabelling rules.",
            "Fixed the receiver configuration and replayed a test alert.",
        ],
    },
    "Product Guidance": {
        "subjects": [
            "Clarification needed on the {ver} support matrix",
            "Guidance request: recommended node sizing for {env}",
        ],
        "issue": [
            "Customer asked for written confirmation of a supported configuration.",
            "Sizing guidance requested ahead of a capacity change.",
        ],
        "resolution": [
            "Supplied the support matrix reference and confirmed the configuration.",
            "Shared sizing guidance and agreed a follow-up review.",
        ],
    },
}

CONTACT_FIRST = [
    "Alena", "Marcus", "Priya", "Tobias", "Renata", "Devon", "Ingrid",
    "Hassan", "Yuki", "Callum", "Nadia", "Emeka",
]
CONTACT_LAST = [
    "Okafor", "Whitfield", "Raman", "Lindqvist", "Duarte", "Halloran",
    "Petrov", "Nakamura", "Bergeron", "Adeyemi", "Kovac", "Sandoval",
]


# --------------------------------------------------------------------------
# Filesystem safety gate — the only place writes and deletes are authorised
# --------------------------------------------------------------------------
def is_demo_target(path: Path, out_dir: Path) -> bool:
    """True only for `<out_dir>/demo-*.json` with no traversal component."""
    name = path.name
    return (
        name.startswith(DEMO_PREFIX)
        and name.endswith(".json")
        and len(name) > len(DEMO_PREFIX) + len(".json")
        and ".." not in path.parts
        and path.parent.resolve() == out_dir.resolve()
    )


def assert_demo_target(path: Path, out_dir: Path, action: str) -> None:
    if not is_demo_target(path, out_dir):
        raise SystemExit(
            f"refusing to {action} {path}: only {out_dir}/{DEMO_PREFIX}*.json is writable "
            "by this script (accounts/ holds real customer data)"
        )
    if path.is_symlink():
        raise SystemExit(
            f"refusing to {action} {path}: it is a symlink and could point outside {out_dir}"
        )


# --------------------------------------------------------------------------
# Account specs
# --------------------------------------------------------------------------
@dataclass
class AccountSpec:
    slug: str
    name: str
    tier: str
    industry: str
    cases: int
    # Severity-at-open mix, weighted toward Sev 3/4 as real queues are.
    sev_mix: Tuple[float, float, float, float]
    # First-response breach probability for the SLA-bound severities.
    breach_rate: Dict[str, float]
    # Share of P1/P2 cases with no First Response milestone at all (unknown).
    no_milestone_rate: float
    open_rate: float
    reclass_rate: float
    trend: Tuple[float, float]
    nodes: int
    clusters: int
    arr: int
    stakeholders: int
    write_quarter_twin: bool = False
    edge_rows: bool = False
    narrative: bool = False
    themes_limit: int = 8
    warnings: List[str] = field(default_factory=list)


ACCOUNT_SPECS: List[AccountSpec] = [
    AccountSpec(
        slug="demo-northwind-grid-systems",
        name="Northwind Grid Systems",
        tier="Enterprise",
        industry="Energy & Utilities",
        cases=384,
        sev_mix=(0.06, 0.18, 0.44, 0.32),
        breach_rate={"Sev 1": 0.31, "Sev 2": 0.27},
        no_milestone_rate=0.06,
        open_rate=0.09,
        reclass_rate=0.16,
        trend=(0.55, 1.55),
        nodes=612,
        clusters=9,
        arr=2_480_000,
        stakeholders=6,
        write_quarter_twin=True,
    ),
    AccountSpec(
        slug="demo-cascadia-health-network",
        name="Cascadia Health Network",
        tier="Enterprise",
        industry="Healthcare",
        cases=128,
        sev_mix=(0.03, 0.11, 0.47, 0.39),
        breach_rate={"Sev 1": 0.05, "Sev 2": 0.06},
        no_milestone_rate=0.03,
        open_rate=0.06,
        reclass_rate=0.10,
        trend=(1.40, 0.70),
        nodes=214,
        clusters=4,
        arr=910_000,
        stakeholders=4,
        narrative=True,
    ),
    AccountSpec(
        slug="demo-talos-robotics",
        name="Talos Robotics",
        tier="Growth",
        industry="Industrial Automation",
        cases=27,
        sev_mix=(0.07, 0.19, 0.41, 0.33),
        breach_rate={"Sev 1": 0.18, "Sev 2": 0.15},
        no_milestone_rate=0.12,
        open_rate=0.15,
        reclass_rate=0.12,
        trend=(0.85, 1.20),
        nodes=48,
        clusters=2,
        arr=180_000,
        stakeholders=3,
        edge_rows=True,
    ),
    AccountSpec(
        slug="demo-juniper-fields-coop",
        name="Juniper Fields Co-op",
        tier="Growth",
        industry="Agriculture",
        cases=3,
        sev_mix=(0.0, 0.0, 0.55, 0.45),
        breach_rate={"Sev 1": 0.0, "Sev 2": 0.0},
        no_milestone_rate=0.0,
        open_rate=0.33,
        reclass_rate=0.0,
        trend=(1.0, 1.0),
        nodes=11,
        clusters=1,
        arr=42_000,
        stakeholders=1,
    ),
]

# Fifth account is assembled by hand — it is the defensive-path fixture and has
# no generated cases at all.
SPARSE_SPEC = AccountSpec(
    slug="demo-edgecase-sparse-systems",
    name="Sparse Systems Ltd",
    tier="",
    industry="",
    cases=0,
    sev_mix=(0.0, 0.0, 0.0, 1.0),
    breach_rate={"Sev 1": 0.0, "Sev 2": 0.0},
    no_milestone_rate=0.0,
    open_rate=0.0,
    reclass_rate=0.0,
    trend=(1.0, 1.0),
    nodes=0,
    clusters=0,
    arr=0,
    stakeholders=0,
    warnings=[
        "Environment__c: object not accessible for this synthetic account",
        "Case: no rows returned in the 24-month window",
    ],
)


def edge_case_rows() -> List[Dict[str, Any]]:
    """Rows that exercise the report's defensive branches.

    Each entry is annotated with the branch in `computeAccountStats()` /
    `slaLabel()` it is there to cover. Dates are literals inside the default
    `--as-of` window; the report does not filter by date, so a shifted window
    only makes them look older.
    """
    return [
        {   # slaBound P1 with no milestone -> "no milestone", excluded from scored
            "caseNumber": "09900001",
            "contact": "Marcus Whitfield",
            "openedAs": "Sev 1",
            "severity": "Sev 1",
            "status": "Closed",
            "created": "2025-11-04",
            "duration": "2d 6h",
            "subject": "Control plane outage with no First Response milestone recorded",
            "issue": "Milestone rows are absent for this case, so first response cannot be scored.",
            "resolution": "Restored the control plane; entitlement milestone data missing in source.",
            "owner": None,
            "slaBound": True,
            "slaBreach": None,
            "slaTargetMins": None,
            "slaActualMins": None,
            "slaMilestone": None,
        },
        {   # slaBound false with a breach flag -> report must null it out
            "caseNumber": "09900002",
            "contact": None,
            "openedAs": "Sev 3",
            "severity": "Sev 3",
            "status": "Closed",
            "created": "2025-12-15",
            "duration": "9d 3h",
            "subject": "Storage cleanup request flagged breached but not SLA-bound",
            "issue": "Row carries slaBreach true while slaBound is false.",
            "resolution": "Must not count toward met or breached totals.",
            "owner": None,
            "slaBound": False,
            "slaBreach": True,
            "slaTargetMins": 480,
            "slaActualMins": 900,
            "slaMilestone": "First Response",
        },
        {   # no `severity` key -> sevKey(row.openedAs) fallback
            "caseNumber": "09900003",
            "contact": "Priya Raman",
            "openedAs": "Sev 2",
            "status": "Closed",
            "created": "2026-01-22",
            "duration": "31h",
            "subject": "Networking degradation with severity field absent",
            "issue": "Only openedAs is present; the report must fall back to it.",
            "resolution": "Overlay path repaired.",
            "owner": None,
            "slaBound": True,
            "slaBreach": True,
            "slaTargetMins": 120,
            "slaActualMins": 356,
            "slaMilestone": "First Response",
        },
        {   # neither severity nor openedAs -> 'P4' fallback, slaBound omitted
            "caseNumber": "09900004",
            "contact": None,
            "openedAs": None,
            "severity": None,
            "status": "Closed",
            "created": "2026-02-08",
            "duration": "4h",
            "subject": "Case with no severity of any kind",
            "issue": None,
            "resolution": None,
            "owner": None,
            "slaBreach": None,
            "slaTargetMins": None,
            "slaActualMins": None,
            "slaMilestone": None,
        },
        {   # slaBound key absent -> derived from SLA_BOUND[slaSev]
            "caseNumber": "09900005",
            "contact": "Ingrid Lindqvist",
            "openedAs": "Sev 1",
            "severity": "Sev 1",
            "status": "Closed",
            "created": "2026-03-02",
            "duration": "18h",
            "subject": "Sev 1 with slaBound omitted from the payload",
            "issue": "slaBound is missing; the report derives boundness from severity.",
            "resolution": "Cluster recovered inside the response window.",
            "owner": None,
            "slaBreach": False,
            "slaTargetMins": 30,
            "slaActualMins": 11,
            "slaMilestone": "First Response",
        },
        {   # empty duration on a closed case -> excluded from resolution stats
            "caseNumber": "09900006",
            "contact": None,
            "openedAs": "Sev 4",
            "severity": "Sev 4",
            "status": "Closed",
            "created": "2026-03-27",
            "duration": "",
            "subject": None,
            "issue": None,
            "resolution": None,
            "owner": None,
            "slaBound": False,
            "slaBreach": None,
            "slaTargetMins": None,
            "slaActualMins": None,
            "slaMilestone": None,
        },
        {   # `severity` and `openedAs` deliberately disagree. The report keys the
            # bucket off `openedAs` (P1 here), which is the commitment window
            # that actually applied. Kept inside P1/P2 either way, so the
            # fixture's own SLA math stays self-consistent.
            "caseNumber": "09900007",
            "contact": "Devon Halloran",
            "openedAs": "Sev 1",
            "severity": "Sev 2",
            "status": "Closed",
            "created": "2026-04-14",
            "duration": "3d 1h",
            "subject": "Opened Sev 1, stepped down after first response",
            "issue": "Severity at open and current severity deliberately disagree.",
            "resolution": "Stepped down once the workaround held.",
            "owner": None,
            "slaBound": True,
            "slaBreach": False,
            "slaTargetMins": 30,
            "slaActualMins": 22,
            "slaMilestone": "First Response",
        },
        {   # still open, no ClosedDate equivalent
            "caseNumber": "09900008",
            "contact": "Yuki Nakamura",
            "openedAs": "Sev 2",
            "severity": "Sev 2",
            "status": "Open",
            "created": "2026-06-19",
            "duration": "Open",
            "subject": "Certificate rotation follow-up still open",
            "issue": "Awaiting a maintenance window from the customer.",
            "resolution": "",
            "owner": None,
            "slaBound": True,
            "slaBreach": False,
            "slaTargetMins": 120,
            "slaActualMins": 47,
            "slaMilestone": "First Response",
        },
        {   # numeric SLA fields arriving as strings
            "caseNumber": "09900009",
            "contact": "Hassan Adeyemi",
            "openedAs": "Sev 2",
            "severity": "Sev 2",
            "status": "Closed",
            "created": "2026-05-06",
            "duration": "46h",
            "subject": "Milestone minutes arriving as strings from the source system",
            "issue": "slaTargetMins and slaActualMins are strings, not numbers.",
            "resolution": "Interlock ingress restarted.",
            "owner": None,
            "slaBound": True,
            "slaBreach": True,
            "slaTargetMins": "120",
            "slaActualMins": "415",
            "slaMilestone": "First Response",
        },
    ]


# --------------------------------------------------------------------------
# Deterministic helpers
# --------------------------------------------------------------------------
def derive_seed(seed: int, slug: str) -> int:
    """Per-account seed that does not shift when other accounts are added."""
    digest = hashlib.sha256(f"{seed}:{slug}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def month_starts(as_of: date, months: int) -> List[date]:
    out: List[date] = []
    for i in range(months - 1, -1, -1):
        total = (as_of.year * 12 + as_of.month - 1) - i
        out.append(date(total // 12, total % 12 + 1, 1))
    return out


def month_weights(spec: AccountSpec, starts: List[date]) -> List[float]:
    n = max(len(starts) - 1, 1)
    lo, hi = spec.trend
    weights = []
    for i, start in enumerate(starts):
        trend = lo + (hi - lo) * (i / n)
        weights.append(SEASONAL_WEIGHT[start.month] * trend)
    return weights


def pick_created(rng: random.Random, month: date, as_of: date) -> datetime:
    last_day = calendar.monthrange(month.year, month.month)[1]
    if month.year == as_of.year and month.month == as_of.month:
        last_day = min(last_day, as_of.day)
    days = list(range(1, last_day + 1))
    day_weights = [
        1.0 if date(month.year, month.month, d).weekday() < 5 else 0.25 for d in days
    ]
    day = rng.choices(days, weights=day_weights, k=1)[0]
    hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
    minute = rng.randint(0, 59)
    return datetime(month.year, month.month, day, hour, minute, tzinfo=timezone.utc)


def bucket_hours(rng: random.Random, severity: str) -> float:
    buckets = RESOLUTION_BUCKETS[severity]
    lo, hi, _ = rng.choices(buckets, weights=[b[2] for b in buckets], k=1)[0]
    return round(rng.uniform(lo, hi), 2)


def format_duration(hours: Optional[float], is_closed: bool) -> str:
    """Mirror of `_format_duration()` in mirantis.py."""
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
    days, rem = divmod(whole_hours, 24)
    if days > 0:
        return f"{days}d {rem}h"
    return f"{whole_hours}h"


def sev_key(value: Any) -> Optional[str]:
    """Mirror of `sevKey()` in perf-report.html."""
    match = re.search(r"([1-4])", str(value or ""))
    return f"P{match.group(1)}" if match else None


def parse_duration_hours(value: Any) -> Optional[float]:
    """Mirror of `parseDurationHours()` in perf-report.html."""
    if not value or value == "Open":
        return None
    text = str(value)
    m = DURATION_PATTERNS[0].match(text)
    if m:
        return int(m.group(1)) * 24 + int(m.group(2))
    m = DURATION_PATTERNS[1].match(text)
    if m:
        return float(m.group(1))
    m = DURATION_PATTERNS[2].match(text)
    if m:
        return float(m.group(1)) / 60.0
    return None


def step_severity(rng: random.Random, opened: str) -> str:
    """Severity after triage. Step-downs dominate; a small share escalate."""
    idx = SEV_LABELS.index(opened)
    candidates = [(idx + 1, 0.55), (idx + 2, 0.20), (idx - 1, 0.25)]
    valid = [(i, w) for i, w in candidates if 0 <= i < len(SEV_LABELS)]
    chosen = rng.choices([i for i, _ in valid], weights=[w for _, w in valid], k=1)[0]
    return SEV_LABELS[chosen]


# --------------------------------------------------------------------------
# Case synthesis
# --------------------------------------------------------------------------
@dataclass
class SynthCase:
    number: str
    created: datetime
    opened_as: str
    current: str
    is_closed: bool
    hours: Optional[float]
    cause: str
    subject: str
    issue: str
    resolution: str
    contact: Optional[str]
    sla_bound: bool
    sla_breach: Optional[bool]
    target_mins: Optional[int]
    actual_mins: Optional[int]
    milestone: Optional[str]


def make_contacts(rng: random.Random, n: int) -> List[str]:
    names: List[str] = []
    while len(names) < n:
        candidate = f"{rng.choice(CONTACT_FIRST)} {rng.choice(CONTACT_LAST)}"
        if candidate not in names:
            names.append(candidate)
    return names


CAUSE_WEIGHTS = {
    "Upgrade / Migration": 3.0,
    "License Fulfillment": 2.2,
    "Cluster Stability": 2.6,
    "Storage / Disk": 2.0,
    "Access Management": 1.6,
    "Networking": 2.4,
    "Certificate Management": 1.8,
    "Lifecycle Automation": 1.2,
    "Observability": 1.0,
    "Product Guidance": 0.8,
}
assert set(CAUSE_WEIGHTS) == set(CAUSE_CODES), "CAUSE_WEIGHTS must cover every cause code"


def synth_cases(spec: AccountSpec, rng: random.Random, as_of: date) -> List[SynthCase]:
    starts = month_starts(as_of, CASE_MONTHS)
    weights = month_weights(spec, starts)
    months = sorted(rng.choices(starts, weights=weights, k=spec.cases)) if spec.cases else []
    contacts = make_contacts(rng, max(spec.stakeholders, 1))
    cause_names = list(CAUSE_CODES)
    cause_weights = [CAUSE_WEIGHTS[name] for name in cause_names]
    as_of_dt = datetime(as_of.year, as_of.month, as_of.day, 23, 59, tzinfo=timezone.utc)

    cases: List[SynthCase] = []
    for month in months:
        created = pick_created(rng, month, as_of)
        opened_as = rng.choices(SEV_LABELS, weights=list(spec.sev_mix), k=1)[0]
        current = step_severity(rng, opened_as) if rng.random() < spec.reclass_rate else opened_as

        hours = bucket_hours(rng, opened_as)
        age_days = (as_of_dt - created).total_seconds() / 86400.0
        recency = 1.0 if age_days < 30 else 0.35 if age_days < 90 else 0.04
        is_closed = not (rng.random() < spec.open_rate * recency * 3.0)
        if is_closed and created + timedelta(hours=hours) > as_of_dt:
            is_closed = False
        if not is_closed:
            hours = None

        cause = rng.choices(cause_names, weights=cause_weights, k=1)[0]
        pack = CAUSE_CODES[cause]
        subject = rng.choice(pack["subjects"]).format(
            env=rng.choice(ENVIRONMENTS), ver=rng.choice(VERSIONS)
        )
        issue = rng.choice(pack["issue"])
        resolution = rng.choice(pack["resolution"]) if is_closed else ""
        if current != opened_as and resolution:
            resolution = f"{resolution} Reclassified {opened_as} to {current} after first response."
        contact = rng.choice(contacts) if rng.random() > 0.08 else None

        bound = SLA_BOUND[opened_as]
        breach: Optional[bool] = None
        target: Optional[int] = None
        actual: Optional[int] = None
        milestone: Optional[str] = None
        if bound:
            if rng.random() >= spec.no_milestone_rate:
                milestone = "First Response"
                target = FIRST_RESPONSE_TARGET_MINS[opened_as]
                breach = rng.random() < spec.breach_rate[opened_as]
                factor = rng.uniform(1.15, 4.2) if breach else rng.uniform(0.08, 0.94)
                actual = max(1, int(round(target * factor)))

        cases.append(
            SynthCase(
                number="",
                created=created,
                opened_as=opened_as,
                current=current,
                is_closed=is_closed,
                hours=hours,
                cause=cause,
                subject=subject,
                issue=issue,
                resolution=resolution,
                contact=contact,
                sla_bound=bound,
                sla_breach=breach,
                target_mins=target,
                actual_mins=actual,
                milestone=milestone,
            )
        )

    # Case numbers are issued in the order cases arrive, so they must be
    # assigned after the full set is sorted by open date.
    cases.sort(key=lambda c: c.created)
    base = 3_100_000 + (derive_seed(0, spec.slug) % 400_000)
    for i, case in enumerate(cases):
        case.number = f"{base + i:08d}"
    return cases


def case_to_row(case: SynthCase) -> Dict[str, Any]:
    """Mirror of one `ticketDetail` row from `map_source_review()`."""
    return {
        "caseNumber": case.number,
        "contact": case.contact,
        "openedAs": case.opened_as,
        "severity": case.opened_as,  # SLA severity is severity at open
        "status": "Closed" if case.is_closed else "Open",
        "created": case.created.date().isoformat(),
        "duration": format_duration(case.hours, case.is_closed),
        "subject": case.subject,
        "issue": case.issue,
        "resolution": case.resolution,
        "owner": None,
        "slaBound": case.sla_bound,
        "slaBreach": case.sla_breach,
        "slaTargetMins": case.target_mins,
        "slaActualMins": case.actual_mins,
        "slaMilestone": case.milestone,
    }


# --------------------------------------------------------------------------
# Aggregations
# --------------------------------------------------------------------------
def read_row(row: Dict[str, Any]) -> Tuple[str, bool, Optional[bool]]:
    """How `computeAccountStats()` reads one ticketDetail row.

    `openedAs` wins: the SLA severity is whichever one applied when the
    commitment was made. Boundness also requires a P1/P2 bucket, so a payload
    declaring `slaBound` on a P3/P4 row stays out of the scored set rather than
    entering the top-line percentage but not the per-severity breakdown. An
    unbound row's breach flag is discarded.
    """
    slasev = sev_key(row.get("openedAs")) or sev_key(row.get("severity")) or "P4"
    declared = row.get("slaBound")
    bound = SLA_BOUND[f"Sev {slasev[1]}"] and (declared is None or bool(declared))
    breach = row.get("slaBreach") if bound else None
    return slasev, bound, breach


def report_sla_math(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Recomputation of `computeAccountStats()` for the console summary and the
    self-checks in `validate_payload()`."""
    totals = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    bound: List[Tuple[str, Optional[bool]]] = []
    for row in rows:
        slasev, is_bound, breach = read_row(row)
        totals[slasev] += 1
        if is_bound:
            bound.append((slasev, breach))

    scored = [(sev, breach) for sev, breach in bound if breach is not None]
    met = sum(1 for _, breach in scored if breach is False)
    by_sev = {}
    for key in ("P1", "P2"):
        sub = [b for sev, b in scored if sev == key]
        hit = sum(1 for b in sub if b is False)
        by_sev[key] = {
            "total": len(sub),
            "met": hit,
            "breached": len(sub) - hit,
            "pct": round(100 * hit / len(sub)) if sub else None,
        }
    return {
        "slaTotals": totals,
        "bound": len(bound),
        "scored": len(scored),
        "met": met,
        "breached": len(scored) - met,
        "pct": round(100 * met / len(scored)) if scored else None,
        "bySev": by_sev,
    }


def theme_labels(rows_causes: List[str], limit: int) -> List[str]:
    counts: Dict[str, int] = {}
    for cause in rows_causes:
        counts[cause] = counts.get(cause, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [f"Cause Code: {label} ({n})" for label, n in ordered]


def build_support(cases: List[SynthCase], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Mirror of `map_support()`, keyed on severity at open."""
    buckets = {"p1": 0, "p2": 0, "p3": 0, "p4": 0}
    for row in rows:
        buckets[read_row(row)[0].lower()] += 1

    p1_hours = [c.hours for c in cases if c.is_closed and c.opened_as == "Sev 1" and c.hours]
    mttr = round(sum(p1_hours) / len(p1_hours), 1) if p1_hours else 0.0
    math = report_sla_math(rows)

    cause_counts: Dict[str, int] = {}
    for case in cases:
        cause_counts[case.cause] = cause_counts.get(case.cause, 0) + 1
    top = sorted(cause_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    total = max(sum(cause_counts.values()), 1)

    open_rows = [r for r in rows if r.get("status") == "Open"]
    return {
        "ticketsTotal": len(rows),
        "p1Count": buckets["p1"],
        "p1Delta": 0,
        "slaMetPct": math["pct"] or 0,
        "slaDeltaPp": 0,
        "p1MttrHours": mttr,
        "p1MttrTargetHours": 3.0,
        "csat": 0,
        "ticketsBySeverity": buckets,
        "timeSpent": [
            {"pct": round(100 * n / total), "label": f"{label} — {n} cases"}
            for label, n in top
        ],
        "_openCases": len(open_rows),
        "_closedCases": len(rows) - len(open_rows),
        "_slaSampleSize": math["scored"],
        "_recentCases": [
            {
                "id": r["caseNumber"],
                "subject": r.get("subject") or "",
                "priority": r.get("openedAs") or "Sev 4",
                "status": r.get("status") or "",
                "created": r.get("created") or "",
            }
            for r in rows[:8]
        ],
        "_entitlements": [],
    }


def build_health(cases: List[SynthCase], rows: List[Dict[str, Any]], sla_pct: Optional[int]) -> Dict[str, Any]:
    """Mirror of `map_health_from_cases()` — open P1 uses current severity."""
    open_p1 = sum(1 for c in cases if not c.is_closed and c.current == "Sev 1")
    if open_p1:
        status = "At Risk"
    elif len(rows) <= 2:
        status = "Low Activity"
    else:
        status = "Healthy"
    return {
        "composite": None,
        "compositeDelta": None,
        "status": status,
        "adoption": None,
        "adoptionDelta": None,
        "support": sla_pct or 0,
        "supportDelta": None,
        "sentiment": None,
        "sentimentDelta": None,
        "commercial": None,
        "commercialDelta": None,
        "history": [],
    }


# --------------------------------------------------------------------------
# Payload assembly
# --------------------------------------------------------------------------
def quarter_label(as_of: date) -> str:
    q = (as_of.month - 1) // 3 + 1
    fy = as_of.year + 1 if as_of.month >= 7 else as_of.year
    return f"Q{q} FY{str(fy)[-2:]}"


def stamp_iso(as_of: date) -> str:
    """Fixed generation timestamp — never `datetime.now()`, so reruns match."""
    return datetime(as_of.year, as_of.month, as_of.day, 12, 0, tzinfo=timezone.utc).isoformat()


def meta_block(spec: AccountSpec, seed: int, as_of: date, counts: Dict[str, int]) -> Dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "lastUpdated": stamp_iso(as_of),
        "source": "SYNTHETIC FIXTURE — scripts/make-sla-fixtures.py (NOT a Salesforce pull)",
        "synthetic": True,
        "generator": "scripts/make-sla-fixtures.py",
        "seed": seed,
        "asOf": as_of.isoformat(),
        "accountId": "SYNTHETIC-" + spec.slug,
        "warnings": ["Synthetic data — do not present to a customer."] + spec.warnings,
        "counts": counts,
    }


def build_account_payload(spec: AccountSpec, seed: int, as_of: date) -> Dict[str, Any]:
    rng = random.Random(derive_seed(seed, spec.slug))
    cases = synth_cases(spec, rng, as_of)
    rows = [case_to_row(c) for c in cases]
    if spec.edge_rows:
        rows.extend(edge_case_rows())
        rows.sort(key=lambda r: (r.get("created") or "", r.get("caseNumber") or ""))

    themes = theme_labels([c.cause for c in cases], spec.themes_limit)
    math = report_sla_math(rows)
    support = build_support(cases, rows)
    health = build_health(cases, rows, math["pct"])

    open_n = sum(1 for r in rows if r.get("status") == "Open")
    closed_n = len(rows) - open_n
    sev_n = {label: math["slaTotals"][f"P{label[-1]}"] for label in SEV_LABELS}

    notable = []
    for r in rows:
        slasev, _, breach = read_row(r)
        if slasev in ("P1", "P2") and breach is True:
            notable.append(
                {"caseNumber": r["caseNumber"], "text": f"[{r['caseNumber']}] {r.get('subject') or ''}"}
            )
            if len(notable) == 5:
                break

    source_review: Dict[str, Any] = {
        "period": f"past {CASE_MONTHS} months",
        "generated": stamp_iso(as_of),
        "ticketCounts": {"total": len(rows), "open": open_n, "closed": closed_n},
        "executiveSummary": None,
        "overview": (
            f"Synthetic Case history for the past {CASE_MONTHS} months: {len(rows)} tickets "
            f"({open_n} open, {closed_n} closed). "
            f"SLA severity at open: Sev 1={sev_n['Sev 1']}, Sev 2={sev_n['Sev 2']}, "
            f"Sev 3={sev_n['Sev 3']}, Sev 4={sev_n['Sev 4']}."
        ),
        "ticketSummary": {
            "total": len(rows),
            "p1": sev_n["Sev 1"],
            "p2": sev_n["Sev 2"],
            "p3": sev_n["Sev 3"],
            "p4": sev_n["Sev 4"],
        },
        "recurringThemes": themes,
        "notableIncidents": notable,
        "healthTrajectory": None,
        "recommendations": [],
        "contacts": sorted({r["contact"] for r in rows if r.get("contact")}),
        "ticketDetail": rows,
    }

    if spec.narrative:
        # These three fields are read by perf-report.html but the sidecar always
        # emits None/[] — populated here so the render branches get exercised.
        pct_text = "no scored" if math["pct"] is None else f"{math['pct']}%"
        source_review["executiveSummary"] = (
            f"{spec.name} closed {closed_n} of {len(rows)} cases in the window with "
            f"{pct_text} P1/P2 first-response adherence. Volume is trending down."
        )
        source_review["healthTrajectory"] = (
            "Improving — case volume and breach count both fell across the last three quarters."
        )
        source_review["recommendations"] = [
            "Keep the quarterly upgrade cadence; it is the largest single cause code.",
            "Move certificate rotation onto the managed lifecycle job.",
        ]

    products = ["Mirantis Kubernetes Engine", "Mirantis Secure Registry"] if spec.clusters else []
    product_mix = [
        {
            "product": p,
            "entitlement": f"{spec.nodes} nodes across envs" if spec.nodes else "From Environment__c",
            "inUse": str(spec.nodes) if spec.nodes else "",
            "utilizationPct": 0,
            "trend": "— synthetic fixture",
        }
        for p in products
    ]

    counts = {
        "environments": spec.clusters,
        "licenses": len(products),
        "cases": len(rows),
        "caseMilestones": sum(1 for r in rows if r.get("slaMilestone")),
        "entitlements": 1 if spec.clusters else 0,
        "contacts": len(source_review["contacts"]),
        "opportunitiesOpen": 0,
    }

    payload: Dict[str, Any] = {
        "_meta": meta_block(spec, seed, as_of, counts),
        "customer": {
            "name": spec.name,
            "tier": spec.tier,
            "industry": spec.industry,
            "stakeholders": [
                {"name": name, "title": "Platform Engineering"}
                for name in source_review["contacts"][: spec.stakeholders]
            ],
        },
        "quarter": quarter_label(as_of),
        "preparedBy": "Synthetic Fixture Generator",
        "preparedByEmail": "",
        "presentationDate": as_of.isoformat(),
        "nextQbr": {"label": "", "date": ""},
        "commercial": {
            "arr": {"current": spec.arr, "prior": spec.arr, "yoyPct": 0},
            "contractValue": spec.arr * 3 if spec.arr else None,
            "pipelineUSD": 0,
            "renewalDate": date(as_of.year + 1, 3, 31).isoformat() if spec.arr else "",
            "renewalSponsor": "",
            "expansions": [],
            "_recentClosed": [],
        },
        "usage": {
            "clusters": spec.clusters,
            "clustersDelta": 0,
            "nodes": spec.nodes,
            "nodesDelta": 0,
            "workloads": 0,
            "workloadsDelta": 0,
            "environments": spec.clusters,
            "uptime": 0,
            "_environmentDetail": [],
            "_nodeBreakdown": {
                "compute": spec.nodes,
                "controllers": spec.clusters * 3,
                "monitoring": 0,
                "storage": 0,
                "other": 0,
                "telemetry": 0,
                "of_nodes": spec.nodes,
            },
        },
        "support": support,
        "health": health,
        "nps": {"score": 0, "industry": 30, "delta": 0},
        "products": products,
        "productMix": product_mix,
        "sourceReview": source_review,
        "incidents": [
            {
                "id": r["caseNumber"],
                "date": r.get("created") or "",
                "severity": "P1",
                "summary": r.get("subject") or "P1 case",
                "duration": r.get("duration") or "",
                "rcaStatus": "CLOSED" if r.get("status") == "Closed" else "OPEN",
                "action": r.get("resolution") or "",
            }
            for r in rows
            if r.get("openedAs") == "Sev 1"
        ][:6],
        "incidentsPattern": "",
        "wins": [],
        "risks": [],
        "mirantisRoadmap": [],
        "customerRoadmap": [],
        "training": {"delivered": [], "planned": [], "deliveredNote": "", "plannedNote": ""},
        "execSummaryTakeaways": [
            {
                "kind": "SUPPORT",
                "label": "01 · SUPPORT",
                "headline": (
                    f"{len(rows)} cases · {sev_n['Sev 1']} P1 · "
                    f"{'—' if math['pct'] is None else str(math['pct']) + '%'} first response"
                ),
                "body": f"Open cases: {open_n}. P1 MTTR (closed): {support['p1MttrHours']}h.",
            }
        ],
        "asks": {"fromUs": [], "fromYou": []},
        "nextActions": [],
        "previousActions": [],
        "sections": {
            k: True
            for k in (
                "execSummary", "accountHealth", "goalsRecap", "usage", "supportDeepDive",
                "incidents", "wins", "risks", "mirantisRoadmap", "customerRoadmap",
                "renewal", "training", "asks", "nextQuarter", "asksTracker", "appendix",
            )
        },
    }
    return payload


def build_sparse_payload(seed: int, as_of: date) -> Dict[str, Any]:
    """Defensive-path fixture: empty ticketDetail, missing keys, nulls.

    `customer.name` is the one field kept, because `computePortfolio()` reads
    `d.customer.name` without a guard and a missing value throws for the whole
    portfolio, not just this row.
    """
    spec = SPARSE_SPEC
    return {
        "_meta": meta_block(spec, seed, as_of, {"cases": 0}),
        "customer": {"name": spec.name, "stakeholders": []},
        "quarter": quarter_label(as_of),
        "presentationDate": as_of.isoformat(),
        "sourceReview": {
            "period": None,
            "ticketCounts": None,
            "executiveSummary": None,
            "overview": None,
            "recurringThemes": None,
            "notableIncidents": [],
            "healthTrajectory": None,
            "recommendations": None,
            "contacts": [],
            "ticketDetail": [],
        },
        "products": [],
        "productMix": [],
        "sections": {},
    }


# --------------------------------------------------------------------------
# Self-checks
# --------------------------------------------------------------------------
def validate_payload(payload: Dict[str, Any], label: str) -> None:
    rows = (payload.get("sourceReview") or {}).get("ticketDetail") or []
    for row in rows:
        duration = row.get("duration")
        if duration not in ("", "Open", None) and parse_duration_hours(duration) is None:
            raise SystemExit(
                f"{label}: duration {duration!r} on case {row.get('caseNumber')!r} is "
                "unparseable by perf-report.html parseDurationHours()"
            )
        if row.get("slaBreach") not in (None, True, False):
            raise SystemExit(f"{label}: slaBreach must be true/false/null, got {row.get('slaBreach')!r}")

    math = report_sla_math(rows)
    parsed = [read_row(row) for row in rows]

    scored = sum(1 for _, bound, breach in parsed if bound and breach is not None)
    if math["scored"] != scored:
        raise SystemExit(f"{label}: scored/bound accounting is inconsistent")

    # The met percentage must be computed over P1/P2 only. Rows that declare
    # slaBound false while still carrying a breach flag (Sev 3/4 noise from the
    # source system) must be discarded, not counted as passes.
    for row, (_, bound, breach) in zip(rows, parsed):
        if not bound and row.get("slaBreach") is not None and breach is not None:
            raise SystemExit(
                f"{label}: unbound case {row.get('caseNumber')!r} kept its breach verdict"
            )
    if math["bySev"]["P1"]["total"] + math["bySev"]["P2"]["total"] != math["scored"]:
        raise SystemExit(
            f"{label}: P3/P4 rows leaked into the scored denominator "
            "(SLA is P1/P2 first response only)"
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def write_json(path: Path, out_dir: Path, payload: Dict[str, Any], dry_run: bool) -> int:
    assert_demo_target(path, out_dir, "write")
    body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    if not dry_run:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    return len(body.encode("utf-8"))


def clean(out_dir: Path, dry_run: bool) -> List[Path]:
    removed: List[Path] = []
    for path in sorted(out_dir.glob(f"{DEMO_PREFIX}*.json")):
        assert_demo_target(path, out_dir, "delete")
        if not dry_run:
            path.unlink()
        removed.append(path)
    return removed


def parse_args(argv: List[str]) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(
        description="Write deterministic synthetic demo-*.json fixtures for perf-report.html.",
    )
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"RNG seed (default {DEFAULT_SEED})")
    p.add_argument(
        "--as-of",
        default=DEFAULT_AS_OF,
        help=f"End of the trailing {CASE_MONTHS}-month window, YYYY-MM-DD "
             f"(default {DEFAULT_AS_OF}; pinned so reruns are byte-identical)",
    )
    p.add_argument("--out-dir", default=str(repo_root / "accounts"), help="Accounts directory")
    p.add_argument(
        "--clean",
        action="store_true",
        help=f"Delete {DEMO_PREFIX}*.json from the accounts directory and exit without generating",
    )
    p.add_argument("--dry-run", action="store_true", help="Report what would change without touching disk")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        raise SystemExit(f"--as-of must be YYYY-MM-DD, got {args.as_of!r}")

    if args.clean:
        if not out_dir.is_dir():
            print(f"nothing to clean: {out_dir} does not exist")
            return 0
        removed = clean(out_dir, args.dry_run)
        verb = "would remove" if args.dry_run else "removed"
        for path in removed:
            print(f"{verb} {path.name}")
        kept = sorted(
            p.name for p in out_dir.glob("*.json") if not p.name.startswith(DEMO_PREFIX)
        )
        print(f"{verb} {len(removed)} file(s); left {len(kept)} non-{DEMO_PREFIX} file(s) untouched")
        return 0

    if not out_dir.is_dir():
        if args.dry_run:
            print(f"would create {out_dir}")
        else:
            out_dir.mkdir(parents=True, exist_ok=True)

    print(f"seed={args.seed} as-of={as_of.isoformat()} out-dir={out_dir}")
    total_rows = 0
    for spec in ACCOUNT_SPECS:
        payload = build_account_payload(spec, args.seed, as_of)
        validate_payload(payload, spec.slug)
        rows = payload["sourceReview"]["ticketDetail"]
        math = report_sla_math(rows)
        targets = [out_dir / f"{spec.slug}.json"]
        if spec.write_quarter_twin:
            # The sidecar writes both {slug}.json and {slug}-{quarter}.json; the
            # report de-duplicates them by customer name.
            targets.append(out_dir / f"{spec.slug}-{quarter_label(as_of).lower().replace(' ', '-')}.json")
        for target in targets:
            size = write_json(target, out_dir, payload, args.dry_run)
            print(
                f"  {target.name}: {len(rows)} cases, "
                f"P1={math['slaTotals']['P1']} P2={math['slaTotals']['P2']} "
                f"P3={math['slaTotals']['P3']} P4={math['slaTotals']['P4']}, "
                f"scored={math['scored']} met={math['met']} breached={math['breached']} "
                f"pct={math['pct']}, health={payload['health']['status']}, {size} bytes"
            )
        total_rows += len(rows)

    sparse = build_sparse_payload(args.seed, as_of)
    validate_payload(sparse, SPARSE_SPEC.slug)
    sparse_path = out_dir / f"{SPARSE_SPEC.slug}.json"
    size = write_json(sparse_path, out_dir, sparse, args.dry_run)
    print(f"  {sparse_path.name}: 0 cases (defensive-path fixture), {size} bytes")

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {len(ACCOUNT_SPECS) + 1} accounts / {total_rows} synthetic cases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
