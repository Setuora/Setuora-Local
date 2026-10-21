# Setuora Local

> LAN-first barcode operations and Tally Prime integration for a single site.

## Overview

Setuora Local lets staff scan product barcodes from phones, maintains
serial-level inventory history in a local SQLite database, and posts supported
stock movements to Tally Prime through its XML gateway.

It is designed for a Windows server on the same LAN as staff devices and Tally.

## Status

The supported production deployment is Windows with `Setuora.exe`, automatic
Setuora and Caddy services, and LAN-only HTTPS. Python 3.11 is the verified
runtime for the current dependency lock.

Purchase, sale, and sales-return vouchers support live Tally posting. Purchase
returns and stock issues are recorded locally but remain pending until their
client-specific voucher XML is validated.

## Features

- Role-based access for administration, purchase, sales, and audit users
- Product masters with HSN, GST, unit, rate, discount, and exact Tally names
- Bulk serial generation and calibrated QR label printing
- Batch, manufacturing date, expiry date, warehouse, and franchise tracking
- Purchase, sale, audit, return, issue, barcode assignment, and replacement flows
- FEFO picking and expiry controls
- GST calculation, round-off, pricing, and voucher previews
- Tally master readiness checks, company profiles, and access controls
- Pending sync queue with manual and automatic retries
- Audit reconciliation, dashboards, analysis, Excel exports, and PDF reports
- Verified SQLite backup and restore workflows

## Architecture

```text
Staff phone or workstation
  -> LAN HTTPS
  -> Caddy
  -> Setuora Local / SQLite
  -> Tally XML gateway on the LAN
  -> Tally Prime
```

The application listens on loopback in production. Caddy terminates HTTPS for
approved LAN clients, while Setuora communicates with the configured Tally
company and port.

## Repository Structure

```text
Setuora-Local/
|-- app/                         FastAPI application
|   |-- routers/                Page and API route handlers
|   |-- services/               Business logic and Tally integration
|   |-- static/                 Browser assets
|   `-- templates/              Jinja templates
|-- deployment/                 Windows service and Caddy configuration
|-- docs/                       Deployment, user, and handoff guides
|-- installer/                  Source for the Windows control executable
|-- scripts/                    Setup and lifecycle workflows
|-- tests/                      Pytest coverage
|-- data/                       Runtime database and backups, ignored by Git
|-- logs/                       Runtime logs, ignored by Git
|-- Setuora.exe                 Windows setup and control tool
|-- requirements.lock           Hash-verified dependency lock
`-- .env.example                Configuration template
```

## Requirements

- Windows server with administrator access during setup
- Python 3.11 for the pinned dependency set
- Tally Prime running on the server or reachable on the LAN
- Chrome or Edge on staff phones and workstations
- A reserved LAN address or reviewed local DNS name
- Internet access during the initial installer workflow

## Installation

For a new Windows server, run `Setuora.exe` and choose **Start**. Approve the
administrator prompt. The installer installs missing prerequisites, checks out
the official `main` branch under `C:\Setuora`, creates the environment, installs
verified dependencies, configures HTTPS, starts the Setuora and Caddy services,
and opens the browser.

Double-click `Setuora.exe` afterward for the controls menu:

- **Start** installs Setuora or finishes an interrupted installation. On an
  installed copy it fast-forwards a clean checkout when GitHub is reachable,
  then runs and verifies Setuora and opens the browser. When GitHub is
  unreachable or the checkout has local changes, it warns and starts the
  current version.
- **Stop** stops Setuora and the HTTPS proxy and disables their automatic
  startup, so they stay stopped after Windows restarts until you choose Start.
- **Uninstall** stops Setuora, keeps a checksum-verified copy of `.env`, the
  database and backups, logs, and the Caddy HTTPS state in
  `C:\ProgramData\Setuora\Recovery\Local`, then removes the services, firewall
  rule, HTTPS root certificate, and application folder. Python and Git remain.

Each action requests administrator approval. The same actions are available as
`Setuora.exe start`, `stop` and `uninstall`; `setup`, `repair` and `update`
remain available for scripted maintenance.

Use `Setuora.exe setup --with-caddy=false` only when a separately reviewed HTTPS
proxy already exists. Complete the
[production release checklist](docs/deployment/production-release-checklist.md)
before live use.

## Configuration

Setup creates `.env` interactively. For manual development, copy `.env.example`
to `.env` and review at least:

