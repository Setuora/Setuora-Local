# Setuora Barcode Tally Bridge

Setuora is a LAN-first barcode transaction bridge for Tally Prime. It lets staff scan product barcodes from phones, keeps serial-level history locally, and syncs supported stock movements to Tally through its XML gateway.

## Current Features

- Role-based login for admin, purchase, sales, and audit users
- Product master with HSN, GST, unit, default rate, sales discount, and exact Tally stock item name
- Bulk serial generation and printable/PDF QR labels with the serial number only
- Product batch, manufacturing date, expiry date, and warehouse tracking for assigned stock
- Purchase, sale, audit, sales return, purchase return, stock issue, barcode assignment, and barcode replacement workflows
- Batch pricing, GST split, round off, and voucher preview before submit
- FEFO picking and expiry control for sale, issue, and purchase-return batches
- Tally XML generation for purchase/receive, sale, and sales-return batches
- Tally Check screen for exact-name master readiness
- Live Tally company, ledger-name, and dated sales-book discovery for sync setup
- Saved Tally company profiles with active-company settings
- Editable admin role access controls for pages, actions, and data areas
- Pending sync queue, manual retry, and automatic retry worker
- Audit reconciliation for verified, missing, and extra serials
- Dashboard counts, charts, recent activity, and live refresh
- Configurable stock movement, stock-cover, slow/dead stock, overstock, and expiry-risk analysis with warehouse/franchise filters
- Excel reports, transaction history, scan history, and PDF audit reports
- SQLite-safe backup download and restore procedure

## Folder Structure

```text
Proj_Setu/
|-- README.md                         Project guide and setup notes
|-- Setuora.exe                       Unified Windows setup and control tool
|-- scripts/                          Windows workflows used by Setuora.exe
|   |-- setup.bat                     Setup workflow
|   |-- start_setuora.bat             Start workflow
|   |-- stop_setuora.bat              Stop workflow
|   `-- update.bat                    Update workflow
|-- requirements.txt                  Direct Python dependency pins
|-- requirements.lock                 Hash-verified production dependency lock
|-- app/                              FastAPI application
|   |-- main.py                       App entrypoint and route registration
|   |-- models.py                     SQLAlchemy database models
|   |-- routers/                      Page and API route handlers
|   |-- services/                     Business logic and integrations
|   |-- static/                       Browser JavaScript, CSS, and assets
|   `-- templates/                    Jinja HTML templates
|-- deployment/                       Windows service and Caddy config
|-- docs/                             Deployment, handoff, and context docs
|-- tests/                            Pytest coverage
|-- data/                             Runtime database and backups, ignored by git
`-- logs/                             Runtime logs, ignored by git
```

## Prerequisites

- Python 3.11 for the current pinned dependency set
- Tally Prime installed and running on the server machine or reachable on the LAN
- Chrome or Edge for staff phones
- Administrator access during Windows setup to install Caddy, its service, and the LAN firewall rule

## Quick Windows Setup For Non-Technical Users

For a new server, run `Setuora.exe` and choose `Install or finish setup`, then approve the
Windows administrator prompt. The executable installs Git for Windows if it is
missing, downloads or updates the official `main` branch into `C:\Setuora`, and
then runs the complete interactive setup described below. Internet access is
required. Setup places a copy of `Setuora.exe` in the installation folder for
later use.

The installer can also repair or update an existing `C:\Setuora` installation.
Choose `Repair this installation` for an automatic dependency, virtual-environment,
service, import, and regression-test check. Repair keeps `.env`, the database,
backups, runtime data, and source files unchanged, then restores the app to its
previous running or stopped state. Installer source and reproducible build
instructions are in `installer/`.

The same executable controls the normal lifecycle after setup:

```text
Setuora.exe setup
Setuora.exe repair
Setuora.exe update
Setuora.exe start
Setuora.exe stop
```

Setup, repair, update, and stop command windows close automatically when their
work finishes. A console-mode start window remains open only while the Setuora
server is running and closes after the server stops.

Setup checks for Python 3.11, installs the hash-verified dependency lock, creates
`data/` and `logs/`, asks for the first admin login, writes `.env`, and runs a
smoke test. By default it configures Caddy HTTPS, installs automatic Setuora and
Caddy Windows services, starts both, and opens LAN firewall ports 80 and 443.
Use `Setuora.exe setup --with-caddy=false` only when another reviewed HTTPS proxy
is already in use.

`Setuora.exe setup`, `repair`, `update`, `start`, and `stop` request Administrator
access automatically when launched through the unified executable.
The updater refuses a dirty worktree, does nothing when the installation is
already current, installs the hash-verified dependency lock, runs the test suite,
and restores the app to the state it had before the update. Normal releases are
fast-forwarded. If official release history differs from a clean installation,
the installed commit is first preserved on a timestamped `setuora-backup/...`
branch before source files are realigned. Runtime data, `.env`, and backups are
not changed.

Before going live on the target server, run the [production release
checklist](docs/deployment/production-release-checklist.md). It verifies the
actual Windows services, Caddy TLS, security headers, source checkout, tests,
and a fresh verified backup without disclosing secrets.

## 1. Open The Project Folder

```bash
cd /home/dj/Projects/Proj_Setu
```

On Windows, use the folder where this project is copied, for example:

```powershell
cd C:\Setuora
```

## 2. Create A Virtual Environment

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate again.

## 3. Install Dependencies

```bash
pip install --require-hashes -r requirements.lock
```

If `pip` is missing on Linux, install it with your OS package manager, then rerun the command.

## 4. Create The Environment File

Linux/macOS:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
copy .env.example .env
```

