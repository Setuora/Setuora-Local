# Codex Windows Handoff

Read this first when continuing the project on the Windows SERVER machine.

## Project Summary

Setuora Barcode Tally Bridge is a LAN-only FastAPI app for Swarnagowri Foods & Beverages. It lets staff scan product barcode serials from phones, keeps serial-level traceability in SQLite, and syncs supported stock movements to Tally Prime through the XML gateway.

Tally remains the master for accounting, GST, stock reports, and inventory valuation. The app is a transaction capture and traceability layer.

## Current Build State

Implemented:

- Login and role-based access
- Product master
- Serial QR label generation
- Printable/PDF QR labels
- Purchase/receive, sale, audit, sales return, purchase return, stock issue, and barcode replacement workflows
- Batch pricing and voucher preview with GST split, round off, and final value
- Tally XML generation for purchase/receive, sale, and sales-return batches
- Tally Check master-readiness page
- Editable admin role access controls for pages, actions, and data areas
- Live sync gate: Tally sync cannot be enabled until Tally Check is complete
- Pending sync queue, manual retry, and automatic retry worker
- Audit reconciliation: verified, missing, extra
- CSV/XLSX report export
- PDF QR labels and PDF audit reports
- SQLite-safe backup download
- Deployment docs and Windows service helper

Run the complete current test suite using Python 3.11 and the hash-verified
dependency lock. The release is ready only when:

```text
All collected tests pass.
```

## Windows Setup Commands

Recommended first-time setup from PowerShell in the project folder:

```powershell
.\scripts\setup.bat
```

Normal startup after setup:

```powershell
.\scripts\start_setuora.bat
```

Stop Setuora, including the Windows service when installed:

```powershell
.\scripts\stop_setuora.bat
```

