# QBR Data Automation — three paths

The HTML deck cannot call Salesforce directly (CORS, OAuth, IP allowlist). So "automation" is a question of **who fetches the data** and **how it reaches the deck**. Three paths, in increasing engineering cost:

## A · TAM-pasted import — SHIPPED

**No infra. Works today.**

In the QBR Configurator, an "Import from data source" panel accepts:

- A Salesforce Report JSON row
- A flat object with named fields (e.g. `{ "ARR": 3500000, "RenewalDate": "..." }`)
- A full `qbr.data.json` payload (from this service later, or hand-written)

Mapped fields overwrite the form; everything else stays as the TAM typed it. Recognized field names:

```
Account.Name, Account.Tier__c, Account.Industry, Account.ARR__c, Account.ARR_Prior__c,
Account.Renewal_Date__c, Owner.Name, Owner.Email, P1_Count__c, SLA_Met__c, CSAT__c, NPS__c,
telemetry.clusters, telemetry.nodes, telemetry.workloads, Quarter
```

**How a TAM uses it:**
1. In Salesforce, run a Report on the account, click `Export → JSON`
2. Open QBR Configurator
3. Paste into the Import panel, click "Map & merge into form"
4. Fill in the narrative pieces (wins, risks, asks) manually
5. Save draft → Open deck

---

## B · CLI tool — SCAFFOLD AT `scripts/mirantis-qbr-sync.js`

**One-time engineering project (~1–2 days for an engineer with SF API experience).**

A small Node script with Salesforce + Zendesk + Datadog credentials, run as:

```bash
node scripts/mirantis-qbr-sync.js --account "Vertex Logistics" --quarter "Q3 FY26"
# ✓ Wrote ./accounts/vertex-logistics-q3-fy26.json
```

TAM points the deck's `dataFile` tweak at the generated file. Could run on a TAM's laptop, in CI, or on a cron.

The scaffold has all the SOQL queries blocked out, helper functions for ARR / quarter math, and clear `TODO` markers where the real API calls go. Read the file — it's annotated for the engineer who'll implement it.

**Who to hand it to:** RevOps engineer or anyone with SF API access. They need to:
1. Get a Salesforce Connected App registered (talk to Mirantis IT)
2. Implement the `sfLogin()` and `sfQuery()` functions (~50 lines with `jsforce`)
3. Add Zendesk / Datadog API calls for support + telemetry
4. Optionally: schedule via cron or GitHub Actions

---

## C · Live web service — SPEC AT `SERVICE-CONTRACT.md`

**Multi-week project. Highest UX.** The deck fetches per-account JSON from a stable URL; TAMs don't sync or paste anything. Read the contract — it documents the endpoints, schema, data sources to wire, and open questions for IT.

This is what you build once the deck pattern has proven out across enough QBRs that the manual workflow becomes the bottleneck.

---

## D · iPaaS (Workato / Zapier) — alternative

If Mirantis already has Workato or similar licensed, an integration there can write `qbr.data.json` to a shared Google Drive folder. The deck reads via the file picker or a stable Drive URL. Code-light path. Talk to whoever owns the iPaaS tool internally.

---

## Recommendation

**Ship A today. Build B in parallel.** A unblocks TAMs immediately; B becomes the standard once your engineering team builds it. C is the long-term destination but only after A/B prove the data shape is right.

The schema (`qbr.data.json`) is the contract. Whatever path you take, the deck doesn't change.
