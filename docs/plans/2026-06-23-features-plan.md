# Setuora - Feature & Deployment Plan (2026-06-23)

Phased, self-contained plan for four requests:

1. Fix nav so Receive/Sale/Audit don't all funnel confusingly into Batches → **Batches dropdown submenu**.
2. **Install all packages + set up SQLite** (reproducible venv).
3. **Dynamic, DB-backed data** for mobile + desktop users → keep server-rendered pages (no GraphQL), add **live auto-refresh**.
4. **Auto-save** for new-batch draft, settings/company, batch rate edits, product create/edit.
   Plus: simple Windows batch-file setup for the single LAN server (SQLite retained).

## Decisions already made (do not re-litigate)

- **Deployment:** Windows batch-file setup on ONE Windows LAN server. **Keep SQLite.** No load balancer / multi-replica (SQLite is single-writer; the SRS scope is ~10 users on one machine). Caddy remains optional for HTTPS on the LAN.
- **No GraphQL.** Both mobile and desktop users use the **browser** (SRS: "No dedicated Android application required"). Pages are already DB-backed via SQLAlchemy. The real need is _freshness for concurrent users_ → solve with lightweight JSON polling + DOM updates, not a new API layer.
- **Auto-save tech:** vanilla `fetch` + debounce, mirroring the existing pattern in [app/static/scanner.js](../../app/static/scanner.js). **No new JS framework, no CDN** (LAN/offline). HTMX is explicitly NOT used.
- **Creation vs. edit auto-save:** auto-save _existing_ records to the DB; auto-save _creation_ forms (new batch, new product) to `localStorage` drafts and restore them - this avoids orphan DRAFT/empty rows. Editing existing records hits the DB.

---

## Phase 0 - Discovery / Allowed APIs (consolidated; already gathered)

Environment (verified via `python --version` + `pip freeze` on 2026-06-23):

- Python **3.11.9**.
- Installed pins: `fastapi==0.138.0`, `starlette==1.3.1`, `uvicorn==0.49.0`, `SQLAlchemy==2.0.25`, `Jinja2==3.1.6`, `python-multipart==0.0.32`, `qrcode==8.2`, `pillow==12.2.0`, `reportlab==5.0.0`, `openpyxl==3.1.5`, `httpx==0.27.0`, `pytest==9.0.3`, `pytest-asyncio==1.3.0`, `anyio==4.13.0`.
- `Flask-SQLAlchemy==3.1.1` is present in the global env but is NOT used by this app - do not add it to requirements.

