# Claude Code — Mirantis QBR template

Project instructions for Claude. Read these before editing.

## Boot order

1. **`MEMORY.md`** — current session state, sandbox account, versions, next actions
2. **`AGENTS.md`** — delegation model, brief template, no-auto-run org policy
3. **`.cursor/rules/*.mdc`** — domain invariants (attach by file glob; still read them when touching matching paths)

Do not restate rule contents in commits or long summaries. Prefer indexes (grep, signatures) over loading whole HTML/Python files.

## What this repo is

Customer-facing QBR deck + SLA & Service Performance report (static HTML on
nginx `:8080`), fed by a Flask Salesforce sidecar (`sf-sync` on `:8081`) that
writes JSON into `./accounts`. Shared bind mount — a pull appears in the
report with no copy step.

| Surface | Entry |
|---|---|
| Deck / Configurator | `QBR Template*.dc.html`, `QBR Configurator.dc.html` |
| SLA report | `perf-report.html` |
| SF sync | `sf-sync/{mirantis,sync,server,oauth,review}.py` |
| Demo fixtures | `scripts/make-sla-fixtures.py` → `accounts/demo-*.json` |

## Org policy (non-negotiable)

- **Do not execute shell commands** or auto-approve tools. Print exact commands;
  wait for the user to run them and paste output.
- Prefer changes verifiable by inspection. Use Bugbot / static review when a
  verify loop would need a shell.
- Never commit or paste `.env`, `accounts/*.json`, or secret-bearing k8s
  manifests. `demo/meridian-financial-solutions.json` is gitignored — leave it.

## Coding boundaries

| Slice | Edit freely | Rule |
|---|---|---|
| Salesforce backend | `sf-sync/*.py` | `sf-sync-python.mdc` |
| Report / deck | `*.html`, `*.js`, `assets/` | `static-frontend.mdc` |
| Containers | `Dockerfile*`, `docker-compose.yml`, `k8s/`, `docker/` | `container-infra.mdc` |

- No new dependencies without asking (`sf-sync/requirements.txt` stays small).
- Front end: vanilla ES2020, no bundler, no CDN, offline-capable.
- Field selection is describe-driven — add to `*_PREFERRED` lists, never hardcode SOQL columns.
- Missing SF data → warnings, not exceptions.

## Product direction (as of MEMORY.md)

1. Sandbox (MKE-k0f) is too thin for SLA/QBR demos.
2. Prefer rich **synthetic demo JSON** matching the real SF→payload contract.
3. Design the QBR against that shape, then switch `sf-sync` to **prod**.
4. Live Jira: [SC-5980](https://mirantis.jira.com/browse/SC-5980) (RevOps fields, LabCare SLA windows, richer Case data).

## Quick commands (for the user to run)

```bash
cd "/Users/jkennedy/Projects/Mirantis QBR template design"
docker compose up --build
python3 scripts/make-sla-fixtures.py
curl -s 'http://localhost:8081/inspect?account=MKE-k0f%20integration' | python3 -m json.tool
```

## When finishing a session

Update **`MEMORY.md`** (date, where we left off, versions, open actions). Do not
duplicate that state into this file.
