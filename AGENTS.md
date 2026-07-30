# Agent playbook — Mirantis QBR template

Orientation and delegation rules. Domain invariants live in
`.cursor/rules/` and attach automatically by file glob — don't restate them
here.

## What this repo is

A customer-facing QBR deck plus an SLA & Service Performance report, both
static HTML served by nginx (`:8080`), fed by a Flask sidecar (`:8081`) that
pulls from Salesforce and writes JSON into the shared `./accounts` folder.
`MEMORY.md` holds current session state and the working sandbox account.

## Org policy: no auto-run

Agents must not execute shell commands or auto-approve tools. Print the exact
command in a fenced block and wait for the user to run it and paste output
back. This applies to subagents too — every brief must say so explicitly, and
the `shell` subagent type is off the table.

Consequence: **no agent can close its own verify loop.** Prefer changes that
are verifiable by inspection, and lean on `bugbot` for review, since static
analysis is the only automated check available.

## Delegation model

**Planner** holds durable context — the Salesforce field map, the
`sourceReview` / `slaScoring` contract, and that initial-response SLA covers
Sev 1–4 with P1/P2 as the headline only. It reads indexes (grep hits,
signatures, subagent reports), not whole files. Session state is in
`MEMORY.md`; Claude Code also reads `CLAUDE.md`.

**Specialists** are `generalPurpose` subagents scoped to a disjoint slice of
the tree. They inherit domain knowledge from the glob rules, so briefs cover
scope and intent, not background.

| Slice | Files | Rule that attaches |
|---|---|---|
| Salesforce backend | `sf-sync/*.py` | `sf-sync-python.mdc` |
| Report / deck | `*.html`, `*.js`, `assets/` | `static-frontend.mdc` |
| Container / deploy | `Dockerfile`, `docker-compose.yml`, `k8s/`, `docker/` | `container-infra.mdc` |

**Verifiers** are `bugbot` on the branch diff, and `security-review` whenever
credentials, OAuth, or request handling changed.

Use `explore`, not `generalPurpose`, for "where does X live" — same answer,
much cheaper.

## When not to delegate

Delegation costs one brief plus one report. Under roughly five file reads or
two edit-verify cycles, that overhead exceeds the saving — just do it inline.
Delegate work that is exploratory, iterative, or spans a whole subsystem.

**Parallel subagents must own disjoint files.** `sf-sync/*.py` alongside
`perf-report.html` is fine. Two agents in `mirantis.py` will clobber each
other.

## Brief template

```
GOAL        One sentence. The outcome, not the steps.
SCOPE       Files you may edit. Everything else is read-only.
CONTEXT     Only what the glob rules don't already cover.
DONE WHEN   Observable conditions, not "it works".
CONSTRAINTS Do not run commands — emit them for the user.
            Do not touch files outside SCOPE.
            Do not add dependencies.

Report back in under 15 lines:
- Files changed (paths only)
- What behavior changed, one sentence each
- Any invariant you had to break, and why
- Exact commands for the user to run
No code blocks of your edits. No narration of your process.
```

That closing report contract is where the token efficiency actually comes
from. Without it a subagent returns hundreds of lines of narration and the
delegation is net-negative.

## Data handling

`accounts/*.json` and `.env` contain real customer and credential data. Both
are gitignored. Never paste their contents into a summary, a commit message,
or a subagent brief — pass the file path instead.
