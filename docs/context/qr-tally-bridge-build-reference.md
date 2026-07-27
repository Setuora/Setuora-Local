# QR Scan Bridge for Tally Prime - Build Reference

Client: Swarnagowri Foods & Beverages (friend's factory, biryani masala / spice products)
Prepared by: Dijo S Benelen
Last updated: 21 Jun 2026

This consolidates the three SRS drafts, the proof-of-concept evidence (test xlsx + Tally screenshot), and the client Q&A into one working reference. Read this before opening an editor.

---

## 1. What this project actually is

Strip away the scope creep across the three drafts and the real ask is small: a phone-camera QR scanner that lets Purchase, Sales, and Audit staff record stock movement, with TallyPrime staying the system of record for accounting, GST, and inventory. The app is a transaction-capture layer, not an ERP, not a billing system, not an inventory system in its own right.

The three drafts represent three different ambition levels of the same idea:

| Doc                                | Scope                                                                                                                                   |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Revised Developer Requirement      | True MVP. 4 roles, 3 actions (receive/sell/audit), no returns.                                                                          |
| Additional Requirements & Workflow | Adds returns, QR replacement, status state machine, dashboard.                                                                          |
| SRS v1.0                           | Formalizes everything into 30 sections: Super Admin tier, multi-warehouse-ready architecture, e-way bill / GST billing as future scope. |

Build against Doc 1's workflow first. Treat Doc 2 and Doc 3 as a backlog, not a spec to build against on day one.

---

## 2. Confirmed facts (client Q&A)

| Question                                                                                       | Answer                              | Implication                                                                                                                                                                                                                               |
| ---------------------------------------------------------------------------------------------- | ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One company or multiple?                                                                       | Single TallyPrime company           | Hardcode the company name in config. No company-switching logic needed in the XML envelope.                                                                                                                                               |
| Network scope                                                                                  | LAN only, inside the factory        | No need for Tailscale, VPN, or public exposure. Solve HTTPS for camera access with a local cert only (see section 8).                                                                                                                     |
| Number of sites                                                                                | Single factory                      | Drop the Godown/multi-location dimension from the schema for now. Cheap to add later, not worth building speculatively.                                                                                                                   |
| Tally edition                                                                                  | Unconfirmed                         | Check via TallyPrime → F1 (Help) → About. Silver is fine if Tally Prime is only ever opened on the SERVER machine itself. Gold is required only if a second physical PC (e.g. the accountant's desktop) also opens the same company data. |
| Was the test voucher scripted or typed manually, and what are the exact ledger names in Tally? | Unconfirmed, message sent to friend | Blocking item. Do not start writing the XML converter until this comes back. See section 12.                                                                                                                                              |

---

## 3. Evidence from the proof of concept

The test import (`Sales_GST_Tally_Import_biryani_4.xlsx`) is a flattened ledger-row format: one row per Dr/Cr posting, with item/HSN/quantity context repeated on each row.

Columns, in order:

```
Voucher Date | Voucher Type Name | Voucher Number | Ledger Name | Ledger Amount |
Ledger Amount Dr/Cr | Item Name | HSN Code | GST Rate % | Billed Quantity |
Item Rate | Item Rate per | Item Amount | CGST Amount | SGST Amount |
IGST Amount | Round Off | Final Invoice Value | QR Serial Number | Change Mode
```

What this confirms:

- `Change Mode: Accounting Invoice` plus `Item Name` carrying through to the Tally screen means this is hitting actual Stock Items, not just ledger accounts. Quantity genuinely moves in inventory. This is the harder thing to prove and it already works.
- `QR Serial Number` exists as a column here but has no equivalent field in Tally itself. Tally has no native per-unit serial concept (batch-wise tracking exists, but it's built for lots, not 100k+ individually serialized units, and would explode master data if misused this way). **Decision: serial-level history lives entirely in the app's own database. Only aggregate quantities get pushed to Tally.**

Bug found in the sample data: the `Sales @ 5%` Cr row appears twice, with identical item and quantity (HSN even drifts from `9042110` to `9042111` between the two occurrences). That duplication is exactly why the resulting Tally voucher (image, Sales No. 4) shows two identical "Sg Biriyani Masala 100grm" lines and a footer total of 20 Pcs instead of the intended 10. Whatever step explodes one logical sale into per-ledger XML rows is double-emitting that row. Find and fix this before reusing that logic in the real converter, or it'll silently double stock-out quantities in production.

---

## 4. The design gap none of the three drafts address

All three documents describe the workflow as: scan one unit → submit → one Tally transaction. The proof of concept contradicts this: 10 individually serialized units (`CHILLY-000001`, etc.) collapsed into **one** Sales voucher to one customer, because that's what a real GST sales invoice actually is: one commercial billing event, not one unit.

If the app is built literally as 1 scan = 1 voucher:

- Tally's Sales/Purchase Register floods with hundreds of micro-vouchers a day.
- Worse: splitting one commercial sale into N separate GST invoices because N units were scanned individually is not how GST invoicing is supposed to work.

**Required fix, not in any draft:** add a session/batch concept on the Sales and Purchase sides.

- Sales User: start a sale → scan all units going to one customer → confirm party → submit once → one Tally voucher with qty = N.
- Purchase User: start a goods-receipt session → scan all units from one supplier delivery → submit once → one Tally voucher with qty = N.
- Auditor: no batching needed, audit never creates a Tally voucher at all.

This changes the Transactions table from "one row per scan" to "scans roll up into a batch, batch maps to exactly one voucher." Resolve this before finalizing the schema in section 10.

---

## 5. How the TallyPrime integration actually works

No official REST API exists. The real mechanism:

1. In TallyPrime: **F1 (Help) → Settings → Connectivity → Client/Server configuration** → set "Act as: Server" → port **9000** (default; some builds expose the same toggle under F12 → Data Synchronization).
2. The backend POSTs an XML `ENVELOPE` (`HEADER` + `BODY` + `IMPORTDATA`) to `http://<tally-host>:9000`. Tally replies with an XML response containing created/altered counts and per-line errors.
3. **Hard prerequisite:** every Ledger, Stock Item, Unit, and GST/HSN reference in the XML must already exist in Tally with an _exact_ name match (case, spacing) before import. Tally does not auto-create masters from a voucher post. This is the actual failure mode to expect in practice, not network issues.
4. Confirm company F11 features: `Maintain Inventory: Yes` and `Integrate Accounts with Inventory: Yes`. If either is off, "stock movement" silently degrades to accounting-only entries with no inventory effect.
5. Parse the XML response on every push. Capture the Tally voucher number/ID back into the local `transactions` table (the original drafts call this field "Tally Reference") so failures are traceable and a retry doesn't double-post.

**Masters checklist to push/verify before the first transaction on any new product:**

- Stock Item name (exact)
- Unit of measure (`Pcs`, must already exist in Tally exactly as written)
- HSN code matching the GST rate slab in use
- Sales/Purchase ledger names (`Sales @ 5%`, `Purchase @ 5%`, etc., exact)
- GST ledgers (`Input CGST @ 2.5%`, `Input SGST@2.5%`, note the existing data has inconsistent spacing between these two ledger names, that inconsistency must be replicated exactly or the import fails)
- Round Off ledger

---

## 6. Recommended tech stack

| Layer                  | Choice                                             | Why                                                                                                                                                                                                           |
| ---------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backend                | FastAPI + Uvicorn                                  | Already the right call in the drafts. Async-friendly, good for a thin transaction-capture layer.                                                                                                              |
| ORM                    | SQLModel or SQLAlchemy                             | Makes SQLite → Postgres/MySQL later a connection-string change, not a rewrite.                                                                                                                                |
| Database               | SQLite, WAL mode                                   | 10 concurrent users and 100k+ rows is comfortably inside SQLite's range. No need for Postgres at this scale.                                                                                                  |
| Auth                   | PyJWT + passlib[bcrypt]                            | 5 fixed roles. A JWT + role-check dependency is the entire auth layer needed. Skip a full framework.                                                                                                          |
| QR scanning (client)   | Native `BarcodeDetector` API (Shape Detection API) | Browser support is already locked to Chrome/Edge on Android plus Chrome desktop, so the native API works directly with no dependency. Falls back to `html5-qrcode` only if Safari/iOS support is ever needed. |
| QR generation (server) | `qrcode` + Pillow                                  | Standard, lightweight.                                                                                                                                                                                        |
| Label PDFs             | `reportlab`                                        | For the bulk QR label sheet export.                                                                                                                                                                           |
| Frontend               | Jinja2 templates + vanilla JS, or htmx             | The UI is a login screen, a big scan button, and a confirm screen. A React build pipeline is the most likely way this timeline balloons for no real benefit.                                                  |
| PWA                    | `manifest.json` + thin service worker              | Static-asset caching and "Add to Home Screen" only. Don't attempt offline transaction queuing client-side, the drafts already place retry logic server-side.                                                  |
| XML building           | `lxml`                                             | Easier to debug malformed envelopes than stdlib ElementTree.                                                                                                                                                  |
| Tally HTTP client      | `requests`/`httpx` with a timeout + retry wrapper  | Catch `ConnectionRefusedError` explicitly and surface "Tally Connection Failed" per the original spec.                                                                                                        |
| Background retry       | APScheduler                                        | Polls `status = PENDING_SYNC` rows every N minutes. No need for Celery/Redis at this scale.                                                                                                                   |
| Excel/CSV export       | `openpyxl`                                         | Matches the column shape already proven against this client's Tally setup.                                                                                                                                    |
| Reverse proxy / TLS    | Caddy or nginx + mkcert                            | See section 8.                                                                                                                                                                                                |
| Deployment             | Windows Service via NSSM                           | See section 9.                                                                                                                                                                                                |

---

## 7. Architecture

```
Phone (camera, Chrome/Edge Android)
        |
        v
HTTPS reverse proxy (Caddy + mkcert, LAN-only cert)
        |
        v
FastAPI backend (auth, scan logic, batch/session handling)
        |
   -----+------------------------
   |                            |
   v                            v
SQLite                    Tally XML gateway
(users, products,         (HTTP POST, port 9000)
 serials, batches,                |
 scan logs, retry queue)          v
                             TallyPrime
                          (stock + GST ledgers)
```

Failed pushes stay in SQLite as `PENDING_SYNC` and get retried by the APScheduler job against the same Tally gateway endpoint.

---

## 8. Camera access over HTTPS (LAN-only deployment)

Phones will not grant camera access (`getUserMedia` / `BarcodeDetector`) over plain HTTP on a LAN IP, only on `https://` or `http://localhost`. A laptop hitting `localhost` during dev will work fine and mask this until the first real phone test.

Since deployment is confirmed LAN-only (factory network, no external access needed):

1. Run Caddy or nginx in front of FastAPI on the SERVER machine.
2. Generate a cert with `mkcert` for the server's LAN IP or a hostname (e.g. `swarnagowri.local`).
3. Install the mkcert root CA on each staff phone once (one-time, a couple of minutes per device).
4. No Tailscale, no public exposure, no Let's Encrypt needed given the confirmed LAN-only scope.

---

## 9. Deployment target (the SERVER machine)

Specs observed: i7-4770 (Haswell), 32GB RAM, 233GB SSD with 212GB already used (~21GB free), Windows 11.

- CPU/RAM: not a constraint for this workload at all. Ignore this axis.
- Disk space: the real constraint. ~21GB free needs to absorb Tally's growing data file, the app, logs, and OS updates. Get a clear read on this before deployment, or it surfaces mid-project as a failed Windows Update on a nearly full boot drive.
- Run the FastAPI app as a Windows Service (NSSM is the simplest wrapper), not a console window someone has to remember to leave open. If the machine reboots after a power cut, the app and Tally's gateway-server setting need to come back without manual intervention.

---

## 10. Database schema (first cut)

Reflects the batching fix from section 4 and the decision to keep serials local-only from section 3.

```
users
  id, username, password_hash, role, active, created_at

products
  id, product_code, product_name, hsn, gst_rate, unit, tally_stock_item_name

serials
  id, serial_number (unique), product_id, status, created_at
  status: GENERATED | RECEIVED | IN_STOCK | SOLD | RETURNED |
          PURCHASE_RETURN | ISSUED | AUDITED | DAMAGED | MISSING | REPLACED

batches
  id, batch_type (RECEIVE | SALE | PURCHASE_RETURN | SALES_RETURN | ISSUE),
  party_name, user_id, status (DRAFT | SUBMITTED | SYNCED | PENDING_SYNC | FAILED),
  tally_voucher_type, tally_voucher_number, created_at, synced_at

batch_items
  id, batch_id, serial_id, quantity, rate, remarks

scan_logs
  id, serial_id, user_id, action, batch_id (nullable for audit-only scans),
  date, time, status

settings
  tally_host, tally_port, company_name
```

Audit scans write directly to `scan_logs` with no `batch_id` and never touch Tally, per the original spec (verification only, no stock movement).

---

## 11. Build order

1. **Phase 0**: master-sync tool. Push/verify Stock Item, Unit, Ledger, HSN masters exist in Tally with exact names before any transaction screen is usable. This is the single highest-value piece of defensive tooling given section 5.
2. **Phase 1 (MVP)**: Doc 1's workflow, with the batching fix from section 4. Login, scan, Receive/Sell/Audit, Tally push, retry queue, scan history. No returns, no replacement, no audit reconciliation.
3. **Phase 2**: returns (sales return, purchase return), QR replacement, simple dashboard. From Doc 2.
4. **Phase 3**: audit session reconciliation (expected vs scanned, missing/extra items), reports/export polish, anything from Doc 3's "future expansion" list (batch/expiry tracking, multi-warehouse) only if actually requested.

Get Phase 1 running against real Tally data for at least a week with real users before starting Phase 2.

---

## 12. Outstanding items before writing code

- [ ] Confirm with the friend: was the test voucher typed manually, or does a script/macro already push Excel data into Tally? (message drafted and sent)
- [ ] Get the exact, copy-pasted (not retyped) ledger names from his Tally: sales ledger, CGST input ledger, SGST input ledger, stock item name.
- [ ] Check Tally edition via F1 (Help) → About. Confirm whether any machine other than SERVER ever opens this company in Tally Prime; if yes, Gold is required.
- [ ] Confirm free disk space on SERVER and a plan if it's tight.
- [ ] Confirm F11 company features: Maintain Inventory = Yes, Integrate Accounts with Inventory = Yes.
- [ ] Decide and confirm with the friend: does one "Sale" in the app always correspond to one customer/one invoice (per section 4), or does the business sometimes want partial/split invoicing from a single scan session? Get this in writing before building the Sales screen.

---

## 13. Risk checklist (quick reference)

- Master name mismatch (Tally rejects the import) - mitigated by Phase 0 master-sync tool.
- Scan-to-voucher mismatch inflating invoice count - mitigated by the batching/session model in section 4.
- Camera access silently failing on real phones over LAN HTTP - mitigated by mkcert + reverse proxy in section 8.
- Tally edition mismatch if a second PC ever opens the company - confirm via section 12.
- Disk space exhaustion on SERVER - confirm via section 12.
- Reusing the buggy duplicate-row logic from the proof of concept - fix before building the real converter, see section 3.
