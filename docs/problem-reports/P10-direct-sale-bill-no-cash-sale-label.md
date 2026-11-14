# P10 — Booking-delivery sale showed `CASH-SALE` in the Bill No column

**ID and title:** P10 — a sale posted against a booking item saved its bill
number (`SB-SL-####`) but the Sales list showed `CASH-SALE` in **Bill No**.

**Module:** Sales → Direct Sales (`templates/direct_sales.html`,
`app/blueprints/sales/_direct_sales_direct_sales_page.py`).

**Page/screen:** Sales → Direct Sales (`/direct_sales`), the sales table, first
column **Bill No**.

**URL/route:** GET `/direct_sales` (`direct_sales_page`) renders the table;
the row comes from POST `/add_direct_sale` (`add_direct_sale`).

**Problem:** A sale created with sale type *Booked Sale* (category
`Booking Delivery`) is saved with an auto/system bill number such as
`SB-SL-1005`. In the list, **Bill No** showed the grey badge `CASH-SALE` and
the real number appeared only under **System Ref**. The operator read this as
"the bill number I saved was replaced by a cash-sale label" and could not match
the row to the bill number they had just been given.

**Expected behavior:** The Bill No column names the bill that was actually
saved — the manual number when one was typed, otherwise the auto/system number.
`CASH-SALE` is a placeholder only for a row that genuinely has no number.

**Evidence:** Confirmed by a regression test against the real routes. With a
client, an open booking for the material, and a `Booking Delivery` POST to
`/add_direct_sale`, the stored sale had `manual_bill_no = NULL` and
`auto_bill_no = 'SB-SL-1000'`, while the rendered row's Bill No cell was:

```
<span class="badge bg-secondary ...">CASH-SALE</span>
```

Testing used a throw-away SQLite database created by `tests/conftest.py`; no
live data was touched.

**Impact:** Display only. No stored amount, stock movement, ledger row or bill
number was changed or lost — the reports that read `auto_bill_no` were correct
all along. The impact was on the operator: the row looked unbilled/labelled as
a cash sale while its Status column said `BILLED`.

**Reports/screens affected:**
- Direct effects: Sales → Direct Sales table on `/direct_sales` (Bill No and
  System Ref columns).
- Not affected: `view_bill`, PDF/print downloads, Client Ledger, Mixed
  Transactions report, pending bills, stock and profit reports. They read the
  stored numbers, which were always right.

**Proposed scope:** Fix the template's Bill No cell so it falls back to
`sale.auto_bill_no` before the placeholder, and keep System Ref showing the
system number once (not duplicated in the same row). No route, model or schema
change.

**Safety checks:**
- A manual bill number still leads the column and keeps its `MB NO.` prefix.
- A linked invoice number still renders as before.
- The `CASH-SALE` badge is still produced for a legacy row with neither number.
- `filter bill_state=billed|unbilled`, the billed/unbilled counters, and every
  download/print link are untouched.
- No change to any financial formula or stored value.

**Acceptance tests:** `tests/test_direct_sale_bill_no_display.py`
1. `test_booking_delivery_shows_its_system_bill_no_in_bill_no_column` — the
   saved `SB-SL-####` appears in Bill No and `CASH-SALE` does not.
2. `test_manual_bill_no_still_leads_the_bill_no_column` — a typed number stays
   in Bill No, System Ref keeps the auto number, and it is not repeated.
3. `test_cash_sale_without_any_number_keeps_the_cash_sale_placeholder` — a
   numbered cash sale shows its number; only a numberless legacy row shows
   `CASH-SALE`.

## Root cause

`templates/direct_sales.html` rendered the Bill No cell from
`sale.manual_bill_no` and `sale.invoice.invoice_no` only, and then emitted the
`CASH-SALE` badge when neither existed. `auto_bill_no` was rendered exclusively
in the next cell (System Ref). A `Booking Delivery` sale never gets an invoice
(`create_invoice` is forced off for that category in
`app/blueprints/sales/_direct_sales_add_direct_sale.py`), and the operator had
typed no manual number, so both conditions were false and the placeholder
replaced the real number.

## Changes made

- `templates/direct_sales.html` — Bill No cell: after the manual number and the
  invoice number, render `sale.auto_bill_no` when nothing has been shown yet;
  the `CASH-SALE` badge remains as the final fallback. Added an explanatory
  comment. System Ref keeps showing `auto_bill_no` unconditionally.
- `tests/test_direct_sale_bill_no_display.py` — new regression tests (three
  cases above) posting through the real `/add_direct_sale` route and reading
  the rendered row from `/direct_sales?show=all`.

## Validation

- `python -m pytest tests/test_direct_sale_bill_no_display.py -q` → 3 passed.
- `python -m pytest -q` (full suite) → **278 passed, 5 skipped**
  (5 skips are the pre-existing optional-dependency skips).
- Manual render check through a disposable DB: booking delivery without a
  manual number → Bill No `SB-SL-1000`, System Ref `SB-SL-1000`; with
  `MB NO.777` → Bill No `MB NO.777`, System Ref `SB-SL-1001`.

## Data and deployment safety

- No schema change, no migration, no data rewrite, no backfill.
- Template-only change: a restart/reload of the web app is enough
  (PythonAnywhere: reload the web app).
- Existing rows need no correction; the numbers were always stored correctly,
  so previously confusing rows start showing their real bill number after the
  reload.

## Rollback

Revert the commit or restore the previous Bill No cell in
`templates/direct_sales.html` and delete
`tests/test_direct_sale_bill_no_display.py`. No business data is affected by a
rollback.

## Status

Fixed locally, validated, not yet deployed.

## Remaining limitations

- `templates/mixed_transactions.html` builds its View-bill link from
  `sale.manual_bill_no or sale.auto_bill_no` without a fallback; a legacy row
  with neither number would still produce a broken link there. Not part of this
  report.
- Sales whose Bill No cell shows the auto number still list the same number in
  System Ref (as requested: the reference stays visible). No de-duplication of
  those two columns was attempted beyond not repeating it when a manual/invoice
  number is present.