Manual fallback:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
copy .env.example .env
```

If activation is blocked:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Start the app:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

Before the first start, set:

```text
APP_SECRET_KEY
BOOTSTRAP_ADMIN_PASSWORD
SESSION_COOKIE_SECURE=true
TRUSTED_HOSTS=<your-lan-hostname-or-ip>,127.0.0.1,localhost
```

The application refuses to initialize its first admin account with an empty,
placeholder, or default password.

## Important Files

Core app:

- `app/main.py`
- `app/models.py`
- `app/database.py`
- `app/routers/`
- `app/services/`
- `app/templates/`
- `app/static/`

Important service modules:

- `app/services/inventory.py` - serial state rules, batch creation, barcode generation
- `app/services/voucher.py` - GST/taxable/final value calculations
- `app/services/tally.py` - Tally XML generation and sync attempts
- `app/services/tally_masters.py` - Tally Check readiness logic
- `app/services/audit.py` - audit reconciliation
- `app/services/exports.py` - XLSX/PDF export generation
- `app/services/backup.py` - SQLite backup download
- `app/services/replacement.py` - barcode replacement
- `app/services/sync_worker.py` - pending sync retry loop

Docs:

- `README.md` - setup and usage
- `docs/context/qr-tally-bridge-build-reference.md` - original project interpretation
- `docs/context/client-decisions-2026-06-22.md` - latest client answers
- `docs/deployment/` - install/service/HTTPS/backup/Tally guides

Deployment helpers:

- `deployment/windows/install_service.ps1`
- `deployment/caddy/Caddyfile.example`

## Current Client Decisions

Captured from latest discussion:

- Stock issue should reduce stock.
- Stock issue should be exportable/importable in an Excel format.
- Admin can issue stock.
- Existing/current stock should be inwarded into the software.
- Reference date for existing-stock inwarding: 2026-06-22.
- Barcode replacement is admin-only.
- Labels should show QR code plus serial number only.
- No price on labels.
- No branding on labels.
- QR payload should remain serial number only.
- App should stay purely local network.
- Phones should use factory Wi-Fi/LAN, not mobile data.
- Outside-factory access is not required.
- HTTPS/certificate setup is acceptable if needed for phone camera access.
- Server has roughly 50 GB to 80 GB free.
- Server backups are handled with Cobian Reflector-style automatic backups.
- Admin can control backups.
- Preferred update window is after 6/7 PM.

See full notes:

```text
docs/context/client-decisions-2026-06-22.md
```

## Tally ZIP Files

Two ZIPs are present:

```text
docs/context/100004.zip
docs/context/100005.zip
```

They contain Tally company data folders, not direct Excel/XML templates. Do not assume exact ledger/master names from these files by parsing binaries. The reliable path is:

1. Open the company data in Tally.
2. Copy exact company, ledger, stock item, unit, and voucher type names.
3. Export or screenshot real vouchers.
4. Use those details in Setuora Settings and Tally Check.

## Tally Sync Status

Live XML posting is supported for:

- Purchase/receive
- Sale
- Sales return as Credit Note

Implemented locally but not live-posted yet:

- Purchase return
- Stock issue

Purchase return and stock issue update local serial status but remain `PENDING_SYNC` with a clear “Tally XML is not configured” message until real Tally voucher formats are validated.

Do not enable Tally sync until:

- Tally company name is exact
- Voucher type names are exact
- Ledger names are exact
- Product stock item names are exact
- Unit names are exact
- Tally Check has no missing/unchecked items
- A purchase/sale/sales-return XML has been validated against the real company

## Pending Client Data

Still needed:

- Exact Tally company name
- Exact stock item names for every product
- Exact ledger names
- Exact voucher type names
- Product-wise current stock as of 2026-06-22
- Product list with HSN/GST/unit/rates
- Real Tally sale voucher details
- Real Tally purchase voucher details
- Real Tally sales return voucher details
- Real Tally purchase return voucher details
- Real Tally stock issue/sample voucher details
- Exact Excel import column format needed for stock issue
- Final yes/no: should barcode generation remain admin-only or also be allowed for purchase users

## Role Rules In Current Code

Current behavior:

- Product creation: admin/super admin
- Serial QR label generation: admin/super admin
- Barcode replacement: admin/super admin
- Purchase/receive: purchase/admin/super admin
- Sale: sales/admin/super admin
- Audit: auditor/admin/super admin
- Sales return: sales/admin/super admin
- Purchase return: purchase/admin/super admin
- Issue: admin/super admin
- Retry sync: admin/super admin
- Reports/settings/users/maintenance: admin/super admin

Client has mentioned purchase person / sales return user in relation to barcode generation. This is not yet changed in code because it needs explicit confirmation.

## Existing Stock Handling

Existing stock should be brought into the app as an inward/setup operation.

Current app-supported approach:

1. Admin creates product masters.
2. Admin generates serial QR labels for existing stock.
3. During generation, use `Existing stock` status so serials start as `IN_STOCK`.
4. Physical labels are applied to current stock.

Before doing this, get final product-wise quantities confirmed against Tally.

## Label Format

Current implemented label format:

- QR code
- Serial number text
- No product name
- No product code
- No price
- No branding
- 48.5 mm x 25.4 mm labels, 4 columns by 11 rows on A4

QR payload:

```text
SERIAL_NUMBER_ONLY
```

## Network Plan

The target is local-only:

```text
Phone browser -> factory Wi-Fi/LAN -> HTTPS local hostname -> Caddy/nginx -> FastAPI on 127.0.0.1:8000
```

Phone camera access usually requires HTTPS when not using localhost. See:

```text
docs/deployment/https-lan-guide.md
deployment/caddy/Caddyfile.example
```

## Backup Plan

The app has a `Maintenance` page for SQLite-safe manual backup download.

Client/server also uses Cobian Reflector-style automatic backup. Keep `.env` backed up separately because it is not inside the SQLite database.

Restore procedure is documented in:

```text
docs/deployment/backup-restore-guide.md
```

## Test Commands

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Linux/macOS:

```bash
.venv/bin/python -m pytest
```

Template parse check:

```bash
python - <<'PY'
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('app/templates'))
for path in sorted(Path('app/templates').glob('*.html')):
    env.get_template(path.name)
    print('ok', path.name)
PY
```

## Do Not Accidentally Do This

- Do not post purchase-return/issue XML to Tally until real voucher XML is validated.
- Do not infer exact Tally master names from screenshots or memory.
- Do not expose the app publicly unless the client explicitly asks.
- Do not put price, GST, customer data, or product data inside the QR payload.
- Do not reset or delete `data/setuora.db` without backing it up.
- Do not revert existing uncommitted work unless the user explicitly asks.
