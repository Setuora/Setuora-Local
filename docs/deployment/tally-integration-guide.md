# Tally Integration Guide

## Tally Settings

In Tally Prime:

1. Open the target company.
2. Enable Tally as a server on port `9000`.
3. Confirm inventory is maintained.
4. Confirm accounts and inventory are integrated.

In Setuora:

1. Open `Settings`.
2. Add or activate the correct company profile.
3. Enter exact company, company stock location/Godown, voucher type, ledger, GST ledger, round-off ledger, stock item, unit, and party names. The stock location defaults to `Main Location`.
   When products in the same company use different GST rates, open `Add product ledger`
   and enter the GST rate, Sales ledger, CGST ledger, SGST ledger, and IGST
   ledger. Use
   `Add product ledger` again for every additional GST rate. Products without a
   matching rate use the default Sales, CGST, and SGST ledgers.
4. Leave both automatic purchase sync and automatic sales sync off during setup.
5. Open `Tally Check`.
6. Open the company profile and click `Load from Tally`. Select a loaded Tally
   company, review its stock locations/franchises, ledger names, and dated Sales Book, then click `Use for sync`
   and save the profile. Loaded ledger names appear as exact-name choices in the
   round-off and product GST ledger fields.
7. Confirm each required master only after comparing exact spelling in Tally.
8. Use a purchase, sale, or sales-return batch page to download the generated Tally XML.
9. Validate that XML against the real Tally company.
10. Enable purchase sync, sales sync, or both only after validating the corresponding vouchers.

Changing the active company disables sync again. Recheck that company's masters before posting live vouchers.

## Current Posting Support

Live XML posting is supported for:

- Purchase/receive
- Sale
- Sales return as Credit Note

These are queued until their corresponding automatic Tally sync option is enabled.
Purchase sync controls purchase/receive batches; sales sync controls sale and
sales-return batches. Temporary failures for enabled batch types are retried in
the background.

Manual Tally Excel exports are available only to admin and super-admin users.

The following local workflows are implemented but their Tally XML is intentionally not posted yet:

- Purchase return
- Issue

They remain `PENDING_SYNC` with a clear message until the exact voucher XML is configured.

## Required Setuora Fields

Setuora requires these fields before it can generate supported Tally XML:

- company name
- sales voucher type
- purchase voucher type
- sales ledger
- purchase ledger
- CGST ledger
- SGST ledger
- round-off ledger

Product masters must also have exact Tally stock item names and units.
Each company profile must select the exact Tally Godown used for stock. Tally Check fetches `Main Location` and any additional franchise/Godown masters so an administrator can choose the company stock location.
Enter the exact customer or supplier ledger on each sale, sales-return, purchase, or receive batch.
For sales and sales returns, enter the debtor ledger as the party name and add the buyer GST
registration type, GST name, and GST number when available.

For sale and sales-return vouchers, Setuora selects the Sales, CGST, SGST, and IGST ledgers from each
product's GST rate. A single voucher can therefore contain products at multiple
GST rates while posting each amount to the correct ledger. Purchase vouchers
continue to use the default Purchase, CGST, and SGST ledgers.

## Sync Statuses

- `PENDING_SYNC`: sync is disabled, settings are incomplete, Tally is unreachable, or the voucher type is not configured for live posting.
- `FAILED`: Tally responded with a non-retryable error.
- `SYNCED`: Tally accepted the voucher.
- `CLOSED`: local-only workflows such as audit or barcode assignment completed without Tally posting.

Admins can open a batch to download XML, review sync attempts, and retry pending or failed supported batches.
