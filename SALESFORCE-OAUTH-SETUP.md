# Salesforce OAuth Setup — QBR Template

Connect the QBR Configurator to Salesforce so **each TAM logs in with their own account**. Pulls run with that person's permissions — not a shared service password.

**Audience:** TAMs setting up Docker locally, and IT/RevOps creating the one-time Connected App.

---

## How it works

| Who | What they do |
|-----|----------------|
| **IT / RevOps (once)** | Create a Salesforce Connected App; share Consumer Key + Secret with the team |
| **Each TAM** | Copy `.env.example` → `.env`, paste the keys, run `docker compose up`, click **Connect to Salesforce** |

The sidecar (`sf-sync` on port 8081) holds each user's OAuth tokens in a **signed browser session cookie** on their laptop. Tokens are not shared between TAMs and are not committed to git.

---

## Part 1 — IT: Create the Connected App (one-time)

You need a Salesforce admin (or someone with **Manage Connected Apps** permission).

### Where to go in Salesforce

1. Log in to Salesforce (production or sandbox).
2. Click the **gear** icon (top right) → **Setup**.
3. In **Quick Find**, type **App Manager** → open **App Manager**.
4. Click **New Connected App** (top right).

### Connected App settings

Fill in **Basic Information**:

| Field | Example |
|-------|---------|
| Connected App Name | `Mirantis QBR Template` |
| API Name | auto-filled |
| Contact Email | your team's admin email |

Under **API (Enable OAuth Settings)**:

| Setting | Value |
|---------|-------|
| **Enable OAuth Settings** | ✓ checked |
| **Callback URL** | `http://localhost:8081/oauth/callback` |
| **Selected OAuth Scopes** | Move these to **Selected OAuth Scopes**: |
| | • **Manage user data via APIs (`api`)** |
| | • **Perform requests at any time (`refresh_token`, `offline_access`)** |

Click **Save**. Salesforce may warn that the app can take **2–10 minutes** to become active — wait before testing.

### Get the Consumer Key and Consumer Secret

1. Back in **App Manager**, find your app → click the dropdown on the right → **View**.
2. In the **API (Enable OAuth Settings)** section, click **Manage Consumer Details**.
3. Salesforce will ask you to verify (email code or MFA).
4. Copy:
   - **Consumer Key** → goes in `.env` as `SF_CONSUMER_KEY`
   - **Consumer Secret** → goes in `.env` as `SF_CONSUMER_SECRET`

Store the secret in your team's password manager. **Do not commit it to git.**

### Who can use the app?

Still on the Connected App page → **Manage** → **Edit Policies** (or **Manage Connected App** → **Edit Policies**):

| Policy | When to use |
|--------|-------------|
| **All users may self-authorize** | Small team, fastest to roll out |
| **Admin approved users are pre-authorized** | Enterprise default — then assign users via **Permission Sets** on the Connected App |

Users must have Salesforce access to Accounts, Opportunities, Assets, and Contacts (standard read is enough for the default queries).

### Sandbox vs production

| Org | `SF_DOMAIN` in `.env` | Login URL |
|-----|----------------------|-----------|
| Production | `login` | `https://login.salesforce.com` |
| Sandbox | `test` | `https://test.salesforce.com` |

Create the Connected App **in the same org** TAMs will pull from. Sandbox keys do not work against production.

---

## Part 2 — TAM: Configure your machine

### Prerequisites

- Docker Desktop (or Docker Engine + Compose)
- This repo cloned locally
- Consumer Key + Secret from IT (Part 1)

### 1. Create `.env`