Open `.env` and update these before real use:

```text
APP_SECRET_KEY=replace-with-a-long-random-secret
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-unique-password
DATABASE_URL=sqlite:///./data/setuora.db
SESSION_COOKIE_SECURE=true
TRUSTED_HOSTS=setuora.local,127.0.0.1,localhost
```

The app refuses to create its first administrator with an empty, placeholder, or
default password. For a LAN deployment, serve only through HTTPS and set the
actual Caddy hostname or address in `TRUSTED_HOSTS`.

## 5. Start The App

Development mode:

```bash
uvicorn app.main:app --reload
```

Production-style local run:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## 6. First Login

If you used `scripts\setup.bat`, use the admin username and password shown at the end of setup. Keep that password somewhere safe because generated passwords are only displayed once.

After logging in:

1. Open `Users`.
2. Create named users for purchase, sales, auditor, and admin roles.
3. Use `Tally access` on a user to assign specific company profiles, ledgers, and Tally usernames. Empty assignment sections allow all values; super admins always have full Tally access.
4. Disable unused accounts or, as super admin, delete accounts that should no longer appear in the user list.
5. Store the first-admin password securely; changing bootstrap settings after the database exists does not change existing users.

Changing `BOOTSTRAP_ADMIN_PASSWORD` after `data/setuora.db` already exists does not reset an existing user. Create, disable, or delete users from the `Users` page. Deleted users with old activity are hidden from the list but kept internally for historical records.

## 7. Basic Setup Inside The App

Do this in order:

1. Open `Settings`.
2. Add or activate a company profile.
3. Enter the exact Tally company, host, port, voucher type names, ledger names, GST ledgers, and round-off ledger.
4. Leave `Enable Tally sync` off during setup. Other fields auto-save, but sync only changes when `Save settings` is clicked.
5. Open `Products`.
6. Create products using exact Tally stock item names, HSN, GST rate, unit, default rate, and sales discount if applicable.
7. Generate serial QR labels from `Products`, or use `Barcode Assignment` for existing physical stock.
8. Open `Tally Check`.
9. Mark each required Tally master as checked only after confirming the exact spelling in Tally.
10. Enable Tally sync only after Tally Check has no missing or unchecked items and a test XML is validated in Tally.

When switching the active company profile, Setuora disables Tally sync again so the new company's masters can be checked before posting.

## 8. Normal Workflow

Purchase stock:

1. Open `Batches` -> `Purchase`.
2. Enter supplier/reference.
3. Scan serials.
4. Check the voucher preview.
5. Submit the batch.

Sell stock:

1. Open `Batches` -> `Sale`.
2. Enter customer/reference.
3. Scan in-stock serials.
4. Use `Pick FEFO` when selling by product and quantity, or scan the earliest-expiry serials manually.
5. Check pricing, GST, round off, and final value.
6. Submit the batch.

Audit stock:

1. Open `Batches` -> `Audit`.
2. Enter location/reference.
3. Scan physical stock.
4. Submit the audit.
5. Review verified, missing, and extra findings.

Returns and issue:

- `Sales return`: scan sold items returned by customer.
- `Purchase return`: scan or FEFO-pick in-stock items returned to supplier.
- `Issue`: scan or FEFO-pick in-stock items issued for sample, office use, damage, marketing, production, or other reasons.

QR label assignment:

1. Open `Barcodes` -> `Assignment`.
2. Select an existing product and quantity, or upload an Excel file.
3. Excel can use `Product Code` or `Product Name` with `Quantity`; optional columns include `HSN`, `GST`, `SGST`, `IGST`, `Batch`, `Mfg Date`, `Expiry Date`, and `Warehouse`. Tally invoice exports with `Description of Goods` and `Quantity` are also accepted.
4. Download the generated Excel file and labels PDF.

Barcode replacement:

1. Open `Barcodes` -> `Replacement`.
2. Enter the damaged/old serial.
3. Leave new serial blank to auto-generate, or enter a new serial manually.
4. Print the new label.

## 9. Tally Integration

Tally sync is disabled by default.

Before enabling sync:

1. In Tally Prime, open the target company.
2. Enable Tally as a server on port `9000`.
3. Confirm inventory is maintained.
4. Confirm accounts and inventory are integrated.
5. In Setuora, complete `Tally Check`.
6. Download `Tally XML` from a purchase, sale, or sales-return batch and validate it against the real company.
7. Enable sync in `Settings`.

