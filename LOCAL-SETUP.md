# Local Setup — Mirantis QBR Template

Run the deck locally, fully offline, with **zero external network requests**. Customer data lives only on your machine (browser `localStorage` + any JSON files you explicitly download). The container makes no upstream connections at runtime.

---

## Option 1 · Docker (recommended)

Build once, run forever. Fonts are baked in during build; runtime is air-gappable.

```bash
docker compose up --build
# → open http://localhost:8080
```

Stop:

```bash
docker compose down
```

There are no mounted volumes — anything saved inside the container disappears on stop. Customer data persists only:

- in your browser's `localStorage` (Configurator's "Save draft")
- in JSON files you explicitly download via the Configurator's "↓ Download JSON" button

### Fully air-gapped

Uncomment `network_mode: "none"` in `docker-compose.yml` to remove the container's network access entirely after the build step. nginx still serves the deck over the published port; the container itself cannot phone home.

---

## Option 2 · Python (no Docker)

If you want to edit files and refresh the browser without rebuilding:

```bash
bash scripts/fetch-fonts.sh        # one-time: downloads woff2 files
python3 -m http.server 8080
# → open http://localhost:8080/
```

You can skip `fetch-fonts.sh` — the deck falls back to system fonts (Helvetica/Arial for Lato, system-ui for Overpass, monospace for JetBrains Mono). Slightly less polished but fully functional.

---

## Workflow for a real customer QBR

1. Open `http://localhost:8080/` → click **Configurator**
2. Either:
   - **Paste-import:** SF Report JSON, flat object, or full `qbr.data.json` into the Import panel
   - **Type directly:** Customer name, ARR, support metrics, narrative (wins/risks/asks)
3. In **Slides to include**, turn off any sections that don't apply this quarter
4. Set the dials in the Tweaks panel (Length, Tone, Sales aggression, Support emphasis, etc.)
5. Click **Save draft** (lives in browser localStorage) and/or **Download JSON** (lives next to the deck)
6. Click **Deck →** to present
7. Export to PPTX from the toolbar when ready
8. Walk the meeting

### Reusing the same account next quarter

The `qbr.data.json` you download is the source of truth. Keep it in a private repo (or wherever Mirantis stores customer data today) per account:

```
accounts/
  vertex/
    q2-fy26.json
    q3-fy26.json
    q4-fy26.json   ← copy q3, edit, hand off
```

To load a specific file, set the deck's `dataFile` tweak in the Tweaks panel: `./accounts/vertex/q4-fy26.json`.

---

## What's in the container

- `QBR Configurator.dc.html` — the intake form
- `QBR Template.dc.html` — the 18-slide deck
- `assets/` — logo, fonts (woff2), shared CSS
- `qbr.data.json` — built-in demo values (Vertex Logistics)
- `scripts/mirantis-qbr-sync.js` — SF integration scaffold (for your RevOps team)
- `AUTOMATION.md`, `SERVICE-CONTRACT.md` — architecture docs

---

## Privacy & data handling

- **No analytics, no telemetry, no fonts CDN, no remote scripts.**
- All UI assets are self-hosted; the only file with any `http://` reference is this README (links to Mirantis-internal docs).
- The browser's `localStorage.qbr_data_draft` key holds the current draft. To wipe it: open the Configurator and click **Clear draft**, or use your browser's site-data controls.
- PPTX export runs entirely in the browser; the resulting `.pptx` is downloaded directly to your disk.

---

## Troubleshooting

**"Port 8080 already in use"** — change the host port in `docker-compose.yml`: `"9090:8080"`.

**"Fonts look wrong"** — `scripts/fetch-fonts.sh` may have failed. Re-run it; the deck still works with system-font fallbacks if the woff2 files are missing.

**"Deck says data is loading but never resolves"** — Configurator and Deck must be served from the same origin (both via `http://localhost:8080/`), so `localStorage` is shared. Opening one as `file://` and the other via http breaks the bridge.

**"I want to test with a real customer's data"** — paste their SF Report JSON into the Configurator's Import panel. The data lives only in your browser. To delete it: Clear draft + close the tab.
