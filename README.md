# Mirantis QBR Template

HTML-native template for Mirantis Quarterly Business Reviews. A TAM, SDM, or Sales lead fills in a single Configurator form (or pastes from Salesforce), turns dials for tone and emphasis, and gets an 18-slide deck that exports cleanly to PowerPoint. Runs locally in Docker with **zero external network requests** at runtime — safe for handling real customer data.

![status](https://img.shields.io/badge/status-prototype-blue) ![runtime](https://img.shields.io/badge/runtime-docker-informational) ![data](https://img.shields.io/badge/data-ephemeral-success)

---

## Quick start

```bash
docker compose up --build
# → open http://localhost:8080
```

See [`LOCAL-SETUP.md`](./LOCAL-SETUP.md) for the no-Docker path and the air-gapped configuration.

---

## What's in the box

| File | What it is |
| --- | --- |
| `QBR Configurator.dc.html` | Intake form — customer/ARR/usage/support, incidents, wins, risks, asks, training, roadmaps. Save draft + Import from Salesforce JSON + per-slide ON/OFF toggles. |
| `QBR Template.dc.html` | 18-slide deck. Reads from `localStorage` → `qbr.data.json` → built-in defaults. Tweaks panel exposes Length, Sales aggression, Support emphasis, Risk framing, Tech depth, Tone, Product mix. |
| `qbr.data.json` | Demo data (Vertex Logistics). Real customer files live outside git in `accounts/` (gitignored). |
| `assets/fonts.css` + `scripts/fetch-fonts.sh` | Self-hosted Overpass / Lato / JetBrains Mono. Docker build fetches woff2 files; runtime serves them locally. |
| `Dockerfile` + `docker-compose.yml` + `docker/` | Container scaffold — nginx:alpine, two-stage build, no mounted volumes (data ephemeral). |
| `scripts/mirantis-qbr-sync.js` | CLI scaffold for the Salesforce → JSON pull (hand to RevOps). |
| `AUTOMATION.md`, `SERVICE-CONTRACT.md`, `LOCAL-SETUP.md` | Architecture docs. Read them before opening a PR that changes data flow. |

---

## How a TAM uses it

1. Open `http://localhost:8080/` → **Configurator**
2. Either:
   - **Paste-import** a Salesforce Report JSON
   - **Type directly** into the form
3. Toggle slides on/off in the **Slides to include** panel
4. **Save draft** (lives in your browser's `localStorage` only)
5. Open the **Deck**, set the dials in the Tweaks panel
6. Export to PPTX from the toolbar

Real customer data never leaves your machine. The container has no mounted volumes and the deck makes no external network requests.

---

## Architecture in one paragraph

The Configurator and Deck are two `.dc.html` files served from the same nginx origin so they share `localStorage.qbr_data_draft`. The Deck's `componentDidMount` reads localStorage first, falls back to `./qbr.data.json`, then to built-in demo values. Dials in the Tweaks panel are React component props that always win over JSON. Section toggles (per-slide ON/OFF) live in the JSON's `sections` map and are combined with the Length dial via `<sc-if>` gates. PPTX export captures each slide's DOM and converts to native PowerPoint shapes.

---

## Adding a new slide

1. Add a `<section data-label="My New Slide" data-speaker-notes="…">…</section>` to `QBR Template.dc.html` between two existing slides
2. Wrap it in `<sc-if value="{{ secMyNewSlide }}">…</sc-if>` if it should be toggle-able
3. Add `secMyNewSlide: d.sections?.myNewSlide !== false` to the renderVals block in the template's logic
4. Add `{ key: 'myNewSlide', label: 'My New Slide' }` to `SLIDE_TOGGLES` in `QBR Configurator.dc.html`
5. Update slide footers if you care about the `N / 18` counts

---

## Data automation

The schema (`qbr.data.json`) is the contract. Four paths to populate it, in increasing engineering cost:

- **A — TAM-pasted import** — shipped, in the Configurator
- **B — CLI tool** — scaffold at `scripts/mirantis-qbr-sync.js`, hand to RevOps
- **C — Live service** — spec at `SERVICE-CONTRACT.md`
- **D — iPaaS (Workato/Zapier)** — alternative if already licensed

See [`AUTOMATION.md`](./AUTOMATION.md) for the full breakdown.

---

## License

Internal Mirantis. Not for redistribution.
