# Client Decisions Captured On 2026-06-22

## Stock Issue

- Stock issue should affect/reduce stock.
- Stock issue should also be available in an Excel-importable format.
- Admin can issue stock.
- Issue QR codes can be entered without purchase bills for initial/inward setup work.

## Existing Stock

- Current stock is to be handled as an inward operation in the software.
- Reference date mentioned: 2026-06-22.
- Existing stock can be made available in the app by inwarding/assigning QR serials.
- Need final product-wise stock quantities confirmed against Tally before doing this.

## Roles And Permissions

- QR replacement should be admin only.
- QR generation permissions need final confirmation:
  - Current app behavior: admin/super admin only.
  - Client note mentioned purchase person / sales return user, but this needs a clear yes/no before changing permissions.
- Sales return workflow is needed.
- Purchase return workflow is needed.

## Tally Examples

- Sale, purchase/stock receipt, sales return, purchase return, and stock issue/sample examples were reportedly provided on 2026-06-21.
- Two ZIP files are present:
  - `docs/context/100004.zip`
  - `docs/context/100005.zip`
- These ZIPs contain Tally company data folders, not direct Excel/XML import templates.
- Reliable extraction of exact master names still requires opening the company in Tally and exporting/copying names or voucher XML.

## Label Printing

- No price.
- No branding.
- Label should print QR plus serial number only.
- QR content remains serial number only.
- Product name/code should not be printed unless the client changes this decision.

## Server And Network

- Server has roughly 50 GB to 80 GB free.
- App should be purely local network.
- Phones should connect through factory Wi-Fi/LAN, not mobile data.
- Outside-factory access is not required.
- HTTPS/certificate setup is acceptable if needed for phone camera access.

## Backup And Support

- Server uses Cobian Reflector-style automatic backups.
- Admin can control backups.
- Preferred maintenance/update window: after 6/7 PM.

## Still Needed

- Exact Tally company name.
- Exact Tally master names copied from Tally.
- Product-wise current stock quantities as of the inwarding date.
- Confirm whether QR generation should remain admin-only or also be allowed for purchase users.
- Confirm exact Excel import column format needed for stock issue.
- Confirm Tally voucher/ledger format for sales return, purchase return, and stock issue before enabling live Tally posting for those workflows.
