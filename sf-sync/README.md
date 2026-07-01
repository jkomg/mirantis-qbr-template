# Salesforce Sync Sidecar

Pulls Salesforce account data directly into the QBR Template's JSON schema. Runs as a second container alongside the static deck.

## What it does

When the TAM clicks **Pull from Salesforce** in the Configurator, the browser POSTs to this sidecar at `http://localhost:8081/pull` with `{account, quarter}`. The sidecar:

1. Authenticates to SF using `simple-salesforce` (password + security token)
2. Runs five SOQL queries: Account, open Opportunities, recent closed Opportunities, Assets, Contacts
3. Maps the results into the `qbr.data.json` schema (preserving the TAM's existing narrative)
4. Writes the result to `/data/accounts/{slug}-{quarter}.json`
5. Returns the payload to the Configurator, which merges it into the form

The TAM still fills in usage telemetry, support metrics, incidents, wins, risks, asks, training, and roadmaps — none of which Salesforce has.

## Setup

Copy `.env.example` to `.env` at the project root and fill in:

```
SF_USERNAME=service-account@example.com
SF_PASSWORD=<service account password>
SF_SECURITY_TOKEN=<from SF Setup → Reset Security Token>
SF_DOMAIN=login          # or 'test' for a sandbox
```

Then:

```bash
docker compose up --build
```

The sidecar boots on port 8081. The Configurator's **Check Sidecar** button verifies the connection without making any SF calls.

## CLI mode

The same image exposes a CLI for batch use. Once the stack is up:

```bash
docker compose exec sf-sync python sync.py \
  --account "Vertex Logistics" --quarter "Q3 FY26"
# → ✓ Wrote /data/accounts/vertex-logistics-q3-fy26.json

docker compose exec sf-sync python sync.py \
  --account "Vertex Logistics" --stdout > /tmp/vertex.json
```

Useful for: pre-meeting CI, cron-style nightly refresh, scripting bulk pulls across many accounts.

## SOQL queries you'll want to tune

The queries in `sync.py` use standard SF field names. Mirantis SF will have custom fields the queries don't reference yet. Common edits per-org:

- `Account.AnnualRevenue` is rarely the source of truth for ARR — most orgs use a `ARR__c` custom field. Edit `SOQL_ACCOUNT`.
- Renewal date probably lives on a `Renewal_Date__c` custom field. Add it to the SELECT and map it in `build_payload`.
- Tier values from SF picklists (`Tier__c = "Strategic Tier 1"`) need normalizing to what the deck expects (`Strategic`, `Enterprise`, `Growth`).
- Health score / churn risk if you have custom fields for them.

## Auth flow

v1 uses password + security token (simplest, works on TAM laptops). Mirantis security teams sometimes block this for service accounts — if so, switch to OAuth JWT bearer by:

1. Creating a Connected App in SF with a cert
2. Setting `SF_CONSUMER_KEY` and mounting the private key file
3. Swapping `simple_salesforce.Salesforce(...)` for the JWT bearer flow

That's a v2 conversation.

## Troubleshooting

**"Account not found in Salesforce: 'Vertex Logistics'"** — SOQL is case-sensitive. Try the exact name from the SF UI.

**"INVALID_FIELD: No such column 'Tier__c'"** — the field doesn't exist (or has a different name) in your org. Edit the SOQL in `sync.py`.

**"INVALID_LOGIN: Invalid username, password, security token"** — most likely the security token. Reset it (Setup → My Personal Information → Reset Security Token) and update `.env`.

**Sidecar boots but `/pull` returns 502** — check `docker compose logs sf-sync` for the underlying `SalesforceError`.

**The Configurator's "Check Sidecar" returns unreachable** — the sidecar might still be starting (it takes ~3s). Wait, then retry. If still unreachable, `docker compose ps` should show both `qbr` and `sf-sync` healthy.
