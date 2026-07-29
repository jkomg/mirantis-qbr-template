# Session memory — resume after reboot

Last updated: 2026-07-29

## Where we left off

Set up an agent delegation model (`AGENTS.md` + three glob-scoped rules under `.cursor/rules/`) and used it to unblock SLA report testing. The sandbox has too few cases to exercise the first-response SLA logic, so `scripts/make-sla-fixtures.py` now generates synthetic accounts. Building it surfaced three contract bugs in `perf-report.html`, all fixed. A Bugbot pass then found live Salesforce credentials in a kompose-generated ConfigMap.

**Action still owed: rotate the Salesforce security token for `jkennedy@mirantis.com`.** It was production, plaintext, in `k8s/env-configmap.yaml`. Never committed (the file was untracked), now scrubbed, but rotate anyway.

## Repo / stack

- Path: `/Users/jkennedy/Projects/Mirantis QBR template design`
- GitHub: `jkomg/mirantis-qbr-template`
- Deck/Configurator: `http://localhost:8080`
- Sidecar: `http://localhost:8081` (`SYNC_VERSION=v0.5`)

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
SYNC_VERSION=v0.5
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

## Uncommitted work

### Earlier sessions

1. **Live hydrate** — SF pull / full JSON import no longer fills Vertex demos into blanks (`QBR Configurator.dc.html`).
2. **Placeholder gating** — flags demo leftovers + empty TAM sections after SF pull; Save/Download confirm before proceeding.
3. **AI review** — `sf-sync/review.py`, `POST /review`, Configurator "Generate status commentary" + apply suggested takeaways.
4. Sync no longer defaults empty Account Type → `"Strategic"`.
5. **SLA pull** — Case severity via `Severity_Level__c`, `CaseHistory` + `CaseMilestone`, `sourceReview.ticketDetail` for the report, RevOps commercial fields (`ARR__c`, `Total_Won_Amount__c`, `Open_Pipeline__c`, `Upcoming_renewal_date__c`), `of_nodes__c` for node counts. Version `v0.5`.

### This session

6. **Agent model** — `AGENTS.md` plus `.cursor/rules/{sf-sync-python,static-frontend,container-infra}.mdc`. Domain invariants attach by file glob so subagents inherit them.
7. **Fixtures** — `scripts/make-sla-fixtures.py` writes deterministic `accounts/demo-*.json` (~540 cases across 5 accounts). Only ever touches `demo-*.json`.
8. **Report fixes** — `perf-report.html`: unguarded `d.customer.name` reads no longer blank the whole portfolio; SLA severity keys off `openedAs` before `severity`; boundness requires P1/P2 so the top-line percentage and per-severity table can't diverge; `slaMilestone` surfaced.
9. **Credentials** — plaintext SF password/token removed from `k8s/env-configmap.yaml`; pods layer a `secretRef` over non-secret defaults; `.gitignore` blocks secret-bearing manifests.
10. **k8s volume** — nginx and sf-sync co-located in one pod so they can share the ReadWriteOnce accounts claim (nginx read-only). `k8s/sf-sync-pod.yaml` deleted, merged into `qbr-pod.yaml`.
11. `sync.py --out` now also writes the stable `{slug}.json` the report reads.

Everything above is uncommitted. `demo/meridian-financial-solutions.json` is untracked — include only if you intend to ship it.

## After reboot — verify

1. `python3 scripts/make-sla-fixtures.py` (synthetic accounts; `--clean` removes them)
2. `docker compose up --build`
3. Open **SLA report** → portfolio should show 5 demo accounts with clearly different breach rates
4. Open Configurator → Clear draft (if old Vertex leftovers in localStorage) → Search/Pull **MKE-k0f integration**
5. Confirm orange banner lists empty TAM sections (not Vertex ARR/wins)
6. Save should confirm if flags remain
7. If API key set: **Generate status commentary**

## Still TAM-owned (SF does not fill)

Wins, asks, roadmaps, training narrative, real ARR/NPS when SF has none.

## Org note

Team rule: agents must not auto-run shell/tools — paste command output back into chat to continue.
