# Salesforce Sync Sidecar

Pulls Salesforce account data directly into the QBR Template's JSON schema. Runs as a second container alongside the static deck.

## What it does

When the TAM clicks **Pull from Salesforce** in the Configurator, the browser POSTs to this sidecar at `http://localhost:8081/pull` with `{account, quarter}`. The sidecar:

1. Authenticates to SF (Client Credentials / OAuth / password)
2. Runs Mirantis-aware queries: Account, Opportunities, Contacts, **Environment__c**, **License__c**, **Case** (`Severity_Level__c`), **CaseHistory**, **CaseMilestone**, **Entitlement**
3. Maps footprint, support, products, incidents, risks, and `sourceReview.ticketDetail` (SLA Performance Report) into `qbr.data.json`
4. Writes `/data/accounts/{slug}-{quarter}.json` and `/data/accounts/{slug}.json` (shared `./accounts` bind mount)
5. Returns the payload to the Configurator

Standard Salesforce **Asset** is not used (unavailable in the MKE Ops sandbox). Product mix comes from **License__c** + **Environment__c**.

The TAM still curates wins, roadmap narrative, NPS, and asks — Salesforce does not own those.

## Optional — AI account status review

The Configurator can call `POST /review` for color commentary (strengths, watch items, suggested asks/takeaways).

Add one key to `.env`:

```
OPENAI_API_KEY=sk-...
# or
ANTHROPIC_API_KEY=sk-ant-...
```

Rebuild/restart `sf-sync` so the key is loaded. `/health` reports `reviewAvailable: true` when configured. Commentary is a TAM draft — review before presenting.

## Setup — Client Credentials (Mirantis sandbox)

IT typically provisions **OAuth 2.0 Client Credentials**. No browser login.

```
SF_AUTH_MODE=client_credentials
SF_CONSUMER_KEY=<from Connected App>
SF_CONSUMER_SECRET=<from Connected App>
SF_DOMAIN=mirantis--mkeops.sandbox.my   # My Domain — NOT login/test
```

Full walkthrough: [`SALESFORCE-OAUTH-SETUP.md`](../SALESFORCE-OAUTH-SETUP.md).

Then `docker compose up --build` → Configurator → enter Account name → **Pull**.

## Setup — OAuth Authorization Code (per-user browser login)

Each TAM uses their **own** Salesforce login. One Connected App serves the whole team.

**Full walkthrough:** [`SALESFORCE-OAUTH-SETUP.md`](../SALESFORCE-OAUTH-SETUP.md).

### Quick summary

In Salesforce Setup → App Manager → New Connected App:

| Setting | Value |
|---------|-------|
| Callback URL | `http://localhost:8081/oauth/callback` |
| OAuth scopes | `api`, `refresh_token` |
| Permitted users | Admin-approved or self-authorize (per org policy) |

Copy the **Consumer Key** and **Consumer Secret**.

### Configure `.env` (browser OAuth)

Copy `.env.example` to `.env` at the project root:

```
SF_AUTH_MODE=oauth
SF_CONSUMER_KEY=<from Connected App>
SF_CONSUMER_SECRET=<from Connected App>
SF_DOMAIN=test
FLASK_SECRET_KEY=<long random string>
```

Password credentials are **not** required for the web UI.

### Run

```
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
| GET | `/accounts` | Search Accounts (`?q=` fragment, blank = recent) |
| GET | `/inspect` | Raw counts/samples for one account (`?account=Exact Name`) |
| GET | `/scan` | Rank accounts by usable data (`?limit=25`, optional `?q=`) |
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

- Account commercial SoR: `ARR__c`, `Total_Won_Amount__c`, `Open_Pipeline__c`, `Upcoming_renewal_date__c` (fallback `Renewal_Open_Opportunity_Start_Date__c`, then License end date)
- Environment node SoR: `of_nodes__c` (component node fields as fallback)
- Standard Salesforce **Asset** is not used (unavailable in the MKE Ops sandbox; footprint from **License__c** + **Environment__c**)
- Renewal date probably lives on a `Renewal_Date__c` custom field. Add it to the SELECT and map it in `build_payload`.
- Tier values from SF picklists (`Tier__c = "Strategic Tier 1"`) need normalizing to what the deck expects (`Strategic`, `Enterprise`, `Growth`).
- Health score / churn risk if you have custom fields for them.

**Missing objects:** some sandboxes don't enable standard `Asset`. The pull now skips unavailable objects (Assets, Contacts, Opportunities) and continues with a warning in `_meta.warnings` instead of failing.

## Auth modes

| Mode | Env | Who logs in | Use case |
|------|-----|-------------|----------|
| **Client Credentials** | Key + Secret + My Domain | Integration user on Connected App | Sandbox / server-to-server |
| **OAuth (auth code)** | Key + Secret | Each TAM in browser | Multi-user laptops |
| **Password** | Username + password + token | Shared / personal user | CLI, legacy |

Set `SF_AUTH_MODE=auto` (default), `client_credentials`, `oauth`, or `password`.

## Troubleshooting

**"Account not found in Salesforce: 'Vertex Logistics'"** — SOQL is case-sensitive. Try the exact name from the SF UI. With OAuth, you only see accounts your user can access.

**"Not connected to Salesforce"** — click **Connect to Salesforce** in the Configurator first.

**"Invalid OAuth state"** — cookies blocked or `FLASK_SECRET_KEY` changed mid-session. Reconnect.

**"OAuth is not configured"** — `SF_CONSUMER_KEY` / `SF_CONSUMER_SECRET` missing from `.env`. Rebuild after editing.

**"INVALID_FIELD: No such column 'Tier__c'"** — the field doesn't exist in your org. Edit the SOQL in `sync.py`.

**Sidecar boots but `/pull` returns 502** — check `docker compose logs sf-sync` for the underlying `SalesforceError`.

**The Configurator's "Check Sidecar" returns unreachable** — the sidecar might still be starting (~3s). Wait, then retry. If still unreachable, `docker compose ps` should show both `qbr` and `sf-sync` healthy.

**CORS / cookies** — the Configurator must be served from `http://localhost:8080` (not `file://`) so session cookies work with the sidecar.
