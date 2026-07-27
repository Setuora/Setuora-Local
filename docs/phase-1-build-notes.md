# Phase 1 Build Notes

## Implemented scope

- FastAPI application shell
- SQLite schema
- Session-cookie authentication
- User management
- Product management
- Serial QR label generation
- Printable labels
- Purchase batches
- Sale batches
- Audit batches
- Scan history
- Reports and CSV export
- Tally settings
- Tally master readiness checklist
- Tally gateway test
- Tally XML sync attempts with queue status
- Automatic pending-sync retry worker
- Retry count and last retry tracking
- Persisted audit reconciliation findings
- XLSX scan report export
- PDF QR labels and audit reports
- SQLite-safe backup download and restore procedure
- Live-sync gate tied to Tally Check readiness
- Tally XML preview and sync-attempt request/response viewer
- Sales return, purchase return, and stock issue workflows
- Barcode assignment for existing stock
- Barcode replacement with invalid old serial and linked replacement serial
- Deployment guides and Windows service helper

## Deliberate boundaries

- Purchase return, issue, barcode assignment, and barcode replacement are active local workflows; sales return also has Tally Credit Note XML.
- Tally sync defaults to disabled to avoid posting against unknown ledger names.
- Serial-level detail stays local. Tally receives aggregate voucher quantities by product.

## Next build slice

- Real Tally validation on the client's machine.
