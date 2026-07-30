# Session memory — Cursor / resume after reboot

Last updated: 2026-07-29

Shared with Claude via `CLAUDE.md` → points here. Durable invariants live in
`.cursor/rules/` and `AGENTS.md`. This file is **session state only** — update
it when the working account, version, or next action changes.

## Where we left off

SLA scoring widened to all four severities against the Confluence
[Service Level Management](https://mirantis.jira.com/wiki/spaces/2S/pages/884343127/Service+Level+Management)
table. Tier derivation confirmed on sandbox: `SlaProcess.Name` →
`Entitlement.Support_Level__c`; LabCare/Custom labeled but not scored.
Follow-up posted on **[SC-5980](https://mirantis.jira.com/browse/SC-5980)**
asking for LabCare windows + richer Case sandbox.

**Agreed next product direction:** build rich synthetic / demo JSON that
matches the real SF→QBR contract, design the deck against that, then switch
`sf-sync` to prod. Sandbox is too thin for demos.

**Action still owed:** rotate the Salesforce security token for
`jkennedy@mirantis.com` (was production, plaintext in an untracked
`k8s/env-configmap.yaml`; scrubbed, never committed — rotate anyway).

## Repo / stack

- Path: `/Users/jkennedy/Projects/Mirantis QBR template design`
- GitHub: `jkomg/mirantis-qbr-template`
- Deck / Configurator / SLA report: `http://localhost:8080`
- Sidecar: `http://localhost:8081` (`SYNC_VERSION=v0.6`)

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
SYNC_VERSION=v0.6
```

Optional AI review: `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` →
`/health` reports `reviewAvailable: true`.

## Best sandbox account

| | |
|---|---|
| Name | **MKE-k0f integration** |
| Id | `001VF00000qH4OeYAK` |
| Objects | `Environment__c`, `License__c`, `Case`, `Entitlement`, `CaseMilestone`, `SlaProcess` |
| Not available | standard `Asset` (`INVALID_TYPE`) |
| Tier probe | resolved OpsCare via `SlaProcess.Name`; also sees LabCare / Custom |
| Cases | ~11, almost no Sev 1/2 — use fixtures for SLA UI |

Inspect:

```bash
curl -s 'http://localhost:8081/inspect?account=MKE-k0f%20integration' | python3 -m json.tool
```

## Demo / fixture data

```bash
python3 scripts/make-sla-fixtures.py          # writes accounts/demo-*.json
python3 scripts/make-sla-fixtures.py --clean  # removes only demo-*.json
```

- Prefix `demo-` only; never touches real customer pulls.
- Good for SLA report; **not yet** a full QBR demo pack (wins/asks/roadmaps thin).
- `demo/meridian-financial-solutions.json` is gitignored — leave it alone.

## Key contracts (do not re-discover)

| Topic | Source of truth |
|---|---|
| Case severity | `Severity_Level__c` (not `Priority`) |
| Severity at open | `CaseHistory` → `openedAs` |
| Commercial | `ARR__c`, `Total_Won_Amount__c`, `Open_Pipeline__c`, `Upcoming_renewal_date__c` |
| Nodes | `Environment__c.of_nodes__c` |
| SLA wiki | Confluence space `2S`, page `884343127` |
| SLA score | Initial response, Sev 1–4; P1/P2 = headline only |
| Tier | OpsCare / OpsCare Plus have targets; LabCare/Custom → `supportLevel`, `tier: null` |
| Payload for report | `sourceReview.slaScoring` + `ticketDetail[]` |
| Jira thread | [SC-5980](https://mirantis.jira.com/browse/SC-5980) |

## Agent model

- Planner: this chat + `AGENTS.md`
- Domain rules: `.cursor/rules/{sf-sync-python,static-frontend,container-infra}.mdc`
- Org: **no auto-run** — print commands, wait for paste-back
- Never paste `accounts/*.json` or `.env` into chats / commits / briefs

## After reboot — verify

1. `python3 scripts/make-sla-fixtures.py`
2. `docker compose up --build`
3. SLA report → demo accounts with different breach rates + tier chips
4. Configurator → Clear draft → Pull **MKE-k0f integration**
5. Confirm no Vertex demo backfill into blanks

## Still TAM-owned (SF does not fill)

Wins, asks, roadmaps, training narrative, NPS when SF has none.
