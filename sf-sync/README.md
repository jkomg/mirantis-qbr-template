# Salesforce Sync Sidecar

Pulls Salesforce account data directly into the QBR Template's JSON schema. Runs as a second container alongside the static deck.

## What it does

When the TAM clicks **Pull from Salesforce** in the Configurator, the browser POSTs to this sidecar at `http://localhost:8081/pull` with `{account, quarter}`. The sidecar:

1. Authenticates to SF using the **logged-in user's OAuth session** (or password creds for CLI)
2. Runs five SOQL queries: Account, open Opportunities, recent closed Opportunities, Assets, Contacts
3. Maps the results into the `qbr.data.json` schema (preserving the TAM's existing narrative)
4. Writes the result to `/data/accounts/{slug}-{quarter}.json`
5. Returns the payload to the Configurator, which merges it into the form

The TAM still fills in usage telemetry, support metrics, incidents, wins, risks, asks, training, and roadmaps — none of which Salesforce has.

## Setup — OAuth (recommended)

Each TAM uses their **own** Salesforce login. One Connected App serves the whole team.

**Full walkthrough (IT + TAM):** [`SALESFORCE-OAUTH-SETUP.md`](../SALESFORCE-OAUTH-SETUP.md) in the repo root — includes where to click in Salesforce to get the Consumer Key and Secret.

### Quick summary

In Salesforce Setup → App Manager → New Connected App:

| Setting | Value |
|---------|-------|
| Callback URL | `http://localhost:8081/oauth/callback` |
| OAuth scopes | `api`, `refresh_token` |
| Permitted users | Admin-approved or self-authorize (per org policy) |

Copy the **Consumer Key** and **Consumer Secret**.

### 2. Configure `.env`

Copy `.env.example` to `.env` at the project root:

```
SF_CONSUMER_KEY=<from Connected App>
SF_CONSUMER_SECRET=<from Connected App>
SF_DOMAIN=login          # or 'test' for sandbox
FLASK_SECRET_KEY=<long random string>
```

Password credentials are **not** required for the web UI.

### 3. Run

```bash
docker compose up --build
```

Open the Configurator → **Connect to Salesforce** → log in with your SF identity → **Pull from Salesforce**.

Tokens stay in a signed browser session cookie on localhost. They are not shared between TAMs or written to disk.

## Setup — password flow (CLI / legacy)

For batch CLI pulls or orgs that haven't approved OAuth yet:

```
SF_AUTH_MODE=password
SF_USERNAME=service-account@example.com
SF_PASSWORD=<password>
SF_SECURITY_TOKEN=<from SF Setup → Reset Security Token>
SF_DOMAIN=login
```

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Sidecar status, auth mode, connected user |
| GET | `/oauth/login` | Start Salesforce login (browser redirect) |
| GET | `/oauth/callback` | OAuth redirect target (internal) |
| GET | `/oauth/status` | Current session connection |
| POST | `/oauth/logout` | Clear session tokens |
| POST | `/pull` | Pull account data (requires auth) |

## CLI mode

The same image exposes a CLI for batch use (password auth only):

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

## Auth modes

| Mode | Env | Who logs in | Use case |
|------|-----|-------------|----------|
| **OAuth** (default when keys set) | `SF_CONSUMER_KEY` + `SF_CONSUMER_SECRET` | Each TAM in browser | Multi-user laptops |
| **Password** | `SF_USERNAME` + `SF_PASSWORD` + `SF_SECURITY_TOKEN` | Shared service account | CLI, legacy |

Set `SF_AUTH_MODE=auto` (default), `oauth`, or `password` to force a mode.

## Troubleshooting

**"Account not found in Salesforce: 'Vertex Logistics'"** — SOQL is case-sensitive. Try the exact name from the SF UI. With OAuth, you only see accounts your user can access.

**"Not connected to Salesforce"** — click **Connect to Salesforce** in the Configurator first.

**"Invalid OAuth state"** — cookies blocked or `FLASK_SECRET_KEY` changed mid-session. Reconnect.

**"OAuth is not configured"** — `SF_CONSUMER_KEY` / `SF_CONSUMER_SECRET` missing from `.env`. Rebuild after editing.

**"INVALID_FIELD: No such column 'Tier__c'"** — the field doesn't exist in your org. Edit the SOQL in `sync.py`.

**Sidecar boots but `/pull` returns 502** — check `docker compose logs sf-sync` for the underlying `SalesforceError`.

**The Configurator's "Check Sidecar" returns unreachable** — the sidecar might still be starting (~3s). Wait, then retry. If still unreachable, `docker compose ps` should show both `qbr` and `sf-sync` healthy.

**CORS / cookies** — the Configurator must be served from `http://localhost:8080` (not `file://`) so session cookies work with the sidecar.