Existing patterns to COPY (cite these, don't invent):

- **Nav markup:** [app/templates/base.html](../../app/templates/base.html) `.subnav` block.
- **Client fetch + DOM update:** [app/static/scanner.js](../../app/static/scanner.js) (`submitSerial` posts `FormData`, reads JSON `{ok,...}`).
- **JSON endpoint pattern:** `scan_into_batch` in [app/routers/batches.py](../../app/routers/batches.py):105 returns `JSONResponse({"ok": True, ...})`.
- **Existing rate-edit endpoints (already DB-backed):** `POST /batches/{id}/items/{id}/rate` and `POST /batches/{id}/products/{id}/rate` in [app/routers/batches.py](../../app/routers/batches.py):143-180.
- **Settings save + validation + company mirror:** [app/routers/settings.py](../../app/routers/settings.py) (`validate_settings`, `save_active_company_config`).
- **Dashboard data source:** `dashboard_counts` / `status_summary` in [app/services/inventory.py](../../app/services/inventory.py):249.
- **DB/WAL config:** [app/database.py](../../app/database.py) (SQLite WAL + FK pragmas already set).
- **Caddy reverse proxy:** [deployment/caddy/Caddyfile.example](../../deployment/caddy/Caddyfile.example).

Anti-patterns to avoid:

- Do NOT enable Tally sync via auto-save - the `tally_enabled` readiness gate in `save_settings` must stay behind the explicit "Save settings" button.
- Do NOT create DB rows on field-change for creation forms (orphan rows). Use localStorage drafts there.
- Do NOT add websockets, GraphQL, Celery, Redis, or a second datastore.
- Do NOT post return/issue voucher XML to Tally (existing guardrail in CLAUDE.md).

---

## Phase 1 - Reproducible environment + SQLite (request #2)

**What to implement**

1. Pin exact versions: rewrite [requirements.txt](../../requirements.txt) using the Phase 0 pins (`==`). Keep only packages the app imports (drop Flask-SQLAlchemy).
2. Document/script venv setup (matches README section 2-5): create `.venv`, install, copy `.env`.
3. Confirm SQLite needs no server: it auto-creates at `data/setuora.db` (see [app/database.py](../../app/database.py):16-19) with WAL. Add a one-line note that `data/` must be writable.
4. Generate a real `.env` from `.env.example` with a strong `APP_SECRET_KEY`; document `SESSION_COOKIE_SECURE=true` for HTTPS.

**Verification**

- `.\.venv\Scripts\python.exe -m pytest -q` → 24 passed.
- App boots: `uvicorn app.main:app` → `GET /health` returns `{"status":"ok"}`.
- `pip check` reports no broken requirements.

**Anti-pattern guards:** don't pin versions not produced by Phase 0; don't add a DB server.

---

## Phase 2 - Batches dropdown submenu (request #1)

**What to implement**

1. In [app/templates/base.html](../../app/templates/base.html), replace the flat `Receive / Sale / Audit / Batches` links with a single **Batches** nav item that opens a dropdown containing: Receive, Sale, Audit, Sales return, Purchase return, Issue, and "All batches". Keep the existing `/batches/new?batch_type=...` hrefs.
2. Implement the dropdown with a native `<details>`/`<summary>` (no JS needed; matches the existing `<details class="add-company">` pattern) OR a small CSS hover/focus menu. Prefer `<details>` for keyboard/touch friendliness on mobile.
3. Add `.subnav` dropdown CSS to [app/static/styles.css](../../app/static/styles.css): menu panel uses `--e2` shadow, `--r-md` radius, `--canvas` bg, hairline border; respects the existing pill nav styling. Mark the Batches item `active` when `request.url.path.startswith('/batches')`.
4. Keep Dashboard/Serials/Products/etc. as-is.

**Verification**

- Render the dashboard (Edge headless screenshot per established workflow) - top nav shows a single "Batches menu" that opens the six batch types + All batches.
- Each link still lands on the correct `/batches/new?batch_type=...` form.
- Keyboard: Tab to Batches, Enter opens; Esc/blur closes. Works on a 390px-wide viewport.

**Anti-pattern guards:** don't remove the batch-type query param contract; don't require JS for the menu to open.

---

## Phase 3 - Auto-save (request #4)

Mirror [app/static/scanner.js](../../app/static/scanner.js): one small `app/static/autosave.js`, debounced (~600ms), with a per-form "Saved / Saving... / Save failed" status node. Include it only on the relevant templates.

**3a. Existing-record auto-save → DB (JSON endpoints returning `{ok, error?}`)**

- **Settings / company:** add `POST /settings/autosave` that updates ONLY the company + retry-interval keys via `update_settings` + `save_active_company_config`, reusing `validate_settings`-style checks (port/interval). It must NOT touch `tally_enabled`. Return `{ok:false,error}` on invalid input so the indicator shows it. Wire field `change`/`input` events in [app/templates/settings.html](../../app/templates/settings.html) modal form. The explicit "Save settings" button stays (it's the only path that can enable Tally sync, keeping the readiness gate).
- **Batch rate edits:** the endpoints already exist and persist to DB. Add JS to the rate inputs in [app/templates/batch_detail.html](../../app/templates/batch_detail.html) to POST on debounce instead of requiring submit; show the indicator. Reuse `POST /batches/{id}/items/{id}/rate` and `/products/{id}/rate` but add JSON variants (or `Accept`-based branching) so the page doesn't 303-redirect on every keystroke.

**3b. Creation forms → localStorage draft (no orphan rows)**

- **New batch (party/notes):** in [app/templates/batch_new.html](../../app/templates/batch_new.html), auto-save field values to `localStorage` keyed by batch_type; restore on load; clear on successful submit. Show "Draft saved".
- **Product create:** same localStorage draft approach in [app/templates/products.html](../../app/templates/products.html) create form.

**Verification**

- Settings: change a ledger field, wait, reload → value persisted; check `Setting` row + active `Company.config` updated; bad port shows "Save failed".
- Confirm `tally_enabled` cannot be turned on via autosave (grep the new endpoint; it must not accept/!set that key).
- Batch rate: edit a rate, no full reload, value persists (re-open batch).
- New batch: type party/notes, reload the form → restored; submit → draft cleared.
- `pytest -q` still 24 passed (add 1-2 tests for the settings autosave endpoint: persists fields, rejects bad port, ignores tally_enabled).

**Anti-pattern guards:** no autosave path may enable Tally sync; no DB writes for not-yet-created entities; debounce to avoid request storms.

---

## Phase 4 - Live auto-refresh for concurrent users (request #3, data)

**What to implement**

1. Add read-only JSON endpoints: `GET /dashboard/data` (returns `dashboard_counts` + recent batches/scans) and `GET /batches/{id}/items.json` (current items + statuses). Reuse existing services; require auth.
2. Add `app/static/live.js`: poll the relevant endpoint every ~20s, update only the changed DOM (counts, table rows). Pause when `document.hidden`; resume on focus. No layout shift.
3. Wire `live.js` into [app/templates/dashboard.html](../../app/templates/dashboard.html) and [app/templates/batch_detail.html](../../app/templates/batch_detail.html).
4. Ensure responsive layout holds for mobile + desktop (existing CSS already responsive; verify the new dropdown + indicators at 390px).

**Verification**

- Open dashboard in two browser sessions; create a batch in one → the other reflects new counts/rows within ~20s without manual refresh.
- Network tab: polling pauses when the tab is backgrounded.
- No regressions to server-rendered first paint (JS only augments).

**Anti-pattern guards:** no websockets; polling interval ≥ 15s; endpoints read-only.

---

## Phase 5 - Windows deployment, single server + SQLite

**What to implement**

1. `scripts/setup.bat`: first-time setup for non-technical users. It prepares Python/venv, installs requirements, writes `.env`, creates `data/` and `logs/`, verifies app import, and optionally starts the app.
2. `scripts/start_setuora.bat`: normal launcher after setup.
3. Keep [deployment/windows/install_service.ps1](../../deployment/windows/install_service.ps1) for optional NSSM service installs.
4. Keep [deployment/caddy/Caddyfile.example](../../deployment/caddy/Caddyfile.example) for optional LAN HTTPS when phone camera access requires it.
5. Docs: [README.md](../../README.md), [docs/deployment/installation-guide.md](../deployment/installation-guide.md), and [docs/deployment/backup-restore-guide.md](../deployment/backup-restore-guide.md) document the batch-file setup and backup requirements.

**Verification**

- `scripts/setup.bat -SkipStart` succeeds against an existing `.env`.
- App boots with `scripts/start_setuora.bat`; `GET /health` returns `{"status":"ok"}`.
- Bootstrap admin login works on a fresh `data/setuora.db`.
- `data/` is writable; WAL files are created.

**Anti-pattern guards:** don't require container tooling for the client handoff; don't delete or reset `data/` during setup; keep `.env` and the whole `data/` folder in scheduled backups.

---

## Phase 6 - Final verification

1. `pytest -q` → all pass (24 + new autosave tests).
2. Manual smoke (headless Edge screenshots per established workflow): nav dropdown, settings autosave indicator, dashboard live refresh, batch rate autosave.
3. Grep guards:
   - autosave endpoint does not set `tally_enabled` (`grep -n tally_enabled app/routers/settings.py` → only in `save_settings`).
   - no `graphql`, `websocket`, `flask` imports introduced.
4. `scripts/setup.bat -SkipStart`, then `scripts/start_setuora.bat`; verify `/health` and data persistence.
5. Update [CLAUDE.md](../../CLAUDE.md): nav dropdown, autosave endpoints (and the tally_enabled exclusion), live-refresh endpoints, Windows setup flow + SQLite note.

## Suggested execution order

Phase 1 → 2 → 3 → 4 → 5 → 6. Phases 2, 3, 4 are largely independent and can be parallelized by separate agents; Phase 5 depends on Phase 1's pinned requirements.