Supported live XML posting:

- Purchase/receive
- Sale
- Sales return as Credit Note

Implemented locally but intentionally not live-posted yet:

- Purchase return
- Stock issue

Those remain `PENDING_SYNC` until the exact Tally voucher XML for the client company is validated.

## 10. Reports And Exports

Use `Reports` for:

- Scan history
- Transaction history
- Pending sync
- Excel export
- Expiry summary context

Use batch detail pages for:

- Tally XML download
- Sync attempt request/response details
- Audit PDF export

Use label pages for:

- Browser print
- QR label PDF download
- Serial XLSX download

Use `Expiry` for:

- Expiring stock bands
- Slow-moving expiry risk
- Sleeping stock
- Warehouse expiry exposure
- Shortcuts to product batch entry and FEFO sale

## 11. Backup And Restore

Backup:

1. Open `Maintenance`.
2. Click `Download backup`.
3. Store the downloaded `.db` file safely.
4. Keep a separate copy of `.env`.

Automatic verified backups run by default every 24 hours into
`data/backups/`, retain the latest 14 files, and test each backup with SQLite
integrity and foreign-key checks before keeping it. Super admins can change the
automatic backup switch, backup folder, schedule, retention count, and
off-machine copy folder from `Maintenance`. Set `BACKUP_OFFSITE_DIRECTORY` to
copy the same verified backup to another drive or network share. Keep a
separate copy of `.env`.

For an additional server-level backup such as Cobian Reflector, include the
whole `data/` folder plus `.env`. The `data/` folder can contain SQLite sidecar
files such as `setuora.db-wal` and `setuora.db-shm` while the app is running.

Restore:

1. Open `Maintenance` as a super admin.
2. Use `Import backup` to restore a listed backup or upload a previous `.db` backup.
3. Sign in again with an account from the restored backup.
4. Check Dashboard, Products, Serials, and Reports.

Manual restore is still available when the app is stopped: copy the current
`data/` folder somewhere safe, replace `data/setuora.db`, start the app again, and
then verify the restored data.

## 12. Run Tests

```bash
pytest
```

Or:

```bash
python -m pytest
```

Expected result:

```text
All collected tests pass.
```

The current pinned dependencies are verified with Python 3.11. A Python 3.13 virtual environment may fail before tests start with the current SQLAlchemy pin.

## 13. LAN Phone Camera Setup

Phone camera access usually requires HTTPS when accessed from another device on the LAN. The Windows `scripts\setup.bat` helper can configure this automatically:

1. Run `Setuora.exe setup` as Administrator.
2. Confirm the detected LAN IP address, or enter a local DNS name that resolves to this server.
3. Install `deployment\caddy\setuora-caddy-root.crt` as a trusted CA certificate on every phone and laptop that will use Setuora.

The helper installs `CaddyServer.Caddy` with WinGet, writes and validates
`deployment\caddy\Caddyfile`, creates the automatic `SetuoraCaddy` Windows
service, and opens ports 80 and 443 to the local subnet. It also sets
`SESSION_COOKIE_SECURE=true`.

Recommended production shape:

```text
Phone browser -> https://setuora.local -> Caddy -> http://127.0.0.1:8000
```

For manual setup or troubleshooting, use:

- `docs/deployment/https-lan-guide.md`
- `deployment/caddy/Caddyfile.example`

Back up `deployment\caddy\state` with the app data, but do not share it because it contains Caddy's private keys. Only distribute `setuora-caddy-root.crt`, which is the public root certificate.

## 14. Windows Service Setup

For production, Setuora and Caddy run as automatic Windows services and recover
after process failures. Use `Setuora.exe start` and `Setuora.exe stop` for manual
control when needed.

See:

- `docs/deployment/windows-service.md`
- `deployment/windows/install_service.ps1`

The service should run:

```text
.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

Then Caddy/nginx can expose it over HTTPS on the LAN.

## 15. Useful Deployment Docs

- `docs/codex-windows-handoff.md`
- `docs/deployment/installation-guide.md`
- `docs/deployment/windows-service.md`
- `docs/deployment/https-lan-guide.md`
- `docs/deployment/user-manual.md`
- `docs/deployment/backup-restore-guide.md`
- `docs/deployment/tally-integration-guide.md`

## Troubleshooting

If login does not work:

- Confirm `.env` exists.
- Confirm the app was restarted after editing `.env`.
- Check the bootstrap username/password.

If camera does not open on phone:

- Use Chrome or Edge.
- Serve the app over HTTPS on the LAN.
- Confirm the browser has camera permission.

If Tally sync stays pending:

- Confirm Tally is open.
- Confirm Tally server mode is enabled on port `9000`.
- Open `Tally Check`.
- Confirm every required master is checked.
- Open the batch and review sync attempt details.

If the app fails to start:

- Confirm the virtual environment is active.
- Run `pip install --require-hashes -r requirements.lock`.
- Confirm port `8000` is free.
- Check that `data/` is writable.
- If using the current pinned dependencies, confirm the virtual environment is Python 3.11.