From the project root:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
SF_CONSUMER_KEY=3MVG9...your_consumer_key...
SF_CONSUMER_SECRET=...your_consumer_secret...
SF_DOMAIN=login
FLASK_SECRET_KEY=pick-a-long-random-string-here
```

Generate a random `FLASK_SECRET_KEY` (any long random string). It signs the OAuth session cookie on your machine.

**You do not need** `SF_USERNAME`, `SF_PASSWORD`, or `SF_SECURITY_TOKEN` for the web UI.

### 2. Start the stack

```bash
docker compose up --build
```

Open:

- **Configurator:** http://localhost:8080/QBR%20Configurator.dc.html
- **Sidecar health:** http://localhost:8081/health

### 3. Connect and pull

1. In the Configurator, scroll to **// PULL FROM SALESFORCE**.
2. Click **Connect to Salesforce**.
3. Log in with **your** Salesforce username (SSO/MFA works — Salesforce handles login).
4. Approve access if prompted.
5. You return to the Configurator; status should show **CONNECTED** and your username.
6. Enter the **exact** Salesforce Account name → **↓ Pull from Salesforce**.

Usage, support metrics, wins, risks, and asks still need manual input — Salesforce only fills account/commercial/product fields.

### 4. Disconnect

Click **Disconnect** in the Configurator to clear your session on this machine.

---

## Quick reference — `.env` variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SF_CONSUMER_KEY` | Yes (OAuth) | Connected App Consumer Key |
| `SF_CONSUMER_SECRET` | Yes (OAuth) | Connected App Consumer Secret |
| `SF_DOMAIN` | No | `login` (prod) or `test` (sandbox). Default: `login` |
| `SF_REDIRECT_URI` | No | Default: `http://localhost:8081/oauth/callback` — must match Connected App |
| `FLASK_SECRET_KEY` | Recommended | Random string; signs session cookies |
| `SF_AUTH_MODE` | No | `auto` (default), `oauth`, or `password` |
| `CONFIGURATOR_URL` | No | Where OAuth redirects after login. Default: Configurator on :8080 |

Password flow (`SF_USERNAME` + `SF_PASSWORD` + `SF_SECURITY_TOKEN`) is still supported for CLI batch pulls — see `sf-sync/README.md`.

---

## Troubleshooting

### "NOT CONFIGURED" or "MISSING CREDS" in the Configurator

- `.env` is missing `SF_CONSUMER_KEY` / `SF_CONSUMER_SECRET`, or
- You edited `.env` but didn't rebuild: `docker compose up --build`

### "LOGIN REQUIRED" after clicking Pull

Click **Connect to Salesforce** first. OAuth is per-browser-session.

### Redirect fails / "redirect_uri mismatch"

The Callback URL in the Connected App must **exactly** match:

```
http://localhost:8081/oauth/callback
```

No trailing slash. If you changed `SF_REDIRECT_URI` in `.env`, update the Connected App to match.

### "Invalid OAuth state"

- Browser blocked cookies, or
- `FLASK_SECRET_KEY` changed while you were mid-login

Click **Connect** again. Use `http://localhost:8080` (not `file://`) for the Configurator.

### "user hasn't approved this consumer"

IT needs to either:

- Set **All users may self-authorize**, or
- Pre-authorize your user on the Connected App (permission set / profile)

### "Account not found in Salesforce"

- Account name is **case-sensitive** in SOQL — use the exact name from the SF UI.
- With OAuth, you only see accounts **your user** can access.

### Connected App not working immediately after creation

Wait up to 10 minutes. Salesforce propagates new Connected Apps asynchronously.

### Sidecar unreachable

```bash
docker compose ps          # both qbr and sf-sync should be up
docker compose logs sf-sync
```

The sidecar takes a few seconds to start on first boot.

---

## For developers

| File | Purpose |
|------|---------|
| `sf-sync/oauth.py` | OAuth authorize, token exchange, refresh |
| `sf-sync/server.py` | `/oauth/login`, `/oauth/callback`, `/pull` |
| `sf-sync/sync.py` | SOQL + mapping to `qbr.data.json` |
| `QBR Configurator.dc.html` | Connect / Disconnect / Pull UI |

API endpoints: see `sf-sync/README.md`.

---

## Security notes

- `.env` is gitignored — never commit real keys.
- Each TAM runs Docker locally; customer JSON written by pulls goes to a Docker volume (`sf-data`) unless you download from the Configurator.
- OAuth tokens live in an httpOnly-style Flask session on localhost only.
- For a **hosted** deployment (not localhost), you need a real HTTPS callback URL and IT review — see `SERVICE-CONTRACT.md`.
