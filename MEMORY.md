# Session memory — resume after reboot

Last updated: 2026-07-27

## Where we left off

Salesforce Client Credentials pull works against the Mirantis MKE Ops sandbox. Configurator was still re-injecting **Vertex Logistics** demo defaults for blank fields after pull; that is fixed. Optional **AI account-status commentary** was added on the sidecar.

## Repo / stack

- Path: `/Users/jkennedy/Projects/Mirantis QBR template design`
- GitHub: `jkomg/mirantis-qbr-template`
- Deck/Configurator: `http://localhost:8080`
- Sidecar: `http://localhost:8081` (`SYNC_VERSION=v0.4`)

```bash
cd "/Users/jkennedy/Projects/Mirantis QBR template design"
docker compose up --build
```

## `.env` (sandbox Client Credentials)

```
SF_AUTH_MODE=client_credentials
SF_CONSUMER_KEY=...
SF_CONSUMER_SECRET=...
SF_DOMAIN=mirantis--mkeops.sandbox.my
SYNC_VERSION=v0.4
```

Optional AI review (one provider):

```
OPENAI_API_KEY=sk-...
# or ANTHROPIC_API_KEY=...
```

`/health` should report `reviewAvailable: true` when a key is loaded.

## Best test account

- Name: **MKE-k0f integration**
- Id: `001VF00000qH4OeYAK`
- Useful SF objects: `Environment__c`, `License__c`, `Case`, `Entitlement` (standard Asset unavailable in this sandbox)

## What changed in this session (commit these)

1. **Live hydrate** — SF pull / full JSON import no longer fills Vertex demos into blanks (`QBR Configurator.dc.html`).
2. **Placeholder gating** — flags demo leftovers + empty TAM sections after SF pull; Save/Download confirm before proceeding.
3. **AI review** — `sf-sync/review.py`, `POST /review`, Configurator “Generate status commentary” + apply suggested takeaways.
4. Sync no longer defaults empty Account Type → `"Strategic"`.
5. Docs/env: `.env.example`, `sf-sync/README.md`, Dockerfile copies `review.py`, version bump `v0.4`.

### Files to stage (if not already committed)

- `QBR Configurator.dc.html`
- `sf-sync/review.py` (new)
- `sf-sync/server.py`
- `sf-sync/sync.py`
- `sf-sync/Dockerfile`
- `sf-sync/README.md`
- `.env.example`
- `MEMORY.md` (this file)

Untracked `k8s/*` and `demo/meridian-financial-solutions.json` were present earlier — include only if you intend to ship them.

## After reboot — verify

1. `docker compose up --build`
2. Open Configurator → Clear draft (if old Vertex leftovers in localStorage) → Search/Pull **MKE-k0f integration**
3. Confirm orange banner lists empty TAM sections (not Vertex ARR/wins)
4. Save should confirm if flags remain
5. If API key set: **Generate status commentary**

## Still TAM-owned (SF does not fill)

Wins, asks, roadmaps, training narrative, real ARR/NPS when SF has none.

## Org note

Team rule: agents must not auto-run shell/tools — paste command output back into chat to continue.