```dotenv
APP_SECRET_KEY=replace-with-a-long-random-secret
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-unique-password
DATABASE_URL=sqlite:///./data/setuora.db
SESSION_COOKIE_SECURE=true
TRUSTED_HOSTS=setuora.local,127.0.0.1,localhost
```

The first administrator cannot be created with an empty, placeholder, or
default password. Changing bootstrap values after the database exists does not
change an existing account.

Inside the application:

1. Create or activate a company profile in **Settings**.
2. Enter the exact Tally company, Godown, voucher type, ledger, GST, and
   round-off names.
3. Keep purchase and sales sync disabled during initial validation.
4. Create product masters using the exact Tally stock-item names.
5. Complete **Tally Check** and validate downloaded test XML.
6. Enable only the Tally flows that have passed validation.

Switching the active company disables Tally sync so the new profile can be
checked before posting.

## Usage

After the first login, create named users and grant only the required roles,
company profiles, ledgers, and Tally usernames.

Typical inventory flow:

1. Open **Batches** and choose Purchase, Sale, Audit, Return, or Issue.
2. Enter the counterparty, location, or reference.
3. Scan serials or use FEFO picking where available.
4. Review item state, pricing, GST, round-off, and voucher preview.
5. Submit the batch and review its local and Tally sync status.

Use **Barcodes > Assignment** to generate labels from a product and quantity or
an Excel upload. Use **Barcodes > Replacement** to retire a damaged serial and
print its replacement.

Supported live Tally XML posting:

- purchase/receive
- sale
- sales return as Credit Note

Purchase returns and stock issues remain `PENDING_SYNC` until their exact Tally
voucher XML is approved for the target company.

## Operations

Use `Setuora.exe` **Start** and **Stop** for routine service control, and
`repair` or `update` for maintenance. A direct `update` refuses a dirty worktree, install the verified lock, run tests,
and restore the prior running or stopped state. Runtime settings, databases,
and backups are preserved.

For backups, use **Maintenance > Download backup** or configure automatic
verified backups. The default schedule is every 24 hours with the newest 14
retained. Set `BACKUP_OFFSITE_DIRECTORY` for a second copy on another drive or
network share. Keep a separate protected copy of `.env`.

Phone camera access requires LAN HTTPS. Install only the public Caddy root
certificate, `deployment\caddy\setuora-caddy-root.crt`, on approved devices.
Never distribute the private Caddy state directory.

## Development

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
cp .env.example .env
uvicorn app.main:app --reload
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
copy .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` for local development.

## Testing

Run the test suite from an activated Python 3.11 environment:

```bash
python -m pytest
```

All collected tests must pass before an update or release. Python 3.13 may fail
before test collection with the current SQLAlchemy pin.

## Security

- Serve staff access through HTTPS and restrict ports 80/443 to the approved LAN.
- Keep the application bound to `127.0.0.1` behind Caddy.
- Do not expose Tally port 9000 beyond the required trusted LAN path.
- Protect `.env`, the database, backups, logs, and Caddy private state.
- Use named accounts and least-privilege roles; disable unused accounts.
- Validate exact Tally masters and test XML before enabling live sync.

## Documentation

- [Installation guide](docs/deployment/installation-guide.md)
- [Windows services](docs/deployment/windows-service.md)
- [LAN HTTPS](docs/deployment/https-lan-guide.md)
- [User manual](docs/deployment/user-manual.md)
- [Backup and restore](docs/deployment/backup-restore-guide.md)
- [Tally integration](docs/deployment/tally-integration-guide.md)
- [Production release checklist](docs/deployment/production-release-checklist.md)
- [Installer build and options](installer/README.md)

## Troubleshooting

- **Login fails:** confirm `.env` exists, restart after configuration changes,
  and use an account already stored in the current database.
- **Camera access fails:** use Chrome or Edge over the configured HTTPS LAN URL
  and grant camera permission.
- **Tally sync remains pending:** confirm Tally server mode on port 9000, open
  the intended company, complete Tally Check, and inspect the batch sync details.
- **The application does not start:** confirm Python 3.11, reinstall from
  `requirements.lock`, verify port 8000 is free, and check `data/` permissions.
- **A Windows service fails:** run `Setuora.exe repair`, then review the runtime
  logs and the [Windows service guide](docs/deployment/windows-service.md).
