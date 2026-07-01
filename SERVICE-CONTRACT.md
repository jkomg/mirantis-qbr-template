# QBR Data — Service Contract

A spec for **Architecture C** (live service). Hand this to whoever builds the internal data API.

## Goal

The QBR Template deck loads its data from a stable URL keyed by account ID. TAMs don't paste, sync, or manage files — they pick the account in the Tweaks panel and the deck is current.

---

## Endpoint

```
GET https://qbr-data.example-internal/v1/accounts/{accountId}/{quarter}.json

Examples:
  GET /v1/accounts/0014x000123abc/Q3-FY26.json     // specific quarter
  GET /v1/accounts/0014x000123abc/latest.json      // most recent QBR
  GET /v1/accounts/0014x000123abc/preview.json     // live SF snapshot
```

**Headers**: `Authorization: Bearer <org-sso-token>` — locked to your corporate email domain (e.g. `@example.com`). CORS allowed for the deck's hosted origin only.

**Cache**: `Cache-Control: max-age=300` on `latest.json` and per-quarter files; `no-cache` on `preview.json`.

---

## Response shape

Use exactly the schema in `qbr.data.json`. Fields the service can't fill must be `null` or empty arrays — never invented. The deck has fallbacks for every field.

```jsonc
{
  "_meta": {
    "schemaVersion": "qbr-2026.06",   // required — deck rejects unknown major versions
    "lastUpdated": "2026-06-24T09:14:00Z",
    "source": "qbr-data-service v0.4"
  },
  "customer": { "name": "...", "tier": "Strategic", "industry": "...", "stakeholders": [...] },
  "quarter": "Q3 FY26",
  "preparedBy": "...",
  "preparedByEmail": "...",
  "presentationDate": "2026-06-24",
  "nextQbr": { "label": "...", "date": "2026-09-25" },
  "commercial": { "arr": {...}, "renewalDate": "...", "expansions": [...] },
  "usage": { "clusters": 14, "nodes": 312, "workloads": 1847, ... },
  "support": { "p1Count": 4, "slaMetPct": 96, "p1MttrHours": 3.4, "csat": 4.6, ... },
  "nps": { "score": 42, "industry": 30, "delta": 7 },
  "products": [...],
  "productMix": [...],

  // Manual / TAM-curated — service should preserve verbatim from the last edit
  "incidents": [...],
  "wins": [...],
  "risks": [...],
  "mirantisRoadmap": [...],
  "customerRoadmap": [...],
  "training": {...},
  "execSummaryTakeaways": [...],
  "asks": { "fromUs": [...], "fromYou": [...] },
  "nextActions": [...],
  "previousActions": [...]
}
```

---

## Data sources to wire

| Field group | Source |
| --- | --- |
| `customer.*`, `commercial.*`, `preparedBy*`, `products[]`, `productMix[].entitlement` | **Salesforce** (Account + Opportunity + Asset) |
| `usage.*` | **Datadog / Grafana** (cluster, node, workload counts by customer tag) |
| `support.*` | **Zendesk** or **Salesforce Service Cloud** (tickets, SLA, MTTR, CSAT) |
| `nps.*` | **Delighted / Wootric** (or wherever NPS lives) |
| `health.*` | Computed from the four sources above (Mirantis-internal scoring) |
| `incidents[]` | **PagerDuty** + RCA tracker — manual close-out by TAM |
| `wins[]`, `risks[]`, `execSummaryTakeaways[]`, `asks.*`, `*Actions[]` | **Manual** — TAM curated, persisted by the service |
| `mirantisRoadmap[]` | **Product Management** — single shared roadmap doc, queryable by quarter |
| `customerRoadmap[]`, `training.*`, `nextQbr.*` | **Manual** — TAM curated |

---

## Refresh strategy

- **Live fields** (telemetry, SF Opportunity stages) — refreshed every 5 minutes
- **Quarterly snapshot** — on QBR day, service writes `Q3-FY26.json` from the current state and freezes it. `latest.json` updates per-quarter.
- **Manual fields** — TAM PUTs partial updates: `PUT /v1/accounts/{accountId}/draft.json` with a JSON Patch. Service merges into the live document.

---

## Client integration

The deck takes a `dataFile` tweak. The service-hosted version of the deck sets it to:

```html
<x-import name="QBR Template" data-file="https://qbr-data.example-internal/v1/accounts/{{ accountId }}/latest.json">
```

For the TAM-pasted path (Architecture A), the Configurator's "Import from data source" panel accepts any of these:

- A raw SF Report JSON row
- A Datadog metric query result
- The full `qbr.data.json` document (paste-in from this service)

---

## Open questions for RevOps + IT

1. Who owns the Connected App in Salesforce? OAuth flow?
2. Service hosted where — internal AWS, Mirantis IT-managed?
3. SSO + MFA requirements for TAM access — Okta?
4. Retention — keep all historical quarter snapshots forever?
5. Multi-region — does the EU TAM need EU-hosted data?
6. Audit logging — who looked at which customer's data when?

---

## What this replaces

| Today | After service |
| --- | --- |
| TAM opens Configurator, types numbers | TAM picks account in Tweaks, data is already there |
| TAM pastes SF export JSON | Service did the pull |
| TAM downloads `qbr.data.json`, commits to repo | Service stores snapshots; URL is the source of truth |
| Each TAM keeps their own version | Single source — Mirantis-wide |
