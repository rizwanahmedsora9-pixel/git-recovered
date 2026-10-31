# AMS ERP — Complete Rebuild Specification

**Document type:** Plain-English reverse-engineering and rebuild instructions  
**Target:** A working functional replica (“Xerox”) of the existing AMS ERP  
**Scope:** All active modules, data models, business rules, workflows, screens, APIs, imports/exports, and operational behavior found in the inspected checkout  
**Date of inspection:** 2026-10-31  
**Audience:** An AI coding agent or engineering team rebuilding the application from scratch

---

## 0. How to use this document

1. Read Sections 1–5 first. They define the product, global rules, UI contract, runtime architecture, and data-model invariants. Every later module depends on them.
2. Build in the order given in Section 18. Do not start with reports; reports are projections of the transaction ledgers and will be wrong if the posting rules are not implemented first.
3. Treat the **active behavior** sections as the functional contract. Treat Section 17 (“Known quirks, dormant features, and limitations”) as compatibility notes. Do not accidentally promote dormant or broken legacy features into working modules.
4. The complete physical data dictionary is in Appendix A. The complete registered URL map is in Appendix B. Use them as checklists, not as a substitute for the workflow rules.
5. This document intentionally contains **no source code and no real legacy customer or financial values**. Any example names or amounts in this specification are synthetic placeholders.
6. “Must” means required for functional parity. “Should” means required for UI/UX parity unless a later explicit product decision changes it. “May” means optional implementation detail.

### Fidelity priorities

| Priority | Requirement |
|---|---|
| 1 | Financial and stock effects of every posted document must match the original. |
| 2 | Edit, void, restore, hard-delete, and reconciliation paths must reverse or reapply exactly the same effects. |
| 3 | Ledger, payable, cash-flow, and profit reports must read the same authoritative projections; they must not invent independent accounting. |
| 4 | Screen layout, navigation, filters, modals, themes, and export behavior must match the original user experience. |
| 5 | Legacy compatibility fields, import formats, and dormant tables must be preserved or explicitly migrated, not silently dropped. |

---

## 1. Product definition

### 1.1 What the application is

AMS is a single-store, server-rendered ERP for a building-materials trading business (cement, steel, related goods, and service charges such as rent/delivery). It combines:

- **Inventory:** goods receipt (GRN), stock movements, dispatch, returns, FIFO lot consumption.
- **Sales:** bookings (advance orders), direct sales in five categories, material returns, pending bills, invoices.
- **Money:** client payments, supplier payments, driver/delivery-person settlements, financial accounts (cash/bank/wallet/ledger), transfers, expenses, cash-flow tracking, daily cash/bank reconciliation.
- **Ledgers and reports:** client, supplier, driver, material, decision ledger, current payables, profit & loss with FIFO costing.
- **Collections:** pending-bill follow-up queue, risk scoring, reminders, contact logging.
- **Masters:** clients, client categories, suppliers, materials, material categories, services/charges, delivery persons, accounts, users, settings.
- **Operations:** import/export center, full-database snapshot sync, legacy migration templates, data lab reconciliation, admin diagnostics, backups, wipes, audit logs.

The business timezone is **Asia/Karachi (PKT)**. The currency is Pakistani Rupees, displayed as “Rs.” and stored internally in integer paisa.

### 1.2 What the application is not

- It is **not** a working multi-tenant SaaS. Tenant-named routes and columns are legacy remnants; the tenant guard always returns 404 in single-store mode. See Section 17.
- It is **not** a full double-entry general ledger with journal entries, trial balance, or balance sheet. “Accounts” are named balance holders plus a transaction ledger; P&L is a separate read-only projection.
- It is **not** a rental-management product in the active UI. Rental tables and export modules exist, but no rental blueprint is registered. See Section 17.
- It does **not** use a client-side SPA framework. Pages are Jinja templates with Bootstrap, vanilla JavaScript, and progressive enhancement.

### 1.3 Actors

| Actor | Capabilities |
|---|---|
| Administrator (`admin`) | Full access; bypasses feature flags; only role allowed into Import/Export Center and account master CRUD. |
| Root (`root`) | Treated like admin by permission checks; legacy root-recovery and tenant routes exist but tenant routes are disabled. |
| Standard user | Access controlled by 23 feature flags, an access mode (read/write vs read-only), and a backdate-restriction flag. |
| System / background workers | Audit stamps, import jobs, optional reconciliation worker, optional backup scheduler (embedded scheduler disabled by default). |

There is no public customer portal. Every business page requires login.

---

## 2. Global rules that apply to every module

### 2.1 Time and dates

- All business timestamps are **naive datetimes representing Asia/Karachi local time**. Do not store UTC and convert only at display time; the original stores PKT wall-clock values.
- “Now” means current PKT date and time. “Today” means current PKT calendar date.
- Date-only posting rule used by most forms:
  - No date supplied → current PKT timestamp.
  - Date-only equal to today → current PKT timestamp.
  - Date-only in the past → that date at 00:00:00.
  - Full datetime-local value → keep the selected date and time exactly.
- GRN has a richer parser: it accepts date-only, time-only, 24-hour time, 12-hour AM/PM time, several date separators, and combined date-time strings. Invalid GRN date/time input must be rejected with a readable error, never silently replaced by the server clock.
- Future-dated transactions are rejected by the canonical payment services. Some legacy forms fall back to “now” on unparseable dates; preserve that only where explicitly noted.
- Report periods are inclusive calendar dates. A transaction belongs to a day by its PKT calendar date.
- Entry (stock movement) stores date and time as separate text fields in `YYYY-MM-DD` and `HH:MM:SS` form.

### 2.2 Money

- The authoritative money representation is **integer minor units (paisa)**. Legacy float columns remain for compatibility and must be kept synchronized.
- Conversion: parse to a finite decimal, quantize to two decimals using **round-half-up**, multiply by 100. Negative zero becomes zero.
- Line gross = quantity × rate, rounded **once** to whole paisa.
- Document discount is split across lines using the **largest-remainder method** so the shares sum exactly to the document discount.
- Automatic discount splits apply to **goods lines only**. A service/charge line keeps its full value unless the user explicitly selects it for discount.
- A line discount can never exceed that line’s gross value.
- Discounts are **stored on the line at posting time**. Reports must read the stored value, never recompute a proportional split with floating division.
- Returns reverse the original line’s stored discount proportionally to the returned quantity.
- Display: thousands separators, two decimals where precision matters; dashboard KPI tiles may show whole rupees. Percentages are rounded to two decimals where produced.
- Quantity may be fractional; money may not be fractional below paisa.

### 2.3 Bill numbers and identifiers

- Auto bill format: `SB-<NAMESPACE>-<sequence>`. Sequence starts at **1000** per namespace.
- Namespaces: BK (booking), CP (client payment), SP (supplier payment), SL (direct sale), RTN (material return), GRN (goods receipt), EN (entry). Default/unknown namespace is GEN.
- Allocation must be concurrency-safe: take a write lock, synchronize the counter with the maximum existing sequence across all bill-bearing tables for that namespace, skip any value that conflicts with an existing bill, then increment.
- Manual bill format: `MB NO.<value>`. Numeric-only manual values are normalized to integers (no `.0`).
- Bill references are checked for conflicts across bookings, payments, supplier payments, sales, returns, GRNs, entries, invoices, and pending bills. Pending bills are derivative: if a primary source exists, the pending bill does not create a second collision.
- Client codes: `FBMCL-#####`. Material codes: `FBM<category>-######`; names beginning with `FT-` use `FTP<category>-####`. Supplier display code is derived from the database id as `SUP-%04d`; there is no supplier code column.
- Open Khata is a special client with code `OPEN-KHATA` and name `OPEN KHATA`. It is created automatically if missing.
- Source markers are embedded in notes to link derived rows to their origin, for example `[SRC:Payment:<id>]`, `[SRC:DirectSale:<id>]`, `[SRC:CashFlow:<id>]`, `[AUTO_GRN_PAY:<id>]`. These markers are part of the compatibility contract.

### 2.4 Active, voided, deleted, and suspended

- Most transactional tables have `is_void`. A voided row is hidden from active reports but retained for audit.
- Some newer paths follow a **“no voided rows” hard-delete policy**: reverse all financial/stock effects, write audit records, then delete the row. The legacy URL `/void_transaction` always hard-deletes.
- Masters use `is_active`. Suspended masters remain in history but cannot be selected for new transactions. Existing documents keep their historical references.
- “Deleted by Edit” is a special audit classification: when an edit creates a replacement row and voids the old one, the void-audit page detects the newer active twin and labels the old row accordingly.
- Never physically delete a master that is referenced by history unless the specific hard-delete service explicitly allows it and reverses dependent effects.

### 2.5 Source-of-truth hierarchy

| Question | Authoritative source |
|---|---|
| Physical stock quantity | Non-voided Entry rows (IN minus OUT), excluding services. |
| FIFO cost of a sale | Recorded GRNAllocation rows first; reconstructed FIFO only for issues with no allocation. |
| Client balance | Client financial-ledger projection (bookings, sales, payments, discounts, waives, cancellations, opening balance). |
| Supplier balance | Supplier ledger projection (GRN totals, payments, refunds, opening balance, legacy paid_amount only when no auto payment exists). |
| Driver balance | Driver ledger projection (opening + rent allocations + legacy DeliveryRent when no allocation − paid − waived). |
| Account balance | Account.balance_minor, maintained by AccountTransaction effects and adjustments/reconciliations. |
| Cash-flow rows | Derived from AccountTransaction, payments, sales, GRN payments, cash drawer, etc.; manual CashFlowEntry rows mirror into AccountTransaction. |
| Pending bill amount | Derived from the client ledger obligation when traceable; standalone PendingBill rows only when untraceable. |
| Profit | Read-only P&L projection; never a stored running total. |

### 2.6 Transactions, audit, and idempotency

- A document post must be one database transaction: header, lines, stock entries, allocations, payments, account transactions, pending-bill rebuild, and audit all commit together or roll back together.
- Every mutating HTTP request is audited with actor, endpoint, path, and status. Domain services additionally write structured accounting audit records with before/after snapshots, amounts, accounts, parties, and reason.
- Canonical payment and driver-settlement services accept an **idempotency key**. Replaying the same key with the same payload returns the existing row; reusing the key with a different payload is rejected.
- Direct sales additionally use a payload fingerprint and a short replay window to suppress double-click duplicates while still allowing legitimate repeat orders later.
- Payment edits carry an expected revision number; a stale revision is rejected with “changed in another session.”
- Closed reconciliation periods are immutable: creating or financially changing a payment inside a closed period is rejected.

### 2.7 Permissions

- 23 editable feature flags, grouped into five UI groups: Operations; Sales & Billing; Ledgers & Reports; Masters; Finance & System.
- Admin and root bypass all flags.
- A user also has an **access mode**: read/write or read-only. Read-only blocks every save/edit/delete even if the feature flag is on.
- A per-user **restrict back-dated GRN editing** flag blocks non-admin users from creating/editing GRNs dated before today.
- Endpoint-to-permission mapping is explicit; the Accounts blueprint has a fallback requiring either manage-accounts or manage-payments. Account master create/edit/delete/toggle remains admin-only inside the views.
- Import/Export Center is admin-only regardless of the import/export flag. Legacy Migration allows admin/root or the import/export flag.
- The complete flag list and grouping are in Section 6.7 and Appendix B.

---

## 3. User interface contract

### 3.1 Application shell

- Fixed left sidebar on desktop; off-canvas drawer with a mobile header on small screens.
- Sidebar groups, in order:
  1. **Dashboard**
  2. **Inventory:** Stock Summary, Daily Breakdown, History
  3. **Transactions:** GRN (Receiving), Bookings, Sales, Material Return, Pending Bills, Delivery Rent
  4. **Accounts:** Payments, Accounts, Cash Flow, Daily Reconciliation, Financial Tracker, Pay Supplier, Profit Reports
  5. **Ledger:** Supplier Ledger, Client Ledger, Client Categories, Delivery Person Ledger, Decision Ledger, Current Payables, Notifications
  6. **Directory:** Material Brands, Services & Charges, Delivery Persons
  7. **System:** Import & Export, Legacy Data Migration, Settings
- Groups are collapsible. The group containing the active page opens automatically and shows an active dot. Submenu items highlight when active.
- Sidebar footer shows the current user’s initial avatar, username, role, a theme toggle, and Logout.
- Main content area is fluid width with small horizontal padding. Flash messages appear as dismissible Bootstrap alerts at the top of the content area.
- Every mutating form carries a hidden CSRF token. Fetch/XHR requests are patched to send the same token in a header.

### 3.2 Theme

- Dark theme is the default. Light theme is available.
- Preference order: browser localStorage key `ams_theme`, then server session preference, then system preference.
- Dark surfaces: deep navy (`#0f172a`, `#1e293b`, `#020617`), light text (`#f8fafc`), muted text (`#94a3b8`), amber accent (`#fbbf24`).
- Light surfaces: near-white (`#f4f7fb`, `#ffffff`), dark text (`#0f172a`), amber/brown accent (`#d97706`).
- UI v2 pages add a root class and use cleaner tokens: 10 px card radius, 16 px large radius, 40 px field height, 24 px page gutter, subtle shadows, status colors green/amber/rose/cyan/violet.
- Tables, cards, modals, dropdowns, and form controls must all respond to the theme. Do not hard-code white backgrounds inside dark mode.

### 3.3 Shared components

| Component | Required behavior |
|---|---|
| Page header | Icon + title + subtitle on the left; toolbar buttons on the right; wraps on narrow screens. |
| KPI tile | Label, large value, icon, colour accent, sub-caption, and a drill-down link. Whole tile is clickable. |
| Data-table card | Card header with title/icon and optional toolbar; responsive table body; sticky header where used; empty-state row when no data. |
| Forms | Labels above inputs; 38–40 px control height; required fields marked; inline validation messages; submit buttons show loading state and disable while posting. |
| Modals | Bootstrap modals; edit/view modals are loaded on demand from fragment endpoints and must never be cached. A stuck-overlay watchdog offers “Close & reload.” |
| Searchable selects | Combobox with debounced text search, keyboard navigation, hidden id field, and “no matches” state. Client search accepts name or code. |
| Date/time pickers | Flatpickr or native inputs; default to today/now in PKT; GRN accepts typed overrides. |
| Pagination | Page controls preserve all active filters; page size selectors where specified (10/20/50/100). |
| Toasts/alerts | Success, warning, info, danger. Mutating flashes are suffixed with the acting username. |
| Loading overlay | Global overlay for slow navigation; manual close/reload escape hatch. |
| Confirmation dialogs | Required for destructive actions (delete, void, wipe, lock/unlock day, supplier transfer). |

### 3.4 Dashboard

- Title “System Dashboard” with today’s date.
- KPI tiles: Total Inventory, Registered Clients, Daily Cash Received, Daily Due Amount, Total Outstanding, Accounts Hub, Cash Flow, Cash Flow Differences (last three gated by payment/cash-flow permissions).
- Quick-action links to daily breakdown, GRN, sales, bookings, payments, etc.
- “Current Stock by Brand” table: material name, IN, OUT, closing stock, unit. Every material master row appears even with zero movement; entry-only materials appear explicitly; services are excluded. Rows drill into that material’s daily breakdown.
- Daily cash = today’s non-void client payments + booking paid amounts + sale paid amounts. Daily credit = today’s booking unpaid + sale unpaid. Total outstanding comes from the consolidated current-payables projection, not from summing pending bills.

### 3.5 Reference screen 1 — Daily Cash & Bank Reconciliation

Reproduce this layout exactly in structure:

1. Header card: shield/check icon, title “Daily Cash & Bank Reconciliation,” subtitle “Verify actual counted cash/bank balances against ledger expected closing.” Right side: date picker and refresh button.
2. Status banner: green when open (“FINANCIAL DAY STATUS: OPEN FOR RECONCILIATION”) with a “Verify & Lock / Close Day” button; locked state must show locked styling and an unlock action.
3. Three KPI cards: Total Expected Ledger Balance, Total Actual Counted Balance, Net Difference. Counted value is visually emphasized (violet); difference is green at zero, red/amber otherwise.
4. Account positions table titled “ACCOUNT POSITIONS FOR <date>” with columns: Account Name, Opening, In, Out, Transfer In, Transfer Out, Expected Closing, Actual Counted, Difference, Action.
5. In/Transfer In are green with plus signs; Out/Transfer Out are red with minus signs. Actual Counted is an editable numeric field or “Edit Count” action. Difference shows “0 (RECONCILED)” when zero.
6. Saving a count updates the row and totals without a full page reload when requested via fetch; the server returns the refreshed board context.

### 3.6 Reference screen 2 — Financial Tracking Filter Matrix

Reproduce this layout exactly in structure:

1. Header: funnel icon, “FINANCIAL TRACKING FILTER MATRIX,” right-side “Reset Filters” and “Apply Filters” buttons.
2. Filter grid, four columns on desktop:
   - Row 1: Date Range Preset, Direction / Type, Category, Account.
   - Row 2: Client, Supplier, Partner, Worker / Labour.
   - Row 3: Vehicle, Payment Method, Text / Ref Search.
3. Four summary cards: Filtered Total In (green), Filtered Total Out (red), Net Movement (green/red), Matching Records count.
4. Export bar: “Export Filtered Financial Report” with Download PDF Report, Export Excel (.xlsx), Export CSV. Exports must use the exact active filters.
5. Collapsible “FILTERED CATEGORY & ACCOUNT BREAKDOWN” with two side-by-side tables: Category Summary (Category, Type, Total Amount) and Account Summary (Account, In, Out, Net).
6. Transaction table below: Date & Time, Type, Account, Category, Party, In, Out, Transfer, Reference, Description.
7. The same matrix is embedded as the “Transactions” tab inside Cash Flow, with parameter names prefixed to avoid collisions.

### 3.7 Error and empty states

- Never expose SQL, stack traces, driver URLs, or parameter bindings to the user. Replace internal errors with: “The operation could not be completed due to a data error. No changes were saved — please retry, and contact support if the problem persists.”
- Validation errors are specific and actionable (e.g., “Insufficient balance in the selected account,” “Not enough booked quantity,” “Select a cash account for this payment method”).
- Empty tables show a friendly empty row, not a blank card.
- Upload-too-large errors state the configured maximum in MB.
- CSRF failures return a readable “Invalid or expired form token. Reload the page and try again.”

---

## 4. Runtime architecture and operational contract

### 4.1 Stack

- Python 3, Flask 3 application factory, Flask-Login, Flask-SQLAlchemy / SQLAlchemy 2, SQLite.
- Jinja2 templates, Bootstrap 5.3, Bootstrap Icons, Flatpickr, vanilla JavaScript modules.
- pandas + openpyxl for spreadsheets; WeasyPrint for PDFs with ReportLab fallback.
- No external message broker. Background work uses threads plus database job rows and filesystem locks.

### 4.2 Bootstrap sequence

1. Resolve schema version (default v44) and database path. Default file name is the v44 fresh database; retired legacy file names are redirected, never silently reopened.
2. Create instance directories: logs, storage/backups, storage/temp, temporary import uploads/reports.
3. If the database file is missing or zero bytes, remove orphan WAL/SHM/journal sidecars and create an empty SQLite file. If it exists, leave it untouched.
4. Load or generate a persistent secret key in the instance folder.
5. Configure session cookies: HTTP-only; Secure/SameSite=None only under HTTPS; on plain HTTP LAN, Secure is forced off and SameSite falls back to Lax so login survives redirects. Permanent session lifetime 14 days; remember cookie 30 days.
6. Enable SQLite foreign keys and busy timeout on every connection. Journal mode defaults to WAL on local disks and falls back to DELETE on network filesystems.
7. Register core blueprints first, then auto-discover feature modules, then request hooks, then import-job routes.
8. Warm the Jinja template cache.
9. Create missing ORM tables and columns, run compatibility backfills, ensure default admin and Open Khata client, create performance indexes.
10. Apply numbered SQL migrations from the migrations directory, recording each in `migration_history` and stamping the highest numeric prefix into `schema_version`. Destructive statements are blocked unless explicitly allowed.
11. Optionally start the embedded maintenance scheduler (disabled by default).

A bootstrap or migration failure must be logged loudly; the original continues serving with a visible bootstrap error rather than crashing the process. A rebuild should preserve that resilience but must not hide data corruption.

### 4.3 Request hooks

- Session-bound CSRF protection on every POST/PUT/PATCH/DELETE, including login.
- Concurrent login sessions are allowed; session protection is disabled so the same user can work from several LAN PCs. Each request touches the login-session record.
- HTML responses are `no-cache`; on-demand modal fragments are `no-store`.
- Iframe embedding is allowed (the app is designed to run inside preview frames and LAN wrappers). Optional open-CORS mode exists for integrations.
- Mutating requests get an audit entry and an actor-stamped flash message.
- Request timing is logged with method, path, status, and duration.
- Shared dropdown data (active clients, materials, services, delivery persons, settings) is cached for 20 seconds and injected into every authenticated template. A database failure here degrades to empty dropdowns instead of breaking every page.

### 4.4 Files and storage

- Permanent uploads (bill photos, payment photos) live under the configured upload directory and are served through controlled routes.
- Import artifacts live under instance temporary directories with a configurable retention period (default 7 days).
- Backups contain the SQLite database plus permanent uploads, with a manifest and SHA-256 hashes. They are staged, validated, atomically published, and then retention-pruned. Source code, logs, caches, and Git data are excluded.
- Full-database snapshots (`.amsdb`) are portable SQLite copies with a metadata table; they are the recommended transport format.

### 4.5 Background work

- Import jobs: uploaded → queued → processing → completed/failed/cancelled, with progress percentage, ETA, and history entries.
- Optional auto-reconciliation worker: scans for ledger/account drift at a configurable interval (default 600 s), tolerance 0.01, and may auto-fix when enabled.
- Embedded backup scheduler is disabled by default; backups are explicit maintenance operations.
- Filesystem locks prevent duplicate backup/reconciliation work across processes; stale locks are recovered only after age and owner-liveness checks.

---

## 5. Data model — structure and invariants

The complete column-level dictionary is Appendix A. This section defines the relationships and rules that must survive any rebuild.

### 5.1 Model groups

| Group | Tables |
|---|---|
| Identity & settings | user, user_login_session, settings, schema_version, system_lock, root_recovery_code |
| Masters | client, client_category, supplier, material_category, material, service, delivery_person, account_category, account |
| Bill numbering | bill_counter |
| Stock & purchasing | grn, grn_item, grn_allocation, entry, delivery, delivery_item |
| Sales & bookings | booking, booking_item, booking_allocation, booking_allocation_repair_archive, direct_sale, direct_sale_item, direct_sale_draft, invoice, material_return, material_return_item, sale_delivery_persons, delivery_rent |
| Money | payment, supplier_payment, delivery_person_payment, waive_off, pending_bill, account_transaction, account_reconciliation |
| Cash flow | cash_flow_category, cash_flow_subcategory, cash_flow_party, cash_flow_entry, cash_flow_entry_audit, cash_flow_difference_adjustment, cash_flow_reconciliation_audit, cash_day_lock, cash_day_account_position, fbm_cash_drawer_category, fbm_cash_drawer_entry |
| Collections | staff_email, follow_up_reminder, follow_up_contact |
| Audit & repair | audit_log, accounting_audit_log, future_account_audit_log, tenant_wipe_backup_history |
| Import & migration | import_upload, import_job, import_history_entry, migration_run, migration_row, migration_mapping, migration_history (non-ORM) |
| Data lab | recon_basket |
| Dormant rental | fbm_client, fbm_rental_item, fbm_rental |
| Dormant backup email | root_backup_settings, root_backup_email_history |

Counts: **71 ORM models / 71 ORM tables**, plus the non-ORM `migration_history` table created by the migration runner (72 physical tables in a bootstrapped database).

### 5.2 Critical relationships

- Client 1—N Booking, DirectSale, Payment, PendingBill (by code), MaterialReturn; Client 1—N Account via linked_client_id.
- Supplier 1—N GRN, SupplierPayment; Supplier 1—N Account via linked_supplier_id.
- MaterialCategory 1—N Material. Material and Service are parallel masters resolved by name/code; they must never collide.
- GRN 1—N GRNItem; GRNItem 1—N GRNAllocation; DirectSale 1—N DirectSaleItem; DirectSaleItem 1—N GRNAllocation and BookingAllocation.
- Booking 1—N BookingItem; BookingItem 1—N BookingAllocation; BookingAllocation links a sale item to a booking item.
- DirectSale 1—N SaleDeliveryPerson (driver allocations); SaleDeliveryPerson 1—N DeliveryPersonPayment. Legacy DeliveryRent links a sale to a driver by name.
- Account 1—N AccountTransaction as from_account and/or to_account; Account 1—N AccountReconciliation; Account 1—N CashDayAccountPosition.
- PendingBill 1—N FollowUpReminder; FollowUpReminder 1—N FollowUpContact; PendingBill may also carry contacts directly.
- Payment may link to a MaterialReturn via source_type/source_id; SupplierPayment may link to a GRN the same way.
- Invoice links to DirectSale; a sale may exist without an invoice (unbilled cash).
- ImportUpload 1—1 ImportJob; ImportJob 1—N ImportHistoryEntry.
- MigrationRun 1—N MigrationRow; MigrationMapping records legacy reference → new entity.

### 5.3 Money columns

Every financial table that stores money has a legacy REAL column and an authoritative integer minor-unit mirror (for example amount / amount_minor, balance / balance_minor, paid_amount / paid_amount_minor). Writes must set both. Reads in new logic must prefer the minor column and fall back to the float only for legacy rows.

### 5.4 Source linkage columns

Derived rows carry source_module, source_table, source_id, source_bill_no, and transaction_type where applicable. Lookups must try the explicit source link first, then fall back to bill-reference matching scoped by client code/name. Never match across clients on bill number alone.

### 5.5 Normalization rules

- Bill numbers are normalized on every flush: auto bills to `SB-NS-####`, manual bills to `MB NO.value`.
- Client lookup order: exact code → exact name → name with `(code)` suffix → case-insensitive partial match.
- Material resolution: an explicit material id is authoritative. If the id is stale and the text was edited, reject the line; never silently create a new material from a sale line.
- Service detection: exact Service name/code match, plus a word-boundary “rent” rule for legacy names. Substring matches like “transparent” or “current” must not be treated as rent.
- Material normalization key: lowercase, strip, remove non-alphanumeric characters. This key is used for FIFO matching and collision checks.

### 5.6 Indexes and uniqueness

- Unique: user.username, client.code, material.code, material.name (case-insensitive intent), service.name/code, bill_counter.namespace, auto bill numbers per bill-bearing table, manual bill numbers where enforced, idempotency keys.
- Performance indexes on date_posted, client_code/client_name, bill_no/auto_bill_no, source links, Entry(date, type, material), GRNAllocation(grn_item_id, sale_id), BookingAllocation(booking_item_id, sale_item_id), account transaction dates and account ids.
- SQLite foreign keys must be enforced. A rebuild may use a different database only if it preserves the same constraints and transactional behavior.

---

## 6. Master data modules

### 6.1 Clients

**Screen:** `/clients` — two paginated tables (Active and Suspended), 10 rows each, search by name/code, category filter. Summary cards show visible-page payable, advance, billed, and received totals. Each row shows current balance, status label, last transaction/payment dates, and obligation count derived from the client ledger.

**Fields:** code (auto), name, phone, address, category, financial book/page, cement book/page, steel book/page, legacy book number, location URL, page notes, opening balance and opening balance date, active flag.

**Rules:**
- Add: name required; code auto-generated; duplicate name/code rejected; category defaults to General.
- Edit: changes are audited. Renaming a client must not rewrite historical free-text names unless the explicit transfer/reclaim operation is used.
- Suspend/activate: suspended clients cannot be selected for new transactions; history remains.
- Delete: blocked when referenced; use transfer/reclaim for reattribution.
- Opening balance: positive = client owes us (debit); negative = we owe client / advance (credit). The opening date controls whether it is included in a dated projection.
- Client categories: General is protected and cannot be renamed/deleted/deactivated. A category in use cannot be deactivated or deleted. Custom categories can be created, renamed, toggled, and deleted when unused.
- Transfer client: moves Entry, PendingBill, and WaiveOff rows by client code to another client. This is a partial reattribution; it does not move bookings/sales/payments. Reclaim reverses a transfer using the audit payload.

### 6.2 Suppliers

**Screen:** `/suppliers` — searchable list with derived display code, contact info, opening balance, current payable from the supplier ledger, and actions.

**Rules:**
- Add/edit name, phone, address, opening balance. Suspended suppliers cannot receive new transactions.
- Delete is blocked when any GRN lot has been consumed. The hard-delete service purges GRNs, payments, and legacy IN entries only when no consumption exists, unlinks accounts, and rebuilds totals.
- Opening balance edits are audited and dated.
- Supplier ledger shift and transfer are separate operations; see Section 12.4.

### 6.3 Materials and categories

**Screen:** `/materials` (“Material Brands”) — paginated by code, filter by category and unit, merge modal, category management.

**Rules:**
- Add: name required and unique; code auto-generated from category if blank; unit defaults to Bags; category defaults to General. Name/code collisions with Services are rejected in both directions.
- Edit: name, code, category, unit, unit price, active flag. Renaming cascades the free-text label into Entry, BookingItem, DirectSaleItem, GRNItem, MaterialReturnItem, DeliveryItem, and ReconBasket.
- Merge: choose source and target; cascade all historical labels to the target name, then delete the source master. Quantities and costs are not re-priced.
- Delete: blocked when referenced by history; otherwise removed.
- Bulk unit update and activate-all actions exist.
- Categories: create, rename, toggle, delete when unused. Materials with no category are backfilled to General.
- Material total is a cached quantity rebuilt from non-void Entry rows; it is not an independent source of truth.

### 6.4 Services & charges

**Screen:** `/services` — list with usage counts, search, and a “pending rent migration” panel showing rent-like materials and orphan rent labels that could be converted.

**Rules:**
- Services are billed like materials but never move stock, never appear in GRN, and never appear in stock reports.
- Add/edit/rename/merge/delete/toggle require the manage-materials permission.
- Name/code collisions with Materials are rejected in both directions, including punctuation-normalized variants.
- Rename/merge cascades the free-text label into historical sales, bookings, entries, and returns.
- Delete is blocked when historical rows reference the name.
- “Migrate rent to services” converts rent-like Material rows (and orphan rent entry names) into Service rows without changing historical data, because lines are name-keyed.
- Service lookup uses a 30-second in-process cache that must be invalidated on any service mutation.

### 6.5 Delivery persons

**Screen:** `/delivery_persons` — list with active flag, opening balance, and ledger link.

**Rules:**
- Add/edit name, phone, vehicle/details, active flag, opening balance and date.
- Suspend blocks new allocations; history remains.
- Opening balance positive = we owe the driver.
- Driver ledger and settlement are in Section 12.3.

### 6.6 Accounts (financial)

**Screens:** `/accounts/` dashboard, `/accounts/accounts` manage list, add/edit forms, transaction groups, receipts, expenditures, transfers, reconciliations.

**Classification registry (authoritative):** Category → Subcategory → Account Type → allowed channel(s) + required linked entity.

| Category | Subcategories |
|---|---|
| Assets | Cash; Bank; Digital Wallet; Client Receivables; Advances Given; Other Assets |
| Liabilities | Supplier Payables; Client Advances; Loans Payable; Partner / External Payables; Expenses Payable; Other Liabilities |
| Equity / Own Funds | Owner Capital; Partner Capital; Owner Drawings / Withdrawals; Retained Funds; Other Equity |
| Income | Sales Income; Service Income; Other Income |
| Expenses | Operating Expenses; Delivery & Transport; Salaries & Labour; Rent & Utilities; Administrative Expenses; Other Expenses |
| External / Clearing | Partner Clearing; Intercompany / Company Transfer; External Settlement; Suspense / Temporary Clearing |

- Channels: cash, bank, digital_wallet, ledger_only, other. Most ledger classifications force ledger_only; cash/bank/wallet types force their instrument; a few allow a choice.
- Linked entity types: none, client, supplier, partner, worker, vehicle, party. Client/supplier links are real foreign keys; the others are free-text names.
- Status: active, inactive, archived. Only active accounts can be selected for new transactions.
- Channel details: cash → location + responsible person; bank → bank name, account title, account number/IBAN, branch; wallet → provider, wallet number, holder. Changing channel clears incompatible stale details.
- Required validation: bank name/title/number for bank; provider/number for wallet; linked entity when the classification demands one. The server re-validates every dropdown; the browser cascade is only a convenience.
- Legacy columns (source_category, account_type, type, category) are kept synchronized from the new classification so older payment/transfer logic keeps working. Digital wallets behave like bank for payment-method matching.
- Opening balance: amount + direction (debit = money we have, credit = money we owe) + effective date. Editing the opening balance shifts the current balance without creating an adjustment row.
- Physical balance edits create an Adjustment transaction with a required reason from: Physical cash verification, Bank reconciliation, Historical correction, Data migration correction, Opening position correction, Accounting correction, Other.
- Account master CRUD is admin-only. Transaction groups (AccountCategory) can be created/renamed/toggled/deleted when unused.

### 6.7 Users, settings, and permissions

**Screen:** `/settings` — general company settings, material categories, user list, feature-by-user matrix, add/edit user modals, change password, recent activity, backup download, reconciliation scan/fix, and selected-data wipe.

**General settings:** company name, address, phone, currency symbol, always-allow-negative-stock flag.

**User fields:** username, role (standard/admin), status, access mode (read/write or read-only), password hash, 23 feature flags, restrict-backdated-GRN flag.

**Permission groups and flags:**

| Group | Flags |
|---|---|
| Operations | view dashboard; manage GRN; view stock; view daily; view history |
| Sales & Billing | manage bookings; manage sales; manage payments; manage pending bills; view delivery rent |
| Ledgers & Reports | view client ledger; view supplier ledger; view decision ledger; view reports; view cash flow |
| Masters | manage clients; manage suppliers; manage materials; manage delivery persons |
| Finance & System | manage accounts; manage notifications; import/export; access settings |

- New standard users get sensible defaults: viewing and core transaction flags on; directory management, import/export, settings, accounts, and cash flow off.
- Read-only mode blocks all mutations regardless of flags.
- Passwords are stored as Werkzeug hashes. Legacy plaintext fallbacks are upgraded to hashes on successful login and then cleared.
- Login is case-insensitive on username, always uses a remember-me session, records a login session, and redirects only to same-origin relative next URLs.
- Root recovery codes exist as a legacy emergency path; they are single-use and audited.
- Activity log shows recent audit entries. Void audit shows deleted/suspended records with restore actions where supported.


---

## 7. Purchasing and inventory workflows

### 7.1 GRN (Goods Receipt Note) — create

**Screen:** `/grn` — a three-step wizard plus a history table with per-lot consumption indicators.

**Step 1 fields:** supplier (searchable, auto-creates a new supplier if typed), date, time, payment type (Credit / Cash / Bank Transfer), pay-from account, bank name, account holder, account number, “Use current time” button.

**Step 2 fields:** item search/scan (materials only; services are rejected), selected item, cost price, quantity, Add/Reset, and a line table (Item, Quantity, Price, Total, Action).

**Step 3 fields:** loading expense, freight expense, other expense, adjustment amount, discount, purchase tax %, purchase tax amount, purchase tax type (Asset / Expense / Purchase(Adv Tax)), supplier invoice no., internal bill no., bill date, due date, paid amount, comments, photo.

**Posting rules:**
1. Resolve or auto-create the supplier. Normalize the manual bill; reject conflicts. Allocate the next GRN auto bill.
2. Parse date/time with the GRN parser. Enforce the backdate restriction for non-admin users.
3. If paid amount > 0 and the method is cash/bank, require an active account of the matching category, validate sufficient balance, and copy bank details from the account. Credit leaves no account link.
4. Create the GRN header. For each line with quantity > 0: reject services, create a GRNItem, increase the material cached total, and create an Entry IN stamped with the GRN auto bill and the same date/time. Zero/negative quantity lines are skipped with a warning, not saved.
5. GRN total = sum of active line quantity × price + loading + freight + other expense + tax − discount + adjustment.
6. If paid amount > 0 and a supplier exists, create or update exactly one automatic SupplierPayment marked `[AUTO_GRN_PAY:<id>]`, with source_type GRN, and synchronize its AccountTransaction. Moving a paid GRN into a closed reconciliation period is rejected.
7. Commit everything in one transaction. A duplicate manual bill at the database level is treated as a duplicate submission and rejected without creating a second GRN.

**History table:** filters by start/end date and supplier/bill search; sort by date or supplier; columns include lot-consumption expander, GRN number, date/time, supplier, items, total quantity, discount, actions (view, edit, delete, print). The consumption indicator is read-only and comes from the shared FIFO projection (Section 7.3).

### 7.2 GRN — edit and delete

**Edit:** `/edit_grn/<id>` reuses the wizard.
- Backdate policy applies to the stored date and to any newly submitted date.
- If any lot is locked by a cash/credit sale, **stock-affecting changes are blocked**: item quantity, rate, add/remove item. Header-only edits (supplier, freight, note, photo, paid amount, dates) are allowed only when the submitted item lines exactly match the stored lines.
- On a full edit: reverse old stock, delete old GRNItems and old IN entries, then rebuild lines and entries from the form. Preserve GRNItem ids where possible so linked sale lines keep their lot reference.
- On a header-only edit: keep items/entries, but synchronize entry timestamps if the GRN date/time changed.
- Re-validate account category and balance. When the account is unchanged, only the **increase** in paid amount must be covered by the current balance; when the account changes, the full new paid amount must be covered.
- Re-sync the automatic supplier payment; void the old supplier’s auto payment if the supplier changed. Closed-period violations roll back the whole edit.
- Bill date and due date are saved on edit when the form carries them; an omitted key keeps the old value, an empty value clears it.

**Delete:** blocked when any lot is locked/consumed. Otherwise the hard-delete service removes IN entries, allocations, items, and the automatic supplier payment, reverses stock, and keeps audit records.

### 7.3 FIFO lot consumption

- One GRNItem is one FIFO cost lot.
- The authoritative replay combines: (1) recorded GRNAllocation rows from posted sales, then (2) reconstructed FIFO for physical issues with no allocation (legacy/imported dispatches, booking supplies, standalone OUT entries).
- Lot order: GRN date, then GRN id, then item id. A lot is available from the calendar day its GRN was posted.
- Material returns restore quantity to the lots the original dispatch consumed, proportional to the cost layers used. An untraceable return becomes a dated return layer for costing only; it never inflates a GRN’s original received quantity.
- Per lot report: received, consumed, remaining, consumption percent, and status (recorded / reconstructed / untouched / unknown). Colour bands: low < 25%, moderate < 50%, high < 75%, critical < 100%, complete at 100%.
- The GRN page, the P&L stock breakdown, and the consumption API must all use the **same** projection. Filtering the displayed GRNs must not filter the replay, or older lots would appear unconsumed.
- A lot is locked when any active sale allocation consumes it. Locked lots block GRN deletion and stock-affecting GRN edits.

### 7.4 Standalone dispatch (Entry)

**Screen:** `/dispatching` — date, type (IN/OUT), client, material/service, quantity, bill number, nimbus/vehicle reference, driver, note, optional invoice controls.

**Rules:**
- Standard users cannot add back-dated records; only today is allowed. Admins may backdate.
- OUT requires a driver name; the driver is auto-created if new.
- OUT to an unknown client is rejected with a redirect to Direct Sale (“for cash customers, use the Direct Sale form”).
- OUT to a known client validates against the booking pool: booked − dispatched (excluding pure cash/credit direct sales) must cover the requested quantity. Services may be dispatched against booked rent.
- An OUT with no nimbus value is auto-tagged “Booking Delivery” when fulfilling a known client.
- Create the Entry; update the material cached total only for real materials (never services). IN adds, OUT subtracts.
- IN entries may create an Invoice when a bill number or create-invoice flag is present. OUT entries do not create invoices because they fulfil bookings that already have financial records.
- Clients flagged as requiring a manual invoice cannot dispatch OUT without a bill number or invoice.

**Edit Entry:** non-admins may edit only today’s entries. The old stock effect is reversed and the new effect applied. Pending bills are **not** auto-rewritten from OUT edits (to avoid overwriting booking bills with partial dispatch amounts).

**Delete Entry:** non-admins may delete only today’s entries. Deletion sets the void flag and reverses stock; it does not hard-delete.

**Import dispatch:** accepts the dispatch template, resolves/creates clients and materials according to the chosen missing-client strategy, creates OUT entries, and records discrepancies. See Section 14.

### 7.5 History / tracking

**Screen:** `/tracking` — requires at least one filter before showing rows. Filters: start/end date, client, material, bill number, category, search, type (IN/OUT/CANCEL/PAYMENT), has-bill (billed/unbilled).

- Merges Entry rows and client Payment rows into one chronological list, 15 per page.
- Shows voided rows as well as active rows (filterable by type); each row carries a best bill reference, source type, unit rate when available, and void status.
- “Has bill” treats UNBILLED- and #-prefixed references as unbilled.
- Category filters match either the entry’s stored client category or the client master category.
- Rows link to bill view, edit, and delete actions according to permissions.
- `/mixed_transactions` is a redirect to tracking filtered by Mixed Transaction.

### 7.6 Stock summary and daily transactions

**Stock Summary:** `/inventory/stock_summary` — per material: opening, IN, OUT, closing, computed from non-void Entries. Services are excluded. Includes GRN receipt history joined by the automatic bill reference. Filters by date and material; export available.

**Daily Breakdown:** `/inventory/daily_transactions` — merges Entries and Payments for a selected day, 50 per page, with filters and active/voided/all modes. This is the operational day view, distinct from the financial Cash Flow page.

---

## 8. Sales workflows

### 8.1 Bookings

**Screen:** `/bookings` — filters (client, bill, date range, rows per page, active/voided/all), table (manual bill, auto bill, client, material, qty, total, actually paid, notes, actions), and an Add Booking modal.

**Add Booking fields:** client (name or code), repeatable item rows (material/service, qty, unit rate, line total), total amount, paid now, receive method (Cash/Bank), receive-into account, booking date/time, discount + reason, auto bill (read-only preview), manual bill, photo, photo URL, notes.

**Posting rules:**
1. Client must exist; otherwise reject with “add client first.”
2. Every line with quantity > 0 must have rate > 0 and resolve to an active Material or Service.
3. Split the header discount across lines once (goods-only automatic split; services only when explicitly selected) and store each line’s discount.
4. Create the Booking and BookingItems. Pending amount = max(0, amount − discount − paid).
5. Synchronize the derived PendingBill from the booking source (never increment a stale snapshot).
6. If paid now > 0, post a Receipt AccountTransaction into the selected cash/bank account using the marker `[SRC:Booking:<id>]`. **Do not create a second client Payment row.** Validate account category and active status. Legacy bookings with paid-now but no account are left untouched on edit unless the paid amount increases.
7. Commit and redirect to the bill download.

**Edit Booking:**
- Lines are updated **in place** so delivered allocations keep their foreign keys.
- Cannot reduce a line below already-delivered quantity.
- Cannot change the material of a line that has been delivered.
- Cannot remove a delivered line; the related sale must be voided/corrected first.
- Re-store line discounts from the saved header discount and lines.
- Re-sync the pending bill, including voiding the old bill reference when it changed.
- Re-sync the paid-now receipt; require an account when the paid amount increases.
- Preserve the original timestamp when the submitted datetime is unchanged at minute precision.

**Booking cancellation (from the client material ledger):**
- Remaining = booked − delivered + booked returns. Deliveries consume oldest booking first (FIFO); the cancel UI lists leftovers newest-first.
- The user may select specific material rows. Preflight: a fully-cancelled line with an active BookingAllocation blocks the whole operation.
- For each selected line: write a CANCEL Entry with nimbus “Booking Cancel” and a note carrying rate and amount; shrink the booking item quantity or delete it when fully cancelled; archive any void allocations before deleting the line.
- Recompute each touched booking’s amount from its remaining lines; rebuild or delete its pending bill.
- Revert: a CANCEL entry can be reverted by parsing the stored rate/amount from its note, restoring the booking item quantity and amount, and removing the cancellation entry.

### 8.2 Direct sales — categories and posting

**Screen:** `/direct_sales` (“Sales”) — filters (client, bill, billed state, category, material, rows), two quick-add tiles (Billed Sales / Unbilled Cash), table (bill no., system ref, status, client, date/time, material, qty, total, driver rent, notes, actions), Add Sale modal, Hold Bills drawer.

**Sale types (UI labels → stored categories):**
| UI label | Stored category | Meaning |
|---|---|---|
| Booked Sale | Booking Delivery | Fulfil existing bookings only; no money due. |
| Booked + Due | Mixed Transaction | Some booked fulfilment plus a chargeable credit portion. |
| Due Sale | Credit Customer | Pure chargeable credit to a registered client. |
| Open Khata | Open Khata | Walk-in credit under the shared OPEN-KHATA master. |
| Cash Sale | Cash | Fully paid walk-in or registered sale; may be unbilled. |

**Add Sale fields:** category, client (or manual client name for Cash/Open Khata), delivery persons (repeatable: person, bags, rent amount), sale date/time, item rows (material/service, optional GRN lot, optional alternate material, qty, rate, ignore-booking toggle, line total), rent charged to client, actual delivery rent, rent difference, auto bill (assigned on save), manual bill, photo/URL, notes, total amount, discount + reason, paid now, payment method, account, allow-negative-stock override.

**Posting pipeline (must be implemented in this order):**
1. Validate payment account category and require an account when paid > 0. Clear payment linkage when paid = 0.
2. Idempotency: reuse the form key; reject a reused key with a different payload; suppress keyless double-posts within a short replay window using a payload fingerprint.
3. Resolve the client. Compute per-material booking pool = booked − delivered + booked returns. Pure cash/credit/open-khata lines never consume the booking pool.
4. For each item row: resolve Material/Service by id or text; split into a booked slice (rate 0) up to the available pool and a chargeable slice for the remainder. Alternate materials are allowed only on booked slices and physically ship the alternate material.
5. De-duplicate exact repeated lines, then expand chargeable goods lines into FIFO lot slices, reserving lots across lines of the same document. Services and booked slices are never lot-allocated.
6. Require a rate for every non-booked slice (fallback to material unit price, else reject).
7. Validate stock for the **delivered** material (including alternate fulfilments), aggregated across the whole document, unless the per-sale override or global negative-stock setting is enabled.
8. Enforce category policy:
   - Booking Delivery: client must have a booking balance; all lines must be booked; amount, paid, discount forced to 0; no invoice; no cash tracking.
   - Mixed: must include both booked and chargeable portions with amount > 0.
   - Credit Customer: registered client; no booked lines.
   - Open Khata: manual customer name required; not a registered client; no invoice; no cash tracking; ensures the OPEN-KHATA master exists.
   - Cash: must be fully paid (paid + discount ≥ amount).
   - Credit-style categories must still have a pending amount or discount; otherwise reject with “use Cash Sale if fully paid.”
9. Discount cannot exceed amount (except Booking Delivery, which carries none). Split and store per-line discounts.
10. Validate manual bill uniqueness. Create an Invoice when requested (manual number or timestamp-based INV number), with status PAID/PARTIAL/OPEN and balance = amount − discount − paid.
11. Create the DirectSale header, SaleDeliveryPerson allocations (or a single driver allocation), DirectSaleItems with stored discounts and frozen cost rates, and Entry OUT rows tagged nimbus “Direct Sale”, per-line client category, transaction category Billed/Unbilled, driver, alternate mapping, and source stamps.
12. Apply BookingAllocations for booked slices and GRNAllocations for chargeable slices.
13. Finalize the transaction: rebuild stock totals from entries, synchronize pending bill, delivery rent, waive-off mirror, and account receipt; then commit. A concurrent idempotency-key collision redirects to the already-saved sale.
14. Delete the held draft if one was resumed. Redirect to the bill download.

**Rent reconciliation on the sale:** rent item revenue = sum of service/rent lines (qty × rate, falling back to the client’s latest booked rate); delivery rent cost = actual driver rent; variance loss = max(0, cost − revenue). These feed the P&L service margin and driver payable.

### 8.3 Alternate materials

- An alternate may be supplied only against a booked material with available booking balance.
- The physically delivered material is the alternate; the originally booked material is stored as booked_material for reference.
- Stock is validated and decremented for the alternate, never for the booked material.
- Excess beyond the booking balance also ships the alternate and is priced from it.
- Bills and ledgers display the delivered material as primary, with “ALT for booked <original>” as context.
- Services can never be alternates.

### 8.4 Discounts

- One header discount per document; optional per-item named amounts from the discount panel.
- Named amounts win; the remainder is allocated across goods lines by value using largest remainder.
- Services keep full value unless explicitly named.
- Unknown named items are rejected, not silently dropped.
- Shares are capped at each line’s gross and stored on the line with a kind (manual / allocated / none) and reason.
- Returns withdraw the stored discount belonging to the returned quantity (Section 8.7).

### 8.5 Held drafts

- Hold saves the full form payload as a DirectSaleDraft with a summary (client, category, driver, item count, total qty, total amount) and timestamps.
- Resume restores the payload into the Add Sale form; saving uses the final save time, not the hold time, and deletes the draft.
- Drafts can be deleted from the Hold Bills drawer.

### 8.6 Sale edit, delete, restore

**Edit:** `/edit_bill/DirectSale/<id>` reconstructs lines with the same booking-split and FIFO logic, excluding the sale’s own old entries from the booking-pool calculation so a no-op edit does not drop allocations.
- Registered-client categories require a real client.
- Credit/Mixed/Open Khata edits require a manual bill number.
- Alternate mappings are preserved when the edit form omits them.
- Old booking allocations are archived and deleted before lines are replaced; GRN allocations are deleted; sale items and driver allocations are rebuilt.
- Invoice totals/balance/status are updated in place.
- Category policy is re-enforced; fully-paid historical edits are allowed even on credit-style categories to avoid blocking corrections.
- Finalize rebuilds stock, pending bill, rent, waive-off, and account receipt.

**Delete / void:** the legacy `/void_transaction` and `/delete_transaction` URLs hard-delete via the shared service. `/delete_bill` is admin-only. Effects removed: entries, driver rents/allocations, booking/GRN allocations, pending bill, orphan invoice, account transactions, waive-off mirrors. Audit is retained.

**Restore:** `/unvoid_transaction` restores voided Entries, DirectSales (atomic restore with tracking), MaterialReturns, Bookings, and Payments, reapplying stock and ledger effects and rebuilding pending bills.

### 8.7 Material returns

**Screen:** `/material_returns` — filters (client, bill, material, date range), table, and Add/Edit Return modal.

**Fields:** client, date/time, return type (Normal Return / Booked Return), manual bill, next auto bill, repeatable rows (material/service, qty, unit price, rent price, line total), note, total return amount.

**Rules:**
1. Client must be active. Return type must be uniform across all rows.
2. Normal return: unit price required (fallback to material unit price); rent price optional. Total = qty × (unit + rent).
3. Booked return: rent price is the value; total = qty × rent. Booked returns restore the client’s booking pool.
4. Returnable quantity caps: normal returns are capped by the client’s delivered-not-yet-returned quantity; booked returns by the booked pool. Edits add back the return’s own previous quantity before re-validating.
5. Create the MaterialReturn, a Payment of type Material Return (method “Material Return”, source link, marker `[MATERIAL_RETURN:<id>]`), and Entry IN rows tagged nimbus “Material Return” with transaction category Return or Booked Return.
6. Store on each return item the discount reversed from the original sale/booking line, looked up oldest-first and skipping quantity already returned. This prevents phantom losses on discounted returns.
7. Increase material stock for real materials; synchronize the payment’s waive-off mirror; apply the return amount as a settlement against the client’s pending bills.
8. Edit (admin-only): return type cannot change; reverse old entries, replace items, rebuild entries and the linked payment, rebuild pending bills.
9. Delete/void reverses stock and removes the return’s payment effect; restore reapplies it.

### 8.8 Invoices and bill viewing

- Invoice numbers are manual or timestamp-based INV numbers; uniqueness is enforced.
- A sale may be unbilled (cash) and still have an auto bill reference.
- `/view_bill/<bill_no>` resolves across bookings, payments, invoices, sales, GRNs, and pending bills using source hints (type, id, client, entry). If multiple primary sources match and no hint is given, show a collision-resolution chooser.
- Bill view shows the document, client balance as of the bill cutoff, previous balance, recent deliveries, material ledger context, rent reconciliation, and download/print actions.
- `/download_invoice` renders a PDF (WeasyPrint, ReportLab fallback) with company settings, bill lines, totals, and client context.
- `/view_bill_detail` and the P&L transaction-detail page provide deeper drill-downs.

---

## 9. Payments, settlements, and pending bills

### 9.1 Client payments

**Screens:** `/payments` (Payments Hub, read-only list with filters), `/accounts/payments` (full CRUD), and the client ledger’s pay action. All mutate through one canonical service.

**Kinds:**
| Kind | Amount | Discount | Account effect |
|---|---|---|---|
| Receipt | positive | optional, reason required | Money IN to selected account |
| Refund | stored negative | forced 0 | Money OUT of selected account |
| Waive-Off | 0 | positive, reason required | No cash; non-cash Loss row |
| Material Return | positive | 0 | No cash; controlled by the return |

**Validation:**
- Idempotency key + payload hash; expected revision on edit.
- Material-Return-controlled payments cannot be edited/deleted/restored directly; the return must be edited.
- Client must resolve; suspended clients cannot be newly selected, but an unchanged historical client remains valid.
- Method normalizes to Cash, Bank, Check, Card, Online. Cash requires a cash account; all other methods require a bank account. Digital wallets behave as bank.
- Receipt requires an account when amount > 0. Refund requires an account. Waive-Off may be discount-only.
- Insufficient balance is rejected for refunds; receipts do not require balance.
- Future dates rejected. Closed reconciliation periods rejected on create and on financial edits (amount, discount, account, client, type, date).
- Manual bill uniqueness across all sources.

**Effects:**
- Create/update the Payment with minor-unit mirrors, revision, actor, auto bill CP.
- Synchronize a WaiveOff mirror for the discount (skipped in ledger totals when it is a direct-sale discount mirror).
- Synchronize AccountTransactions: Receipt IN, Refund OUT, Loss for waive-off, all keyed by `[SRC:Payment:<id>]`. Amount/account changes void-and-recreate so old and new effects never coexist.
- Rebuild pending bills for both the old and new client when attribution changes.
- Write a structured accounting audit record.

**Delete/restore:** soft-void via the canonical service, reversing account effects and rebuilding pending bills; restore reapplies. Hard delete (admin) additionally removes waive-offs, generated pending bills, source-marked account transactions, and unlinks returns.

### 9.2 Supplier payments

**Screens:** `/accounts/supplier_payments`, Pay Supplier page, supplier ledger pay action.

**Rules:**
- Amount must be > 0. Supplier must exist and be active for new relationships.
- Method/account category and balance checks mirror client payments (money OUT).
- GRN-controlled automatic payments cannot be edited directly; edit the GRN.
- Idempotency, revision, future-date, closed-period, and manual-bill guards apply.
- Effects: SupplierPayment + one AccountTransaction type Supplier Payment keyed by `[SRC:SupplierPayment:<id>]`.
- Delete/restore soft-void and reverse/reapply; hard delete removes the row and its accounting.
- Supplier ledger credits GRN totals and debits payments; legacy GRN.paid_amount is debited only when no automatic SupplierPayment exists, to avoid double counting.

### 9.3 Driver / delivery-person settlements

**Screens:** Delivery Rent page (`/delivery_rents`), driver ledger pay action, Accounts “Driver Service Payment”.

**Delivery Rent page:** paginated SaleDeliveryPerson allocations with date/driver filters; totals for rent, paid, waived, due; per-driver totals; pay modal per allocation; void deletes the allocation and its payments.

**Canonical settlement service:**
- Inputs: driver, optional allocation id, amount paid, waive-off amount, method, account, reference, date, note, idempotency key.
- Outstanding = opening balance + active rent allocations + legacy DeliveryRent (only when no allocation exists for that sale) − paid − waived. Never a stored field.
- FIFO settlement: oldest open rent first when no allocation is specified; a specific allocation pays that row.
- Overpayment is rejected. Paid amount requires a matching cash/bank account with sufficient balance; waive-off needs no account.
- Effects: one DeliveryPersonPayment per allocated slice + one AccountTransaction type Driver Payment (money OUT) + one Loss row for the waive-off, all source-keyed.
- Edit/void/restore follow the same revision, closed-period, and audit rules as other payments.
- The driver ledger and account ledger are projections of this pair; neither keeps an independent balance.

### 9.4 Pending bills

**Screen:** `/pending_bills` — filters (client code, bill number with variant matching, bill kind SB/MB, cash flag, manual flag, category including Unbilled Cash / Cash Paid / Open Khata / sale categories / client category), 15 per page.

**Nature:** PendingBill is a **derived tracker**, not an independent receivable. Amounts are rebuilt from the client ledger obligation whenever the source changes. Standalone manual pending bills exist only when untraceable to a booking/sale.

**Rules:**
- Add manual: client must exist; bill number required and unique; kind MB; amount, reason, nimbus, photo/URL, note.
- Edit: re-normalize bill, re-check conflicts, update linked Entry bill references.
- Delete: hard-delete with reminders/contacts; no void flag left behind.
- Toggle paid: marks the tracker paid/unpaid; the notification “mark paid” action closes reminders but does **not** create a receipt.
- Rebuild: after any booking/sale/payment/return/cancellation change, recompute the client’s obligations and synchronize pending rows; never increment a stale snapshot.
- Export/import are admin-only and route through the Import/Export Center.

---

## 10. Accounts and cash

### 10.1 Accounts dashboard

**Screen:** `/accounts/` — KPI cards (client payments today, supplier payments today, expenditures today, receipts today, company money, cash money, cash accounts, bank accounts), account cards with today-opening balance, due-client and supplier-payable summaries, and the Receive / Pay / Transfer action bar.

**KPI rules (must match the source, including its legacy quirks):**
- Client payments today = sum of active Payment rows with an account, amount > 0, posted today.
- Supplier payments today = active SupplierPayment rows + legacy GRN.paid_amount only when no automatic SupplierPayment represents that GRN.
- Expenditures today = active FbmCashDrawerEntry “out” rows (legacy cash drawer).
- Receipts today = booking paid-now + direct-sale paid + client payments + standalone Receipt AccountTransactions that are **not** source-marked `[SRC:…]`. Source-linked receipts are already counted by their document and must not be added again.
- Company money = sum of balances of company accounts; cash money = the cash-category subset.
- Each KPI has a detail page and a JSON API under `/accounts/kpi/…` and `/accounts/api/kpi/…`.

### 10.2 Receive / Pay / Transfer

All three share one POST endpoint (`/accounts/transactions/new`) with a mode field.

**Receive modes:**
| Receive from | Behaviour |
|---|---|
| Client ledger | Delegates to the canonical client-payment service (Receipt, or Waive-Off when amount = 0 and discount > 0). |
| Other source | Requires a free-text source label; adds a standalone Receipt AccountTransaction into the destination account. |
| A classified source account (Loan, Partner, Worker, etc.) | Validates the source account belongs to the chosen category, differs from destination, and has sufficient balance (Loan accounts may go negative). Writes a Transfer. |

**Pay targets:**
| Target | Behaviour |
|---|---|
| Company transfer | Transfer between two active accounts; source balance must cover. |
| Supplier | Canonical supplier-payment service. |
| Driver | Canonical driver FIFO settlement service. |
| Client refund | Canonical client-payment service with kind Refund. |
| Loan | Repayment into a Loan-classified account (moves its negative balance toward zero). |
| Personal / other expense | Expense or Payment AccountTransaction out of the source account. |

**Common validation:** amount > 0, active accounts, method/category match, sufficient source balance (except loans as source), distinct source/destination for transfers, atomic commit, structured audit for every new AccountTransaction.

**Standalone transfers:** `/accounts/transfers` lists transfers with date/search filters and totals; `/accounts/transfers/add` is a dedicated form using minor-unit math and the shared apply-effect helper.

**Account transaction edit/delete:**
- Reconciliation entries are immutable.
- Entries linked to Payment / SupplierPayment are edited/deleted **only** through their source document.
- Unlinked entries may be edited (reverse old effect, apply new) or soft-voided, with closed-period checks on every affected account and audit records.

### 10.3 Account ledger

`/accounts/ledger/<account_id>` — date range (default 90 days), search, type filter, show-voided toggle, 50 rows/page.
- Opening balance is reproducible: explicit account opening baseline + all ledger movements up to the day before the range. Never derived by subtracting the current balance.
- Running balance is computed over **all** active movements in the range even when search/type filters narrow the displayed rows.
- Columns: date/time, type, description/note, from/to account, amount, delta, running balance, source link, void state.
- Period IN/OUT totals exclude voided rows.

### 10.4 Per-account reconciliation (immutable)

`/accounts/<id>/reconcile` compares the ledger balance (expected) with a user-entered actual balance on a chosen date.
- Difference type: Matched / Short (actual < expected) / Excess (actual > expected).
- Writes an AccountReconciliation plus Adjustment AccountTransaction(s) (Reconciliation Loss / Reconciliation Excess) so the ledger reflects the physical count.
- Reconciliation records and their adjustments cannot be edited or deleted; `/accounts/reconciliations` is a read-only history with account/status/date filters.
- A reconciliation closes the period up to its timestamp for later financial edits (the closed-period guard).

### 10.5 Daily Cash & Bank Reconciliation board

**Screen:** `/daily_reconciliation` (reference image #1). One row per active company cash/bank account for the selected financial day.

**Columns:** Account (with CASH/BANK badge), Opening, In, Out, Transfer In, Transfer Out, Expected Closing, Actual Counted (input), Difference, Lock state. Footer totals; day picker; Verify & Lock; Unlock; per-row Save/Clear; optional lock note.

**Rules:**
- Opening = latest locked counted closing before the day + all movements from the day after that lock through yesterday; if never locked, the account’s explicit opening baseline + all prior movements.
- Expected closing = opening + in − out + transfer in − transfer out.
- Save count stores the physical figure per account/day. Difference = counted − expected.
- Verify & Lock freezes every account’s counted figure (defaulting to expected when uncounted), writes a day-level lock with totals and difference, and makes the locked counted totals the next day’s opening.
- Unlock removes the day lock and unlocks the positions. Clear count removes one account’s counted figure.
- JSON responses mirror the HTML board for live updates.

### 10.6 Cash Flow page

**Screen:** `/cash_flow` — opening balance, running-balance transaction table, summary cards, filters, manual movement form, category/subcategory/party management, physical-cash reconciliation, exports, and an embedded Financial Tracking tab.

**Row sources (one collector):**
1. Manual CashFlowEntry rows (recorded on this page).
2. Derived rows from client Payments (cash method), cash DirectSales, booking paid-now, supplier payments, driver payments, GRN paid amounts, transfers, expenses, and other AccountTransactions. Derived rows are labelled with their origin (e.g., “From Accounts · Client Payments”) and are not editable here.

**Manual movement fields:** direction (In / Out / Transfer), amount, source account, destination account (transfer only), category, subcategory, party (type + name), description, note, reference, date/time. Categories/subcategories/parties can be created inline, renamed, enabled/disabled, or deleted when unused. Idempotency key prevents double-posting. Each manual entry creates exactly one AccountTransaction and applies its balance effect; edit reverses and reapplies; void/restore keep history and audit.

**Filters:** date range, type (all/in/out/transfer), origin (all/recorded/derived), category, subcategory, party type, party, account, search, notes, reference, description, created-by, status (active/voided/all), amount min/max.

**Opening balance:** explicit input wins; otherwise today’s session override; otherwise fresh-start zero when the user explicitly reset today; otherwise automatic carry-forward from prior movements. “Fresh start” is opt-in per session and hides earlier entries for today without changing account balances.

**Physical-cash reconciliation:** enter physical cash available + reason for a date. Difference is computed against the **full unfiltered company-wide closing**, never the filtered view. Saving stores a CashFlowDifferenceAdjustment with an audit trail (create/edit/delete history). It does not change account balances; it informs the next-day opening in this report. `/cash_flow_differences` lists history; a detail page shows the audit trail.

**Exports:** CSV and PDF, scope = filtered or all (respecting the status filter).

### 10.7 Financial Tracking filter matrix

**Screen:** `/financial_tracker` (reference image #2) and the embedded “Transactions” tab on Cash Flow.

**Filter matrix:** preset (this month default, today, yesterday, this week, last 30, all), direction, category, account, client, supplier, partner, worker, vehicle, method, free-text search. Dropdown options are derived from the data in the selected window.

**KPI strip:** total in (cash in + transfer in), total out (cash out + transfer out), net, record count. **Breakdowns:** by category and by account. **Export bar:** CSV of the filtered rows.

**Rules:** only active rows; role filters match party type + name; vehicle searches description/note/reference; method matches method or origin label; search spans reference, description, note, party, category, account. Totals always cover all matching rows, not just a page.

### 10.8 Audit trail, expenditures, receipts

- `/accounts/audit` — AccountTransaction list (date/search/type/account/show-voided, 60/page) with IN/OUT totals, plus a parallel AccountingAuditLog stream (40/page) showing actor, action, entity, before/after JSON, reason.
- `/accounts/expenditures` — merges legacy FBM cash-drawer outflows with AccountTransaction Expense/Payment/Driver Payment rows; filters by search/category/method; 50/page.
- `/accounts/receipts` — lists DirectSales and paid GRNs for the day with authoritative GRN totals (items + landed costs), not just expense columns.

---

## 11. Ledgers

### 11.1 Shared ledger engine

All party ledgers are **projections**, not stored balances. One engine builds typed rows (date, type, reference, description, debit, credit, source type/id, account, note, running balance), sorts them, applies a running balance with the correct convention, filters rows, and computes closing balances.

**Filter set (shared):** start/end date, transaction type, free-text query, amount min/max, account, status. Pagination never changes the closing balance: the page shows the whole-ledger closing and, when filters are active, a separate filtered closing.

**Conventions:**
- Client / driver: debit increases what is owed to the business (or by the business for drivers), credit reduces it. Positive closing = outstanding/payable; negative = credit/advance.
- Supplier: credit increases what the business owes; debit reduces it. Positive closing = payable.

### 11.2 Client financial ledger

`/ledger/<client_id>` (and `/financial_ledger/<client_id>` details).

**Rows:** opening balance; each obligation (Booking, DirectSale, standalone PendingBill, Invoice where relevant) as a debit with its embedded paid amount as credit; embedded discounts as Waive-Off credits; client Payments (Receipt credit, Refund debit, Material Return credit); standalone WaiveOffs; booking CANCEL entries as credits at the stored rate.

**Obligations:** built from the client snapshot with FIFO bill allocation of later receipts/returns/discounts, so a payment reduces the oldest open bills first. Duplicate client names are treated as one financial identity; the first stable master row carries the ledger. Unmatched historical rows are reported in an audit bucket, never invented into a balance.

**Outputs:** total debit/credit, closing balance, status (Outstanding / Credit / Settled), last transaction date, last payment date, obligation list with per-bill paid/remaining, and downloadable ledger/clearance/full-history PDFs.

### 11.3 Client material ledger

`/client_ledger/<id>` — per material: booked qty, dispatched qty, returned booked qty, remaining qty, latest booked rate, booked/dispatched/remaining value. Shows recent bookings, deliveries, returns, cancellations, and the booking-cancel panel. Remaining = booked − delivered + booked returns; deliveries consume oldest bookings first. This is the screen from which booking cancellation and revert are launched.

### 11.4 Supplier ledger

`/suppliers` (list) and `/supplier_ledger/<id>` (detail) — per supplier: opening balance, GRN credits at the authoritative GRN total, legacy GRN paid debits only when no automatic SupplierPayment exists, SupplierPayment debits (refunds/credits become credits), running payable, status (Payable / Credit / Settled), last transaction/payment dates. Pay Supplier action uses the canonical service. Supplier payable overview aggregates all suppliers.

### 11.5 Delivery-person ledger

`/delivery_person_ledger/<id>` (alias `/delivery_ledger/<id>`) — opening balance, active sale rent allocations (debit), legacy DeliveryRent only when no allocation represents the same sale/person, driver payments (credit) and waive-offs (credit). Payments linked to voided allocations are excluded as integrity exceptions. Pay / edit / void / restore actions delegate to the canonical driver service. Opening balance is editable by users with delivery-person management permission. Downloadable PDF/CSV.

### 11.6 Decision ledger

`/decision_ledger` — management overview combining:
- Per-client financial summary (bookings + sales debit; payments, paid-now, discounts credit; opening balance).
- Per-client material summary (booked, dispatched, remaining, latest price, values).
- Supplier payable and driver payable summaries.
- Material-level movement and stock context.
This is a read-only decision screen; it does not create transactions.

### 11.7 Current payables

`/current_payables` (alias `/unpaid_transactions`) — grouped by client **after** ledger consolidation, so partial payments cannot be hidden by row-level filters. Filters: client, amount operator/min/max/exact, date range (semantics: last contributing transaction date; the amount remains the complete current balance), status (outstanding/all/settled/credit). Totals, pagination (10–100/page), JSON APIs, per-client drill-down, and CSV export. Orphan historical rows are not manufactured into payables.

### 11.8 Ledger downloads

- Client ledger PDF, client clearance certificate, full client history.
- Delivery-person ledger PDF/CSV.
- Current payables CSV.
All use the same projections as the screens; exports must not recompute balances differently.

---

## 12. Reports

### 12.1 Profit & Loss Control Center

**Screen:** `/profit_reports` — one authoritative projection of all business events into P&L rows. Routes only validate input, enforce permission, and serialize the same view model to HTML, PDF, or Excel.

**Core formula (plain-language breakdown):**
- Delivered goods sales − FIFO cost of goods sold = Gross profit.
- Earned service revenue − direct service costs = Service margin.
- Gross profit + service margin + other income − operating expenses − other losses + other gains = Net profit / loss.
- Gross revenue − discounts = net revenue; the two are shown separately so they cannot be confused.

**Recognition rules:**
- Revenue is recognized on delivery/sale, not on receipt. Credit is a receivable; a booking advance is not income until supplied.
- Returns reverse revenue and restore cost using the original cost layers and the stored per-line discount reversal.
- Purchases, customer receipts, supplier payments, and driver payments are position/settlement information, not new P&L events.
- Rows with unknown cost are excluded from net and reported explicitly (unknown-cost revenue, row count). Estimated costs are labelled and separated from actual costs.
- Delivery rent cost is the actual driver rent; service revenue is the rent charged on the sale; the variance is a loss when cost exceeds revenue.

**Summary metrics:** gross/net material revenue, delivery revenue, other income, material cost (actual vs estimated), delivery cost, COGS, expenses, other losses/gains, discounts, discount loss, returns revenue/cost, total profit, recorded-cost profit, profit/loss entry counts, unknown-cost rows/revenue, estimated-cost rows, recognized revenue, margin %, markup %, row counts, and a formula check (revenue + gains − COGS − expenses − losses − unresolved revenue) with its difference from total profit.

### 12.2 Periods and comparison

Presets: today, yesterday, this week, this month (default), last month, this year, last year, last 7 days, last 30 days, all dates, custom (inclusive; reversed dates are swapped). The previous equivalent period is computed automatically for comparison cards (amount and percent change), unless disabled.

### 12.3 Filters and transaction explorer

Filters: client (code resolves to name), material, supplier, category, GRN id, payment status (paid / partially paid / unpaid / credit outstanding), booking status (open / partially delivered / completed / cancelled), transaction group (goods / services / returns / expenses / other), entity type, client category, metric (profit/loss/unknown cost), cost status, entry type, free-text search, sort/order. **Filters only select rows; they never change a calculation.** Totals cover every matching row; pagination (10–100/page) affects only the table. Automatic filter changes fetch a partial results region without a page reload.

Payment status comes from the client ledger’s FIFO bill allocation; booking status comes from supplied-vs-booked quantity. Neither changes revenue.

### 12.4 Breakdowns and insights

- Breakdown tables by transaction group, payment status, booking status, client category, and GRN/stock consumption (reusing the same FIFO projection as the GRN page).
- Insights: top clients, top items, daily revenue/COGS/expenses/profit, transaction-type totals.
- Warnings: short-stock cost, missing cost, unmatched/orphan dispatches, unlinked zero-value supplies — surfaced, never silently converted into revenue or cost.

### 12.5 Financial position panel

When requested (summary view, not entries view): payments received in the period (deduplicated against source-linked receipts), customer receivables and credits, supplier payables and advances, purchases in the period, and current stock context. Balances are current positions, not as-of-date profit. Customer advances use identifiable booking paid amounts; later unallocated receipts are not assigned to a booking.

### 12.6 Disclosed limitations (must be shown in the rebuilt report)

1. Booking/source lines have no general rate-history snapshot; earlier activity uses current stored rates where no immutable snapshot exists, with audit detail instead of invented history.
2. Hard-deleted documents cannot be reconstructed from ordinary report rows; only explicit repair archives and audit records are evidence.
3. Standalone imported dispatches without a reliable match are warnings only.
4. Unlinked legacy AccountTransaction payments (except identifiable loans/transfers/settlements) are treated as explicit other expenses for review.
5. GRN loading/freight/tax/other landed costs are not allocated into FIFO unit costs and are not silently expensed or added to COGS.
6. Receipts and payments are settlements, not P&L events.
7. Current stock has no authoritative moving-average valuation; stock value is not presented as profit.

### 12.7 Exports and drill-down

- `/profit_reports/export/pdf` — print-ready P&L (WeasyPrint; failure is an explicit error).
- `/profit_reports/export/excel` — workbook with summary, comparison, transactions, daily insights, top items, and financial position.
- `/profit_reports/transaction/<type>/<id>` — document detail with its P&L rows, cost layers, settlement status, and audit evidence.
- Summary cards link into the entries view with the matching filter pre-applied.

### 12.8 Financial details (cash / credit)

`/financial_details` — type = cash or credit; date range (default today); client search (code resolves to name); min/max amount.
- Cash: client Payments + booking advances + direct-sale paid amounts.
- Credit: booking credit (amount − paid) + direct-sale credit (amount − paid), with amount filters.
Rows show date, client, amount, type, reference, note. This is a lightweight legacy detail view distinct from the P&L.

### 12.9 Report redirects

- `/daily_transactions` → inventory daily breakdown.
- `/stock_summary` → inventory stock summary.
- `/mixed_transactions` → tracking filtered by Mixed Transaction.

---

## 13. Notifications and credit follow-up

### 13.1 Queue and risk

**Screen:** `/notifications` — filters (category, status, risk, search), count badges (total, very high, high, medium, low, pending), bill rows, active reminders, completed reminders, staff-email panel.

**Queue source:** non-void PendingBill rows that are unpaid, amount > 0, and not cash-tagged. Each row carries category (Billed / Unbilled / Open Khata / Unbilled Cash / Cash Paid), age in days, amount, client text, contact count, risk score and level.

**Risk scoring (must match):**
- A manual severity override (Low / Medium / High / Very High) wins; “Auto” clears it.
- Otherwise: paid → Low; amount > 10,000 → Very High; > 5,000 → High; > 0 → Medium; else Low.
- An unpaid bill with 2 or more logged contacts is escalated to Very High.
- Sort score = severity rank (Low 1 … Very High 4) × 1,000,000 + amount + age days. Rows sort descending by score, then age, then amount.

### 13.2 Reminders and contacts

- **Set reminder:** one active reminder per bill; saving overwrites the existing active one (date/time + note) and clears its alert/ack stamps.
- **Upcoming:** `/notifications/upcoming` lists active reminders for unpaid, non-void bills ordered by remind time.
- **Log contact:** channel (Call / WhatsApp / SMS / Email / Visit / Other), required customer response (≤200 chars), optional note (≤500), timestamp (defaults now), actor.
- **Close / acknowledge reminder:** requires a response; writes a contact linked to the reminder and marks it done with an acknowledged timestamp.
- **Resolve due (dialog):** outcome Paid or Call back later. Paid marks the bill paid and closes the reminder. Callback requires a future date/time and no other active reminder on the bill; it closes the current reminder and creates the callback. Both write a contact record.
- **Mark paid:** one-click action that sets the bill paid, flags Open Khata bills as cash, closes every active reminder with an automatic “payment received before follow-up” contact, and **does not create an accounting receipt**.
- **Bill detail:** `/notifications/bill/<id>` shows the bill, all open credit bills for the same client, total client due, active reminder, reminder history, contact history (with legacy backfill matching by timestamp), risk score, and actions (set reminder, log contact, set severity, mark paid, resolve due).

### 13.3 Staff emails

Add (validated, unique, lowercased), toggle active, delete. The list is the recipient registry for follow-up notifications. Email delivery itself is removed from this build; the import/export “email file” action explicitly tells the user to download instead.

---

## 14. Import / Export Center and data transport

### 14.1 Access and safety

- The whole `/import_export` area is admin-only in single-store mode. App-upgrade endpoints are disabled and redirect with a warning.
- pandas/openpyxl are required; the page and every import/export action fail closed with a clear message when missing.
- Every import takes a safety backup first; a failed backup blocks the import.
- Uploads and generated exports are archived as artifacts; reports are downloadable.
- Single-store scope: no tenant selection; “tenant” export/restore endpoints return 404.

### 14.2 Page sections

1. **Templates** — empty Excel/CSV templates for clients, dispatch, pending bills, and a combined “Client Full” workbook (Clients, Bookings, BookingItems, Dispatch, Payments, Sales, SaleItems, PendingBills). CSV variant of Client Full is a ZIP of per-sheet CSVs.
2. **Preview** — upload a file; normalize column names; auto-detect dataset (dispatch / pending bills / clients); return row count, columns, and a 10-row preview.
3. **Execute import** — dataset type, conflict strategy (skip/update), missing-client strategy (create/skip/stop). Supports clients, dispatch, pending bills, and client_full. Client Full reads each known sheet, builds name/code triage maps, and processes in dependency order (clients → bookings → booking items → dispatch → payments → sales → sale items → pending bills). Returns a report: imported, updated, skipped, errors, error details, discrepancies.
4. **Master export / import** — one Excel workbook with the business sheets (Clients; MaterialCategories, Materials; Dispatch; Bookings, BookingItems; Payments, FBMCashDrawer, FBMCashDrawerCategories; Sales, SaleItems; GRN, GRNItems; DeliveryPersons, DeliveryRents; PendingBills). Import runs as a background job with progress polling (`/master/import/start`, `/master/import/status/<job_id>`); a synchronous compatibility endpoint also exists. User rows are imported with typed boolean conversion and redacted diagnostics.
5. **Transfer export / import** — choose sections (all_business, literal_all, or individual master sections) and/or export modules. Export builds the matching workbook; import reads the same formats and reports inserted/updated/skipped/failed/warnings with capped row-level issue details and a downloadable issue report.
6. **Full raw export / import** — literal table-by-table Excel export/import of all business tables (excluding the wipe-history and live-session tables). Import is behind a safety toggle (default off), supports append or replace modes, validates row by row, saves valid rows, and lists rejected rows in a report. Replace is blocked for all-tenant scope.
7. **Full database snapshot** — `.amsdb` export/import (Section 14.4).
8. **Module export list** — the selectable modules and their tables:

| Module | Tables |
|---|---|
| Clients | client, recon_basket |
| Suppliers & Supplier Payments | supplier, supplier_payment |
| Pending Bills & Follow-ups | pending_bill, follow_up_contact, follow_up_reminder |
| Notification Data | staff_email, follow_up_contact, follow_up_reminder |
| Stock Dispatch & Receiving | entry, delivery, delivery_item |
| GRN | grn, grn_item, grn_allocation |
| Materials & Categories | material_category, material |
| Direct Sales, Invoices & Driver Payments | direct_sale, direct_sale_item, direct_sale_draft, invoice, delivery_rent, sale_delivery_persons, delivery_person_payment |
| Material Returns | material_return, material_return_item |
| Bookings | booking, booking_item, booking_allocation, booking_allocation_repair_archive |
| Payments | payment, waive_off |
| Delivery Persons & Driver Payments | delivery_person, delivery_person_payment, sale_delivery_persons, delivery_rent |
| Financial Accounts | account, account_category, account_transaction |
| Cash Drawer | fbm_cash_drawer_entry, fbm_cash_drawer_category |
| Cash Flow & Reconciliations | cash_flow_entry, cash_flow_entry_audit, cash_flow_category, cash_flow_subcategory, cash_flow_party, account_reconciliation, cash_flow_difference_adjustment, cash_flow_reconciliation_audit |
| Rental Management (FBM) | fbm_rental, fbm_rental_item, fbm_client |

### 14.3 Template column contracts

- **Clients:** code, name, phone, address, category, financial_book_no, financial_page, cement_book_no, cement_page, steel_book_no, steel_page, book_no, location_url, page_notes, status.
- **Dispatch:** CLIENT_CODE, CLIENT_NAME, CLIENT_CATEGORY, TRANSACTION_CATEGORY, BILL_NO, BILL_DATE, CEMENT_BRAND, QTY, NIMBUS, NOTES, SOURCE, MATCH_STATUS. Legacy aliases accepted: cement_brand→item, client_name→customer, bill_date→date, nimbus→nimbus_no.
- **Pending bills:** client_code, bill_no, name, amount, reason, nimbus.
- **Bookings:** client_name, manual_bill_no, amount, paid_amount, date_posted, note.
- **Booking items:** booking_bill_no, booking_client_name, material_name, qty, price_at_time.
- **Payments:** client_name, manual_bill_no, amount, method, date_posted, note.
- **Sales:** client_name, manual_bill_no, auto_bill_no, category, amount, paid_amount, rent_item_revenue, delivery_rent_cost, rent_variance_loss, date_posted, note.
- **Sale items:** sale_bill_no, sale_client_name, product_name, qty, price_at_time.

### 14.4 Full database snapshot (.amsdb)

- A snapshot is a consistent SQLite copy (online-backup API, WAL-safe) plus a small metadata table. Pure stdlib engine; no pandas.
- **Export:** writes `AMS_FULL_<stamp>.amsdb`, verifies integrity, records row counts, and downloads it.
- **Import:** clean-and-copy by default — delete every target data row (schema/indexes/seeds stay), then insert source rows with original primary keys in foreign-key dependency order. Users are included (full replace). Append mode also exists. The target is backed up automatically first. Integrity, foreign-key, and row-count parity are verified; mismatches and relaxed unique indexes are reported. A JSON import report is stored and linked from the page.
- **CLI:** `python -m full_db_sync export|verify|clean|import` with `--confirm` required for writes; optional JSON report and verbose per-table counts.
- Safety toggle `FULL_DB_SYNC_ENABLED` (default on) gates the UI routes.

### 14.5 Convenience exports

- `/export_clients` — client list with book/page references and pending amount (admin).
- `/export_pending_bills`, `/export_unpaid_transactions` — redirect to the generic export with the dataset preset.
- `/export_grn` — GRN export.
- Generic `/import_export/export` supports clients, dispatch, pending bills, unpaid transactions with date/client/bill/category filters and Excel/CSV/PDF formats.

---

## 15. Settings, users, admin, and maintenance

### 15.1 Settings screen

`/settings` — admin or `can_access_settings`. Sections:
- **Company profile:** company name, address, phone, currency; global negative-stock allowance toggle. Saved via `/update_settings` (admin only).
- **Users & permissions:** user table with role, access mode (read-only / read-write), status, the 23 permission checkboxes grouped into the five feature groups, and the backdate-restriction flag. Add user, edit permissions/password, toggle status, deactivate. The access matrix shows every user’s current values and the defaults.
- **Material categories:** add/rename/toggle/delete.
- **Maintenance:** reconcile data (dry-run or apply fixes), rebuild ERP consistency, system report, void audit, activity log, login sessions, data wipe, accounts-domain wipe.
- **Recent audit:** last 20 audit events.

### 15.2 User rules

- Built-in `admin` cannot be recreated, deleted, deactivated, or edited through the user forms; `root` is protected similarly.
- New user: unique username, hashed password (default from environment when blank), role, access mode, permission flags. `can_manage_directory` is derived from the four master-management flags.
- Edit: role, optional password reset, permission flags, access mode, backdate restriction.
- Delete = deactivate (never hard-delete); cannot deactivate yourself. Toggle status likewise protects self and built-ins.
- Change password: any logged-in user may change their own password.
- Login sessions: live sessions (45-minute window) and recent session history.

### 15.3 Void audit and restore

`/void_audit` — sections (all / voided / suspended / transactions / directory), search. Lists voided Entries, Bookings, Payments, DirectSales, MaterialReturns, PendingBills, DeliveryRents, SupplierPayments and suspended Clients, Materials, DeliveryPersons. Detects “Deleted by Edit” when a newer active row replaced a voided direct-sale entry/pending bill. Restore (`/void_audit/restore/<entity>/<id>`) reactivates the row through the same void-state helpers used elsewhere, reapplying stock/ledger effects where applicable.

### 15.4 Consistency and reconciliation tools

- **Reconcile data** (`/reconcile_data`): dry-run or apply. Checks broken references, direct-sale/booking/payment mismatches, waive-off mismatches, and bill-number normalization; writes a normalization audit report when fixes are applied; stores the report in the session for the settings page.
- **Rebuild ERP consistency** (`/admin/rebuild_erp_consistency`): rebuilds derived state (stock totals, pending bills, rents, waives, account links) for one client or all; audited.
- **System report** (`/system_report`): dispatch-vs-pending-bill sync issues, zero-amount bills, unpaid count, and material stock drift (cached total vs Entry IN−OUT). “Fix system issues” rebuilds direct-sale pending bills and material totals.
- **Activity log** (`/settings/activity`): searchable AuditLog, 50/page.
- **Debug DB** (`/debug/db`): admin-only JSON with path, size, mtime, and table counts.

### 15.5 Data wipe

- **Preview** (`/data_wipe_preview`): JSON counts for selected targets.
- **Selected wipe** (`/delete_selected_data`): confirmation text “DELETE SELECTED” (or “DELETE ALL DATA” for the hard-delete override). Targets expand through a dependency registry so dependent accounting/ledger tables go together. Core transactional/master targets are blocked unless the hard override is used. A pre-wipe safety backup is mandatory; failure blocks the wipe. Users, roles, sessions, settings, and audit are never deleted. Full wipe follows foreign-key-safe child-before-parent order.
- **Accounts-domain wipe** (`/admin/accounts_domain_wipe`): confirmation “WIPE_ACCOUNTS”, system-wide lock, pre-wipe snapshot + history row, atomic wipe + post-reset, post-commit integrity verification, automatic restore from the snapshot on failure, and health-snapshot rebuild marked as an intentional reset.

### 15.6 Admin module and Data Lab

- `/admin/` — admin-only module dashboard: loaded module/blueprint counts, system time, module list; `/admin/modules`, `/admin/api/modules`, `/admin/api/health`.
- `/data_lab/` — upload index/finance/dispatch spreadsheets; fuzzy-match bills and clients; produce a reconciliation basket grouped GREEN/YELLOW/RED/BLUE. `/data_lab/basket` lists it; `/data_lab/correct_bill` reassigns a bill’s client across PendingBill and Entry and clears its basket rows; `/data_lab/legacy_import` trusts the finance record and overwrites dispatch client typos. These are direct data-correction tools, distinct from the guarded Import/Export Center.

### 15.7 Backups and background work

- Hourly backup worker (daemon thread) stages, validates (SHA-256 manifest), and atomically publishes backups of the SQLite database and permanent uploads; cross-process lock with stale-owner recovery; retention applied after publish. Disabled unless enabled by configuration.
- Auto-reconcile worker is similarly configuration-gated and off by default in the shipped factory.
- Startup warms templates, ensures schema v44, applies pending migrations, enables foreign keys, and seeds the default admin only when the database is empty and allowed.

---

## 16. Validation and acceptance matrix

The rebuilt app must satisfy these observable behaviours. They are written as user-visible outcomes, not implementation steps.

### 16.1 Bootstrap, auth, permissions

| Scenario | Expected outcome |
|---|---|
| First run on an empty allowed database | Schema v44 is created, foreign keys are on, default admin exists, login works, dashboard renders. |
| Login with wrong password | Rejected; no session; flash explains failure. |
| Standard user opens a denied module | Redirect/flash “Permission denied”; no data mutation. |
| Read-only user submits a form | Mutation blocked; read pages still work. |
| Mutation without CSRF token | Rejected by the session CSRF guard. |
| Admin edits another user’s permissions | Matrix updates immediately; derived directory flag follows the four master flags. |
| Attempt to delete/deactivate built-in admin or self | Blocked with a clear message. |

### 16.2 Masters

| Scenario | Expected outcome |
|---|---|
| Add client with duplicate code/name | Rejected; existing client reused only through explicit lookup. |
| Suspend a client with history | History preserved; new transactions cannot select it; reactivation restores selection. |
| Transfer/reclaim a client | Financial and material history move with the code; ledgers recompute. |
| Add material/service with duplicate name | Rejected; merge/rename tools are the supported path. |
| Rename a material/service used in history | Historical rows keep the old name unless an explicit cascade tool is used; new documents use the new name. |
| Delete a referenced master | Blocked or archived; never orphaned. |
| Supplier ledger shift | Preview shows affected GRNs/payments; apply moves them atomically; reclaim reverses. |

### 16.3 Purchasing and stock

| Scenario | Expected outcome |
|---|---|
| GRN with paid amount and no account | Rejected. |
| GRN with bank method and cash account | Rejected (category mismatch). |
| GRN with insufficient account balance | Rejected. |
| GRN with a service line | Rejected; nothing saved. |
| GRN with zero-quantity line | Line skipped with warning; other lines saved. |
| Duplicate manual GRN bill | Rejected; no second GRN. |
| Edit GRN whose lots are consumed | Stock-affecting changes blocked; header-only edits allowed only when lines are unchanged. |
| Delete GRN with locked lots | Blocked. |
| GRN consumption indicator | Matches the P&L FIFO replay for the same lots. |
| Dispatch OUT beyond booking balance | Rejected. |
| Dispatch OUT to unknown client | Redirect to Direct Sale. |
| Non-admin backdated dispatch | Rejected. |

### 16.4 Sales, bookings, returns

| Scenario | Expected outcome |
|---|---|
| Booking with paid now | One receipt AccountTransaction; no duplicate Payment row; pending bill reflects the remainder. |
| Edit booking line below delivered qty | Rejected. |
| Rename/remove a delivered booking line | Rejected until the sale is voided/corrected. |
| Cash sale not fully paid | Rejected. |
| Credit sale with zero pending and zero discount | Rejected (“use Cash Sale”). |
| Booking Delivery sale with a chargeable line | Rejected. |
| Mixed sale without both slices | Rejected. |
| Alternate material on a booked slice | Ships the alternate, validates alternate stock, keeps booked-material reference, consumes the booking pool once. |
| Alternate on a chargeable slice or service | Rejected. |
| Duplicate sale submission (same key) | One sale; replay returns the existing document. |
| Same key, different payload | Rejected. |
| Keyless double-post within replay window | Suppressed. |
| Sale edit that changes nothing | Booking allocations preserved; no stock drift. |
| Normal return beyond delivered qty | Rejected. |
| Booked return | Restores booking pool; value = qty × rent. |
| Return of discounted sale | Reverses only the stored discount belonging to the returned quantity. |
| Return edit changes type | Rejected. |
| Void/delete sale | Stock, pending bill, rent, waive, and account effects removed; audit retained. |
| Restore sale | Effects reapplied atomically. |

### 16.5 Payments and accounts

| Scenario | Expected outcome |
|---|---|
| Receipt with cash method and bank account | Rejected. |
| Refund with insufficient balance | Rejected. |
| Waive-off with zero discount | Rejected. |
| Discount without reason | Rejected. |
| Edit a Material-Return-controlled payment | Rejected; edit the return instead. |
| Edit a GRN-controlled supplier payment | Rejected; edit the GRN instead. |
| Payment into a closed reconciliation period | Rejected. |
| Payment edit with stale revision | Rejected (“changed in another session”). |
| Account transfer to same account | Rejected. |
| Loan source with insufficient balance | Allowed to go negative. |
| Account reconciliation | Immutable record + adjustment; later edits in the closed period rejected. |
| Daily lock | Counted closings become next-day openings; unlock restores editability. |
| Cash Flow physical count | Compared against full unfiltered closing; does not change account balances. |

### 16.6 Ledgers and reports

| Scenario | Expected outcome |
|---|---|
| Client ledger after partial payment | Oldest bills reduced first; closing matches current payables. |
| Duplicate client names | One financial identity; no double counting. |
| Supplier ledger with automatic GRN payment | GRN paid amount not counted twice. |
| Driver ledger after sale void | Rent allocation removed; linked payments excluded as integrity exceptions. |
| P&L filter change | Totals recompute over all matching rows; pagination does not change totals. |
| P&L unknown cost | Excluded from net and reported explicitly. |
| P&L export | Matches the on-screen filtered projection. |
| Current payables date filter | Filters by last contributing transaction; amount remains the full current balance. |

### 16.7 Notifications, import/export, admin

| Scenario | Expected outcome |
|---|---|
| Two contacts on an unpaid bill | Risk escalates to Very High. |
| Mark paid from notifications | Bill paid, reminders closed, no accounting receipt created. |
| Callback without future time | Rejected. |
| Import without pandas | Fails closed with a clear message. |
| Import with failed backup | Blocked; no data changed. |
| Full DB import | Target backed up; clean-and-copy preserves primary keys and FK order; integrity/FK/parity verified; report stored. |
| Selected wipe without confirmation text | Rejected. |
| Wipe with failed pre-backup | Blocked. |
| Accounts-domain wipe with failed integrity check | Restored from the pre-wipe snapshot. |

---

## 17. Known quirks, dormant features, and legacy compatibility

These behaviours exist in the source and must be preserved (or explicitly documented as disabled) so the rebuilt app behaves like the original.

1. **Single-store mode.** Tenant routes (`/tenants*`) return 404; tenant export/restore return 404; scope is always the full dataset. Root-only app-upgrade endpoints are disabled.
2. **Legacy void URLs.** `/void_transaction` and `/delete_transaction` hard-delete through the shared service; `/delete_bill` is admin-only. Entry delete still soft-voids. Restore endpoints exist for the main document types.
3. **Mixed transactions redirect.** `/mixed_transactions` redirects to tracking filtered by Mixed Transaction.
4. **Inventory redirects.** `/inventory/inventory_log` redirects; `/daily_transactions` and `/stock_summary` redirect to the inventory module.
5. **FBM rental models.** FbmRental/FbmRentalItem/FbmClient and cash-drawer tables exist and are exported/wiped, but the rental UI is not part of the active navigation; cash-drawer outflows still feed the legacy expenditures KPI.
6. **Data Lab writes directly.** It is a correction tool, not a guarded import; it updates PendingBill/Entry client fields and clears basket rows.
7. **Email delivery removed.** Staff emails are stored, but the email-file action tells the user to download instead.
8. **Dummy data removed.** `/generate_dummy_data` flashes that the feature was permanently removed; `/delete_all_data` redirects to settings.
9. **Pending bill “paid” is a tracker flag.** It does not create a receipt; accounting receipts come only from the payment services.
10. **GRN landed costs are not in FIFO unit cost.** Loading/freight/tax/other expenses affect the GRN total and supplier payable but are not allocated into lot unit costs or silently expensed in P&L.
11. **Booking advances are not revenue.** P&L recognizes revenue on supply; the financial position panel shows advances separately.
12. **Legacy float and minor-unit mirrors coexist.** New writes maintain both; reads prefer minor units where present and fall back to floats.
13. **Manual bill normalization.** Manual numbers are stored as `MB NO.<value>`; variant matching is used for lookups and conflict checks.
14. **Open Khata.** A shared master client with code OPEN-KHATA is materialized on demand; walk-in credit sales attach to it while keeping the typed customer name.
15. **Backdate policy.** Non-admin users are restricted from backdated GRN/dispatch edits; the restriction flag is per-user and enforced server-side.
16. **Fresh-start Cash Flow.** Opt-in per session; hides earlier entries for today and zeroes the report opening without changing account balances.
17. **Reconciliation immutability.** Account reconciliation adjustments cannot be edited/deleted; daily locks can be unlocked, but per-account reconciliation history is permanent.
18. **Audit retention.** Hard deletes remove operational rows but keep audit/accounting-audit records; voided rows remain visible in void audit and history filters.

---

## 18. Build order and verification checklist

### 18.1 Recommended build order

1. **Foundation:** app factory, SQLite + foreign keys, Pakistan-time helpers, money/minor-unit helpers, session/auth/CSRF, permission map, base template/theme, flash + audit hooks.
2. **Data model:** all 72 tables with constraints, indexes, relationships, and the bill-number counters; schema version stamp; bootstrap seeds (default admin, default account categories, Open Khata on demand).
3. **Masters:** clients, suppliers, materials/services, delivery persons, accounts classification registry, users/settings.
4. **Shared services:** billing lookup/normalization/conflict, pricing/discount allocation, sales-core booking pool and FIFO, accounting sync markers, payment CRUD, driver payments, void/rebuild, pending-bill rebuild.
5. **Purchasing:** GRN add/edit/delete, consumption projection, dispatch add/edit/delete/import, tracking, stock summary, daily breakdown.
6. **Sales:** bookings, direct sales (all categories, alternates, drafts), returns, invoices, bill view/download, void/restore.
7. **Payments:** client/supplier/driver payments, pending bills, waive-offs.
8. **Accounts:** dashboard/KPIs, receive/pay/transfer, account ledger, reconciliation, daily reconciliation board, cash flow, financial tracker, audit trail.
9. **Ledgers:** client financial/material, supplier, driver, decision, current payables, downloads.
10. **Reports:** P&L projection, filters/explorer, breakdowns, financial position, exports, financial details.
11. **Notifications:** queue/risk, reminders/contacts, staff emails, bill detail.
12. **Import/Export:** templates/preview/execute, master/transfer/raw engines, full DB snapshot, convenience exports.
13. **Admin/maintenance:** settings, void audit, consistency tools, system report, wipes, backups, admin module, Data Lab.
14. **Polish:** empty/error states, print/PDF parity, reference-screen fidelity, permission matrix, regression suite.

### 18.2 Verification checklist

- [ ] All 72 tables exist with the documented columns, foreign keys, and indexes.
- [ ] All registered routes respond with the documented methods and permission gates.
- [ ] Every screen in the screen inventory renders for an admin and respects the permission matrix for a standard user.
- [ ] Bill numbers are unique across document types; manual normalization and variant lookup work.
- [ ] GRN → stock → FIFO consumption → P&L COGS agree for the same lots.
- [ ] Booking → delivery → return → cancellation keeps the booking pool consistent.
- [ ] Every payment creates exactly one source row and the correct AccountTransaction(s); no double counting in dashboards/ledgers.
- [ ] Client/supplier/driver ledgers match their payables/overview projections.
- [ ] Daily reconciliation lock carries counted closings into next-day openings.
- [ ] Cash Flow physical reconciliation ignores display filters and does not mutate account balances.
- [ ] P&L filters never change calculations; unknown costs are disclosed.
- [ ] Import/export round-trips preserve data; full DB snapshot verifies integrity/FK/parity.
- [ ] Wipes require confirmation and pre-backup; failed integrity restores the snapshot.
- [ ] Void/restore and hard-delete paths leave audit evidence and no orphaned effects.
- [ ] Reference screens (Daily Reconciliation board, Financial Tracking matrix) match the provided layouts.

### 18.3 Source verification status

The original repository was inspected and exercised to produce this specification:
- 71 ORM classes / 72 tables inventoried.
- 314 active routes registered and mapped.
- 42 authenticated pages rendered and transcribed (40 direct renders, 2 redirects).
- Regression suite: **413 passed, 6 skipped, 11 subtests passed** (2 warnings) in the inspection environment.
- Both reference PNG screens were transcribed into the UI contract.

A rebuilt implementation should be considered complete only when its own regression suite reproduces these behaviours, not merely when the pages render.

---

## Appendix A. Full data dictionary

71 ORM models map to 71 business tables. The 72nd table, `migration_history`, is schema bookkeeping (applied migration filenames/version stamps) and has no ORM model. Column flags: PK = primary key, UQ = unique, IX = indexed, FK = foreign key target. “Null” means the column accepts NULL. Defaults are the ORM-level defaults; timestamp defaults use Pakistan time.


### Account — table `account`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | — |
| type | VARCHAR(50) | no | Unknown | — |
| category | VARCHAR(20) | no | cash | — |
| source_category | VARCHAR(100) | yes | — | UQ |
| account_type | VARCHAR(50) | no | — | — |
| balance | FLOAT | yes | 0 | — |
| balance_minor | BIGINT | yes | — | — |
| opening_balance | FLOAT | yes | — | — |
| opening_balance_minor | BIGINT | yes | — | — |
| opening_balance_date | DATETIME | yes | — | UQ |
| bank_name | VARCHAR(100) | yes | — | — |
| account_holder_name | VARCHAR(100) | yes | — | — |
| account_number | VARCHAR(50) | yes | — | — |
| branch_code | VARCHAR(50) | yes | — | — |
| is_active | BOOLEAN | yes | True | UQ |
| revision | INTEGER | yes | 1 | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | UQ |
| updated_by | VARCHAR(80) | yes | — | — |
| note | VARCHAR(500) | yes | — | — |
| class_category | VARCHAR(50) | yes | — | UQ |
| class_subcategory | VARCHAR(80) | yes | — | UQ |
| class_account_type | VARCHAR(100) | yes | — | UQ |
| channel | VARCHAR(30) | yes | — | UQ |
| cash_location | VARCHAR(120) | yes | — | — |
| cash_responsible | VARCHAR(120) | yes | — | — |
| wallet_provider | VARCHAR(100) | yes | — | — |
| wallet_number | VARCHAR(80) | yes | — | — |
| wallet_holder | VARCHAR(120) | yes | — | — |
| linked_entity_type | VARCHAR(30) | yes | — | UQ |
| linked_client_id | INTEGER | yes | — | UQ, FK → client.id |
| linked_supplier_id | INTEGER | yes | — | UQ, FK → supplier.id |
| linked_party_name | VARCHAR(160) | yes | — | — |
| account_status | VARCHAR(20) | yes | active | UQ |

Indexes: ix_account_class_category ['class_category'] unique=False; ix_account_channel ['channel'] unique=False; ix_account_account_status ['account_status'] unique=False; ix_account_is_active ['is_active'] unique=False; ix_account_created_at ['created_at'] unique=False; ix_account_opening_balance_date ['opening_balance_date'] unique=False; ix_account_class_subcategory ['class_subcategory'] unique=False; ix_account_source_category ['source_category'] unique=False; ix_account_linked_client_id ['linked_client_id'] unique=False; ix_account_updated_at ['updated_at'] unique=False; ix_account_linked_supplier_id ['linked_supplier_id'] unique=False; ix_account_class_account_type ['class_account_type'] unique=False; ix_account_linked_entity_type ['linked_entity_type'] unique=False.

Relationships: supplier_payments -> SupplierPayment uselist=True; grn_payments -> GRN uselist=True; booking_advances -> Booking uselist=True; client_payments -> Payment uselist=True; direct_sale_payments -> DirectSale uselist=True; outgoing_transactions -> AccountTransaction uselist=True; incoming_transactions -> AccountTransaction uselist=True; reconciliations -> AccountReconciliation uselist=True; delivery_person_payments -> DeliveryPersonPayment uselist=True; fbm_payments -> FBMRental uselist=True.

### AccountCategory — table `account_category`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | UQ |
| note | VARCHAR(300) | yes | — | — |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_account_category_created_at ['created_at'] unique=False; ix_account_category_name ['name'] unique=False.

### AccountReconciliation — table `account_reconciliation`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| account_id | INTEGER | no | — | UQ, FK → account.id |
| previous_reconciliation_id | INTEGER | yes | — | UQ, FK → account_reconciliation.id |
| adjustment_transaction_id | INTEGER | yes | — | UQ |
| reconciliation_date | DATE | no | — | UQ |
| period_start_at | DATETIME | yes | — | — |
| period_end_at | DATETIME | yes | — | — |
| previous_balance | FLOAT | yes | 0 | — |
| opening_balance | FLOAT | yes | 0 | — |
| transaction_in | FLOAT | yes | 0 | — |
| transaction_out | FLOAT | yes | 0 | — |
| transaction_net | FLOAT | yes | 0 | — |
| expected_balance | FLOAT | yes | 0 | — |
| actual_balance | FLOAT | yes | 0 | — |
| difference | FLOAT | yes | 0 | — |
| adjustment_amount | FLOAT | yes | 0 | — |
| final_reconciled_balance | FLOAT | yes | 0 | — |
| previous_balance_minor | BIGINT | yes | — | — |
| opening_balance_minor | BIGINT | yes | — | — |
| transaction_in_minor | BIGINT | yes | — | — |
| transaction_out_minor | BIGINT | yes | — | — |
| transaction_net_minor | BIGINT | yes | — | — |
| expected_balance_minor | BIGINT | yes | — | — |
| actual_balance_minor | BIGINT | yes | — | — |
| difference_minor | BIGINT | yes | — | — |
| final_reconciled_balance_minor | BIGINT | yes | — | — |
| difference_type | VARCHAR(20) | yes | Matched | UQ |
| status | VARCHAR(20) | yes | Reconciled | UQ |
| note | VARCHAR(500) | yes | — | — |
| created_by_id | INTEGER | yes | — | — |
| created_by | VARCHAR(80) | yes | — | — |
| created_ip | VARCHAR(80) | yes | — | — |
| session_id | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_account_reconciliation_updated_at ['updated_at'] unique=False; ix_account_reconciliation_previous_reconciliation_id ['previous_reconciliation_id'] unique=False; ix_account_reconciliation_account_id ['account_id'] unique=False; ix_account_reconciliation_difference_type ['difference_type'] unique=False; ix_account_reconciliation_reconciliation_date ['reconciliation_date'] unique=False; ix_account_reconciliation_created_at ['created_at'] unique=False; ix_account_reconciliation_adjustment_transaction_id ['adjustment_transaction_id'] unique=False; ix_account_reconciliation_status ['status'] unique=False.

Relationships: account -> Account uselist=False; previous_reconciliation -> AccountReconciliation uselist=False.

### AccountTransaction — table `account_transaction`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| from_account_id | INTEGER | yes | — | UQ, FK → account.id |
| to_account_id | INTEGER | yes | — | UQ, FK → account.id |
| amount | FLOAT | yes | 0 | — |
| amount_minor | BIGINT | yes | — | — |
| description | VARCHAR(200) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | UQ |
| note | VARCHAR(500) | yes | — | — |
| transaction_type | VARCHAR(50) | yes | — | UQ |
| source_type | VARCHAR(50) | yes | — | UQ |
| source_id | INTEGER | yes | — | UQ |
| reconciliation_id | INTEGER | yes | — | UQ, FK → account_reconciliation.id |
| reason | VARCHAR(300) | yes | — | — |
| idempotency_key | VARCHAR(64) | yes | — | UQ |
| created_by | VARCHAR(80) | yes | — | — |
| voided_by | VARCHAR(80) | yes | — | — |
| voided_at | DATETIME | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_account_transaction_transaction_type ['transaction_type'] unique=False; ix_account_transaction_reconciliation_id ['reconciliation_id'] unique=False; ix_account_transaction_idempotency_key ['idempotency_key'] unique=False; ix_account_transaction_is_void ['is_void'] unique=False; ix_account_transaction_source_type ['source_type'] unique=False; ix_account_transaction_from_account_id ['from_account_id'] unique=False; ix_account_transaction_to_account_id ['to_account_id'] unique=False; ix_account_transaction_created_at ['created_at'] unique=False; ix_account_transaction_date_posted ['date_posted'] unique=False; ix_account_transaction_source_id ['source_id'] unique=False.

Relationships: from_account -> Account uselist=False; to_account -> Account uselist=False.

### AccountingAuditLog — table `accounting_audit_log`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | VARCHAR(36) | no | <lambda> | PK |
| module | VARCHAR(50) | no | — | UQ |
| action | VARCHAR(30) | no | — | UQ |
| entity_type | VARCHAR(50) | no | — | UQ |
| entity_id | INTEGER | yes | — | UQ |
| user_id | INTEGER | yes | — | UQ |
| username | VARCHAR(80) | yes | — | UQ |
| ip_address | VARCHAR(80) | yes | — | — |
| session_id | VARCHAR(80) | yes | — | — |
| before_json | TEXT | yes | — | — |
| after_json | TEXT | yes | — | — |
| amount_before_minor | BIGINT | yes | — | — |
| amount_after_minor | BIGINT | yes | — | — |
| account_before_id | INTEGER | yes | — | — |
| account_after_id | INTEGER | yes | — | — |
| party_before_id | INTEGER | yes | — | — |
| party_after_id | INTEGER | yes | — | — |
| reason | VARCHAR(500) | yes | — | — |
| created_at | DATETIME | no | pk_model_now | UQ |

Indexes: ix_accounting_audit_log_username ['username'] unique=False; ix_accounting_audit_log_module ['module'] unique=False; ix_accounting_audit_log_entity_type ['entity_type'] unique=False; ix_accounting_audit_log_entity_id ['entity_id'] unique=False; ix_accounting_audit_log_created_at ['created_at'] unique=False; ix_accounting_audit_log_user_id ['user_id'] unique=False; ix_accounting_audit_log_action ['action'] unique=False.

### AuditLog — table `audit_log`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | VARCHAR(36) | no | <lambda> | PK |
| user_id | INTEGER | yes | — | — |
| username | VARCHAR(80) | yes | — | UQ |
| action | VARCHAR(200) | no | — | — |
| details | VARCHAR(1000) | yes | — | — |
| timestamp | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_audit_log_username ['username'] unique=False; ix_audit_log_timestamp ['timestamp'] unique=False.

### BillCounter — table `bill_counter`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| namespace | VARCHAR(12) | no | GEN | UQ |
| count | INTEGER | yes | 1000 | — |

Indexes: ix_bill_counter_namespace ['namespace'] unique=False.

### Booking — table `booking`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_name | VARCHAR(100) | yes | — | — |
| amount | FLOAT | yes | 0 | — |
| paid_amount | FLOAT | yes | 0 | — |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| photo_path | VARCHAR(200) | yes | — | — |
| photo_url | VARCHAR(500) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | — |
| note | VARCHAR(500) | yes | — | — |
| discount | FLOAT | yes | 0 | — |
| discount_reason | VARCHAR(200) | yes | — | — |
| receive_in_account_id | INTEGER | yes | — | FK → account.id |

Indexes: ix_booking_date_posted ['date_posted'] unique=False.

Relationships: items -> BookingItem uselist=True; receive_in_account -> Account uselist=False.

### BookingAllocation — table `booking_allocation`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| sale_id | INTEGER | no | — | UQ, FK → direct_sale.id |
| sale_item_id | INTEGER | no | — | UQ, FK → direct_sale_item.id |
| booking_item_id | INTEGER | no | — | UQ, FK → booking_item.id |
| qty | FLOAT | yes | 0 | — |
| is_void | BOOLEAN | yes | False | — |

Indexes: ix_booking_allocation_sale_item_id ['sale_item_id'] unique=False; ix_booking_allocation_sale_id ['sale_id'] unique=False; ix_booking_allocation_booking_item_id ['booking_item_id'] unique=False.

Relationships: sale -> DirectSale uselist=False; sale_item -> DirectSaleItem uselist=False; booking_item -> BookingItem uselist=False.

### BookingAllocationRepairArchive — table `booking_allocation_repair_archive`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| original_allocation_id | INTEGER | no | — | UQ |
| sale_id | INTEGER | no | — | — |
| sale_item_id | INTEGER | no | — | — |
| booking_item_id | INTEGER | no | — | — |
| qty | FLOAT | yes | 0 | — |
| was_void | BOOLEAN | yes | False | — |
| violations | VARCHAR(200) | no | — | — |
| repair_reason | VARCHAR(500) | no | — | — |
| repair_run_id | VARCHAR(64) | no | — | UQ |
| source_row_json | TEXT | no | — | — |
| sale_snapshot_json | TEXT | yes | — | — |
| sale_item_snapshot_json | TEXT | yes | — | — |
| booking_item_snapshot_json | TEXT | yes | — | — |
| booking_snapshot_json | TEXT | yes | — | — |
| archived_at | DATETIME | no | pk_model_now | UQ |

Indexes: ix_booking_allocation_repair_archive_original_allocation_id ['original_allocation_id'] unique=False; ix_booking_allocation_repair_archive_repair_run_id ['repair_run_id'] unique=False; ix_booking_allocation_repair_archive_archived_at ['archived_at'] unique=False.

### BookingItem — table `booking_item`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| booking_id | INTEGER | no | — | FK → booking.id |
| material_name | VARCHAR(100) | yes | — | — |
| qty | FLOAT | yes | 0 | — |
| price_at_time | FLOAT | yes | 0 | — |
| discount | FLOAT | yes | 0 | — |
| discount_reason | VARCHAR(200) | yes | — | — |

Relationships: booking -> Booking uselist=False; allocations -> BookingAllocation uselist=True.

### CashDayAccountPosition — table `cash_day_account_position`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| position_date | DATE | no | — | UQ |
| account_id | INTEGER | no | — | UQ |
| account_name | VARCHAR(100) | yes | — | — |
| opening | FLOAT | yes | 0 | — |
| amount_in | FLOAT | yes | 0 | — |
| amount_out | FLOAT | yes | 0 | — |
| transfer_in | FLOAT | yes | 0 | — |
| transfer_out | FLOAT | yes | 0 | — |
| expected_closing | FLOAT | yes | 0 | — |
| counted | FLOAT | yes | — | — |
| difference | FLOAT | yes | — | — |
| is_locked | BOOLEAN | yes | False | UQ |
| locked_by | VARCHAR(80) | yes | — | — |
| locked_at | DATETIME | yes | — | — |
| updated_by | VARCHAR(80) | yes | — | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_cash_day_account_position_account_id ['account_id'] unique=False; ix_cash_day_account_position_position_date ['position_date'] unique=False; ix_cash_day_account_position_is_locked ['is_locked'] unique=False.

### CashDayLock — table `cash_day_lock`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| lock_date | DATE | no | — | UQ |
| total_expected | FLOAT | yes | 0 | — |
| total_counted | FLOAT | yes | 0 | — |
| difference | FLOAT | yes | 0 | — |
| note | VARCHAR(500) | yes | — | — |
| locked_by | VARCHAR(80) | yes | — | — |
| locked_at | DATETIME | yes | pk_model_now | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_cash_day_lock_lock_date ['lock_date'] unique=False.

### CashFlowCategory — table `cash_flow_category`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(120) | no | — | UQ |
| direction | VARCHAR(10) | yes | both | UQ |
| is_active | BOOLEAN | yes | True | UQ |
| sort_order | INTEGER | yes | 0 | — |
| notes | VARCHAR(300) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_cash_flow_category_direction ['direction'] unique=False; ix_cash_flow_category_is_active ['is_active'] unique=False; ix_cash_flow_category_name ['name'] unique=False.

Relationships: subcategories -> CashFlowSubcategory uselist=True.

### CashFlowDifferenceAdjustment — table `cash_flow_difference_adjustment`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| adjustment_date | DATE | no | — | UQ |
| amount | FLOAT | yes | 0 | — |
| note | VARCHAR(500) | yes | — | — |
| physical_cash_available | FLOAT | yes | — | — |
| calculated_closing | FLOAT | yes | — | — |
| difference | FLOAT | yes | — | — |
| reason | VARCHAR(500) | yes | — | — |
| old_physical_cash | FLOAT | yes | — | — |
| edited_by | VARCHAR(80) | yes | — | — |
| edited_date | DATETIME | yes | — | — |
| edit_count | INTEGER | yes | 0 | — |
| created_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | UQ |

Indexes: ix_cash_flow_difference_adjustment_adjustment_date ['adjustment_date'] unique=False; ix_cash_flow_difference_adjustment_updated_at ['updated_at'] unique=False; ix_cash_flow_difference_adjustment_created_at ['created_at'] unique=False.

Relationships: audit_trail -> CashFlowReconciliationAudit uselist=True.

### CashFlowEntry — table `cash_flow_entry`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| direction | VARCHAR(10) | no | — | UQ |
| amount | FLOAT | yes | 0 | — |
| amount_minor | BIGINT | yes | — | — |
| account_id | INTEGER | yes | — | UQ, FK → account.id |
| destination_account_id | INTEGER | yes | — | UQ, FK → account.id |
| category_id | INTEGER | yes | — | UQ, FK → cash_flow_category.id |
| subcategory_id | INTEGER | yes | — | UQ, FK → cash_flow_subcategory.id |
| party_id | INTEGER | yes | — | UQ, FK → cash_flow_party.id |
| party_name | VARCHAR(160) | yes | — | UQ |
| party_type | VARCHAR(40) | yes | — | UQ |
| description | VARCHAR(200) | yes | — | — |
| note | VARCHAR(500) | yes | — | UQ |
| reference | VARCHAR(80) | yes | — | UQ |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| created_by | VARCHAR(80) | yes | — | UQ |
| updated_by | VARCHAR(80) | yes | — | — |
| source_type | VARCHAR(50) | yes | MANUAL_CASH_FLOW | UQ |
| source_id | INTEGER | yes | — | UQ |
| account_tx_id | INTEGER | yes | — | FK → account_transaction.id |
| is_void | BOOLEAN | yes | False | UQ |
| voided_at | DATETIME | yes | — | — |
| voided_by | VARCHAR(80) | yes | — | — |
| void_reason | VARCHAR(300) | yes | — | — |
| idempotency_key | VARCHAR(64) | yes | — | UQ |
| revision | INTEGER | yes | 1 | — |
| created_at | DATETIME | yes | pk_model_now | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_cash_flow_entry_idempotency_key ['idempotency_key'] unique=False; ix_cash_flow_entry_destination_account_id ['destination_account_id'] unique=False; ix_cash_flow_entry_party_name ['party_name'] unique=False; ix_cash_flow_entry_reference ['reference'] unique=False; ix_cash_flow_entry_source_id ['source_id'] unique=False; ix_cash_flow_entry_category_id ['category_id'] unique=False; ix_cash_flow_entry_party_id ['party_id'] unique=False; ix_cash_flow_entry_party_type ['party_type'] unique=False; ix_cash_flow_entry_created_by ['created_by'] unique=False; ix_cash_flow_entry_account_id ['account_id'] unique=False; ix_cash_flow_entry_direction ['direction'] unique=False; ix_cash_flow_entry_subcategory_id ['subcategory_id'] unique=False; ix_cash_flow_entry_note ['note'] unique=False; ix_cash_flow_entry_date_posted ['date_posted'] unique=False; ix_cash_flow_entry_source_type ['source_type'] unique=False; ix_cash_flow_entry_is_void ['is_void'] unique=False.

Relationships: account -> Account uselist=False; destination_account -> Account uselist=False; category -> CashFlowCategory uselist=False; subcategory -> CashFlowSubcategory uselist=False; party -> CashFlowParty uselist=False; audit_trail -> CashFlowEntryAudit uselist=True.

### CashFlowEntryAudit — table `cash_flow_entry_audit`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| entry_id | INTEGER | no | — | UQ, FK → cash_flow_entry.id |
| action | VARCHAR(20) | no | — | UQ |
| before_json | TEXT | yes | — | — |
| after_json | TEXT | yes | — | — |
| reason | VARCHAR(300) | yes | — | — |
| changed_by | VARCHAR(80) | yes | — | — |
| changed_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_cash_flow_entry_audit_action ['action'] unique=False; ix_cash_flow_entry_audit_changed_at ['changed_at'] unique=False; ix_cash_flow_entry_audit_entry_id ['entry_id'] unique=False.

Relationships: entry -> CashFlowEntry uselist=False.

### CashFlowParty — table `cash_flow_party`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(160) | no | — | UQ |
| party_type | VARCHAR(40) | yes | person | UQ |
| phone | VARCHAR(40) | yes | — | — |
| note | VARCHAR(300) | yes | — | — |
| is_active | BOOLEAN | yes | True | UQ |
| created_at | DATETIME | yes | pk_model_now | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_cash_flow_party_party_type ['party_type'] unique=False; ix_cash_flow_party_is_active ['is_active'] unique=False; ix_cash_flow_party_name ['name'] unique=False.

### CashFlowReconciliationAudit — table `cash_flow_reconciliation_audit`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| reconciliation_id | INTEGER | no | — | UQ, FK → cash_flow_difference_adjustment.id |
| adjustment_date | DATE | no | — | UQ |
| change_type | VARCHAR(20) | no | — | — |
| old_physical_cash | FLOAT | yes | — | — |
| new_physical_cash | FLOAT | yes | — | — |
| old_difference | FLOAT | yes | — | — |
| new_difference | FLOAT | yes | — | — |
| old_reason | VARCHAR(500) | yes | — | — |
| new_reason | VARCHAR(500) | yes | — | — |
| changed_by | VARCHAR(80) | yes | — | — |
| changed_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_cash_flow_reconciliation_audit_reconciliation_id ['reconciliation_id'] unique=False; ix_cash_flow_reconciliation_audit_adjustment_date ['adjustment_date'] unique=False; ix_cash_flow_reconciliation_audit_changed_at ['changed_at'] unique=False.

Relationships: reconciliation -> CashFlowDifferenceAdjustment uselist=False.

### CashFlowSubcategory — table `cash_flow_subcategory`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| category_id | INTEGER | no | — | UQ, FK → cash_flow_category.id |
| name | VARCHAR(120) | no | — | UQ |
| is_active | BOOLEAN | yes | True | UQ |
| notes | VARCHAR(300) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_cash_flow_subcategory_name ['name'] unique=False; ix_cash_flow_subcategory_is_active ['is_active'] unique=False; ix_cash_flow_subcategory_category_id ['category_id'] unique=False.

Relationships: category -> CashFlowCategory uselist=False.

### Client — table `client`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| code | VARCHAR(50) | no | — | — |
| name | VARCHAR(100) | no | — | — |
| phone | VARCHAR(20) | yes | — | — |
| address | VARCHAR(200) | yes | — | — |
| category | VARCHAR(50) | yes | General | — |
| opening_balance | FLOAT | yes | 0 | — |
| opening_balance_date | DATETIME | yes | pk_model_now | UQ |
| is_active | BOOLEAN | yes | True | — |
| transferred_to_id | INTEGER | yes | — | FK → client.id |
| require_manual_invoice | BOOLEAN | yes | False | — |
| book_no | VARCHAR(50) | yes | — | — |
| financial_page | VARCHAR(50) | yes | — | — |
| cement_page | VARCHAR(50) | yes | — | — |
| steel_page | VARCHAR(50) | yes | — | — |
| financial_book_no | VARCHAR(50) | yes | — | — |
| cement_book_no | VARCHAR(50) | yes | — | — |
| steel_book_no | VARCHAR(50) | yes | — | — |
| location_url | VARCHAR(500) | yes | — | — |
| page_notes | VARCHAR(300) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_client_opening_balance_date ['opening_balance_date'] unique=False; ix_client_created_at ['created_at'] unique=False.

Relationships: payment_records -> Payment uselist=True.

### ClientCategory — table `client_category`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(50) | no | — | IX |
| is_active | BOOLEAN | no | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_client_category_created_at ['created_at'] unique=False.

### Delivery — table `delivery`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_name | VARCHAR(100) | yes | — | — |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| photo_path | VARCHAR(200) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_delivery_date_posted ['date_posted'] unique=False.

Relationships: items -> DeliveryItem uselist=True.

### DeliveryItem — table `delivery_item`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| delivery_id | INTEGER | no | — | FK → delivery.id |
| product | VARCHAR(100) | yes | — | — |
| qty | FLOAT | yes | 0 | — |

Relationships: delivery -> Delivery uselist=False.

### DeliveryPerson — table `delivery_person`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | IX |
| phone | VARCHAR(30) | yes | — | — |
| opening_balance | FLOAT | yes | 0 | — |
| opening_balance_date | DATETIME | yes | pk_model_now | UQ |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_delivery_person_created_at ['created_at'] unique=False; ix_delivery_person_opening_balance_date ['opening_balance_date'] unique=False.

### DeliveryPersonPayment — table `delivery_person_payment`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| delivery_person_id | INTEGER | no | — | UQ, FK → delivery_person.id |
| sale_id | INTEGER | yes | — | UQ, FK → direct_sale.id |
| allocation_id | INTEGER | yes | — | UQ, FK → sale_delivery_persons.id |
| amount_paid | FLOAT | yes | 0 | — |
| amount_paid_minor | BIGINT | yes | — | — |
| waive_off_amount | FLOAT | yes | 0 | — |
| waive_off_minor | BIGINT | yes | — | — |
| payment_account_id | INTEGER | yes | — | UQ, FK → account.id |
| method | VARCHAR(50) | yes | — | — |
| reference | VARCHAR(50) | yes | — | UQ |
| note | VARCHAR(500) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| idempotency_key | VARCHAR(64) | yes | — | UQ, IX |
| revision | INTEGER | yes | 1 | — |
| created_by | VARCHAR(80) | yes | — | — |
| updated_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | UQ |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_delivery_person_payment_updated_at ['updated_at'] unique=False; ix_delivery_person_payment_reference ['reference'] unique=False; ix_delivery_person_payment_payment_account_id ['payment_account_id'] unique=False; ix_delivery_person_payment_date_posted ['date_posted'] unique=False; ix_delivery_person_payment_created_at ['created_at'] unique=False; ix_delivery_person_payment_allocation_id ['allocation_id'] unique=False; ix_delivery_person_payment_idempotency_key ['idempotency_key'] unique=True; ix_delivery_person_payment_is_void ['is_void'] unique=False; ix_delivery_person_payment_delivery_person_id ['delivery_person_id'] unique=False; ix_delivery_person_payment_sale_id ['sale_id'] unique=False.

Relationships: delivery_person -> DeliveryPerson uselist=False; sale -> DirectSale uselist=False; allocation -> SaleDeliveryPerson uselist=False; payment_account -> Account uselist=False.

### DeliveryRent — table `delivery_rent`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| sale_id | INTEGER | yes | — | UQ, FK → direct_sale.id |
| delivery_person_name | VARCHAR(100) | no | — | UQ |
| bill_no | VARCHAR(50) | yes | — | UQ |
| amount | FLOAT | yes | 0 | — |
| note | VARCHAR(500) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| created_by | VARCHAR(80) | yes | — | — |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_delivery_rent_delivery_person_name ['delivery_person_name'] unique=False; ix_delivery_rent_bill_no ['bill_no'] unique=False; ix_delivery_rent_is_void ['is_void'] unique=False; ix_delivery_rent_date_posted ['date_posted'] unique=False; ix_delivery_rent_sale_id ['sale_id'] unique=False.

Relationships: sale -> DirectSale uselist=False.

### DirectSale — table `direct_sale`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| idempotency_key | VARCHAR(64) | yes | — | UQ, IX |
| idempotency_payload_hash | VARCHAR(64) | yes | — | UQ |
| client_name | VARCHAR(100) | yes | — | — |
| client_code | VARCHAR(50) | yes | — | UQ |
| category | VARCHAR(50) | yes | — | — |
| amount | FLOAT | yes | 0 | — |
| paid_amount | FLOAT | yes | 0 | — |
| discount | FLOAT | yes | 0 | — |
| discount_reason | VARCHAR(200) | yes | — | — |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| photo_path | VARCHAR(200) | yes | — | — |
| photo_url | VARCHAR(500) | yes | — | — |
| invoice_id | INTEGER | yes | — | FK → invoice.id |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | — |
| note | VARCHAR(500) | yes | — | — |
| driver_name | VARCHAR(100) | yes | — | — |
| rent_item_revenue | FLOAT | yes | 0 | — |
| delivery_rent_cost | FLOAT | yes | 0 | — |
| rent_variance_loss | FLOAT | yes | 0 | — |
| payment_method | VARCHAR(50) | yes | — | — |
| payment_account_id | INTEGER | yes | — | FK → account.id |
| bank_name | VARCHAR(100) | yes | — | — |
| account_name | VARCHAR(100) | yes | — | — |
| account_no | VARCHAR(50) | yes | — | — |

Indexes: ix_direct_sale_idempotency_key ['idempotency_key'] unique=True; ix_direct_sale_client_code ['client_code'] unique=False; ix_direct_sale_idempotency_payload_hash ['idempotency_payload_hash'] unique=False; ix_direct_sale_date_posted ['date_posted'] unique=False.

Relationships: items -> DirectSaleItem uselist=True; payment_account -> Account uselist=False; booking_allocations -> BookingAllocation uselist=True; invoice -> Invoice uselist=False; grn_allocations -> GRNAllocation uselist=True; delivery_rents -> DeliveryRent uselist=True; delivery_person_allocations -> SaleDeliveryPerson uselist=True.

### DirectSaleDraft — table `direct_sale_draft`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_code | VARCHAR(50) | yes | — | — |
| client_name | VARCHAR(100) | yes | — | — |
| manual_client_name | VARCHAR(100) | yes | — | — |
| category | VARCHAR(50) | yes | — | — |
| driver_name | VARCHAR(100) | yes | — | — |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| item_count | INTEGER | yes | 0 | — |
| total_qty | FLOAT | yes | 0 | — |
| total_amount | FLOAT | yes | 0 | — |
| payload | TEXT | yes | — | — |
| created_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_direct_sale_draft_updated_at ['updated_at'] unique=False; ix_direct_sale_draft_created_at ['created_at'] unique=False.

### DirectSaleItem — table `direct_sale_item`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| sale_id | INTEGER | no | — | FK → direct_sale.id |
| product_name | VARCHAR(100) | yes | — | — |
| qty | FLOAT | yes | 0 | — |
| price_at_time | FLOAT | yes | 0 | — |
| grn_item_id | INTEGER | yes | — | FK → grn_item.id |
| cost_rate_at_sale | FLOAT | yes | — | — |
| discount | FLOAT | yes | 0 | — |
| discount_kind | VARCHAR(20) | yes | allocated | — |
| discount_reason | VARCHAR(200) | yes | — | — |

Relationships: grn_item -> GRNItem uselist=False; booking_allocations -> BookingAllocation uselist=True; direct_sale -> DirectSale uselist=False; grn_allocations -> GRNAllocation uselist=True.

### Entry — table `entry`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| date | VARCHAR(20) | yes | — | — |
| time | VARCHAR(20) | yes | — | — |
| type | VARCHAR(10) | yes | — | — |
| material | VARCHAR(100) | yes | — | — |
| client | VARCHAR(100) | yes | — | — |
| client_code | VARCHAR(50) | yes | — | — |
| client_category | VARCHAR(50) | yes | — | — |
| qty | FLOAT | yes | 0 | — |
| bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| nimbus_no | VARCHAR(50) | yes | — | — |
| invoice_id | INTEGER | yes | — | FK → invoice.id |
| created_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | — |
| transaction_category | VARCHAR(50) | yes | — | — |
| driver_name | VARCHAR(100) | yes | — | — |
| note | VARCHAR(500) | yes | — | — |
| booked_material | VARCHAR(100) | yes | — | — |
| is_alternate | BOOLEAN | yes | False | — |
| source_module | VARCHAR(50) | yes | — | UQ |
| source_table | VARCHAR(50) | yes | — | UQ |
| source_id | INTEGER | yes | — | UQ |
| source_bill_no | VARCHAR(50) | yes | — | UQ |
| transaction_type | VARCHAR(50) | yes | — | UQ |

Indexes: ix_entry_created_at ['created_at'] unique=False; ix_entry_source_id ['source_id'] unique=False; ix_entry_source_module ['source_module'] unique=False; ix_entry_source_bill_no ['source_bill_no'] unique=False; ix_entry_source_table ['source_table'] unique=False; ix_entry_transaction_type ['transaction_type'] unique=False.

Relationships: invoice -> Invoice uselist=False.

### FBMClient — table `fbm_client`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| full_name | VARCHAR(150) | no | — | — |
| address | VARCHAR(250) | yes | — | — |
| phone | VARCHAR(50) | yes | — | — |
| identity_card | VARCHAR(100) | yes | — | — |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_fbm_client_created_at ['created_at'] unique=False.

Relationships: rentals -> FBMRental uselist=True.

### FBMRental — table `fbm_rental`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_id | INTEGER | no | — | FK → fbm_client.id |
| item_id | INTEGER | no | — | FK → fbm_rental_item.id |
| qty | INTEGER | yes | 1 | — |
| rent_per_unit | FLOAT | yes | 0 | — |
| total_amount | FLOAT | yes | 0 | — |
| qty_returned | INTEGER | yes | 0 | — |
| paid_amount | FLOAT | yes | 0 | — |
| discount_amount | FLOAT | yes | 0 | — |
| start_datetime | DATETIME | yes | pk_model_now | UQ |
| return_datetime | DATETIME | yes | — | UQ |
| status | VARCHAR(20) | yes | active | — |
| payment_account_id | INTEGER | yes | — | FK → account.id |
| note | VARCHAR(500) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_fbm_rental_start_datetime ['start_datetime'] unique=False; ix_fbm_rental_return_datetime ['return_datetime'] unique=False; ix_fbm_rental_created_at ['created_at'] unique=False.

Relationships: payment_account -> Account uselist=False; item -> FBMRentalItem uselist=False; client -> FBMClient uselist=False.

### FBMRentalItem — table `fbm_rental_item`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(120) | no | — | — |
| opening_qty | INTEGER | yes | 0 | — |
| available_qty | INTEGER | yes | 0 | — |
| rent_per_day | FLOAT | yes | 0 | — |
| is_void | BOOLEAN | yes | False | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | — |

Indexes: ix_fbm_rental_item_created_at ['created_at'] unique=False.

Relationships: rentals -> FBMRental uselist=True.

### FbmCashDrawerCategory — table `fbm_cash_drawer_category`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | UQ |
| is_active | BOOLEAN | yes | True | UQ |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_fbm_cash_drawer_category_is_active ['is_active'] unique=False; ix_fbm_cash_drawer_category_created_at ['created_at'] unique=False; ix_fbm_cash_drawer_category_name ['name'] unique=False.

### FbmCashDrawerEntry — table `fbm_cash_drawer_entry`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| entry_type | VARCHAR(10) | yes | out | UQ |
| amount | FLOAT | yes | 0 | — |
| category | VARCHAR(100) | yes | — | — |
| method | VARCHAR(20) | yes | Cash | UQ |
| note | VARCHAR(500) | yes | — | — |
| source | VARCHAR(20) | yes | manual | UQ |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| created_by | VARCHAR(80) | yes | — | — |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_fbm_cash_drawer_entry_entry_type ['entry_type'] unique=False; ix_fbm_cash_drawer_entry_source ['source'] unique=False; ix_fbm_cash_drawer_entry_is_void ['is_void'] unique=False; ix_fbm_cash_drawer_entry_method ['method'] unique=False; ix_fbm_cash_drawer_entry_date_posted ['date_posted'] unique=False.

### FollowUpContact — table `follow_up_contact`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| pending_bill_id | INTEGER | no | — | FK → pending_bill.id |
| reminder_id | INTEGER | yes | — | FK → follow_up_reminder.id |
| contacted_at | DATETIME | no | pk_model_now | — |
| channel | VARCHAR(30) | yes | Call | — |
| response | VARCHAR(200) | yes | — | — |
| outcome | VARCHAR(20) | yes | — | — |
| note | VARCHAR(500) | yes | — | — |
| created_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_follow_up_contact_created_at ['created_at'] unique=False.

Relationships: pending_bill -> PendingBill uselist=False; reminder -> FollowUpReminder uselist=False.

### FollowUpReminder — table `follow_up_reminder`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| pending_bill_id | INTEGER | no | — | FK → pending_bill.id |
| remind_at | DATETIME | no | — | — |
| note | VARCHAR(500) | yes | — | — |
| is_done | BOOLEAN | yes | False | — |
| alerted_at | DATETIME | yes | — | — |
| acknowledged_at | DATETIME | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_follow_up_reminder_created_at ['created_at'] unique=False.

Relationships: pending_bill -> PendingBill uselist=False; closure_contact_logs -> FollowUpContact uselist=True.

### FutureAccountAuditLog — table `future_account_audit_log`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| message | VARCHAR(500) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_future_account_audit_log_created_at ['created_at'] unique=False.

### GRN — table `grn`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| supplier_id | INTEGER | yes | — | FK → supplier.id |
| supplier | VARCHAR(100) | yes | — | — |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| photo_path | VARCHAR(200) | yes | — | — |
| photo_url | VARCHAR(500) | yes | — | — |
| loading_cost | FLOAT | yes | 0 | — |
| freight_cost | FLOAT | yes | 0 | — |
| other_expense | FLOAT | yes | 0 | — |
| adjustment_amount | FLOAT | yes | 0 | — |
| discount | FLOAT | yes | 0 | — |
| paid_amount | FLOAT | yes | 0 | — |
| payment_type | VARCHAR(50) | yes | — | — |
| payment_account_id | INTEGER | yes | — | FK → account.id |
| tax_percent | FLOAT | yes | 0 | — |
| tax_amount | FLOAT | yes | 0 | — |
| tax_type | VARCHAR(50) | yes | — | — |
| bank_name | VARCHAR(100) | yes | — | — |
| account_name | VARCHAR(100) | yes | — | — |
| account_no | VARCHAR(50) | yes | — | — |
| supplier_invoice_no | VARCHAR(50) | yes | — | — |
| due_date | DATE | yes | — | — |
| bill_date | DATE | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | — |
| note | VARCHAR(500) | yes | — | — |

Indexes: ix_grn_date_posted ['date_posted'] unique=False.

Relationships: items -> GRNItem uselist=True; supplier_rel -> Supplier uselist=False; payment_account -> Account uselist=False.

### GRNAllocation — table `grn_allocation`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| sale_id | INTEGER | no | — | UQ, FK → direct_sale.id |
| sale_item_id | INTEGER | no | — | UQ, FK → direct_sale_item.id |
| grn_item_id | INTEGER | no | — | UQ, FK → grn_item.id |
| qty | FLOAT | yes | 0 | — |
| cost_rate | FLOAT | yes | 0 | — |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_grn_allocation_grn_item_id ['grn_item_id'] unique=False; ix_grn_allocation_is_void ['is_void'] unique=False; ix_grn_allocation_sale_id ['sale_id'] unique=False; ix_grn_allocation_sale_item_id ['sale_item_id'] unique=False.

Relationships: sale -> DirectSale uselist=False; sale_item -> DirectSaleItem uselist=False; grn_item -> GRNItem uselist=False.

### GRNItem — table `grn_item`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| grn_id | INTEGER | no | — | FK → grn.id |
| mat_name | VARCHAR(100) | yes | — | — |
| qty | FLOAT | yes | 0 | — |
| price_at_time | FLOAT | yes | 0 | — |
| is_void | BOOLEAN | yes | False | UQ |
| is_locked | BOOLEAN | yes | False | UQ |

Indexes: ix_grn_item_is_void ['is_void'] unique=False; ix_grn_item_is_locked ['is_locked'] unique=False.

Relationships: grn -> GRN uselist=False; sale_items -> DirectSaleItem uselist=True; allocations -> GRNAllocation uselist=True.

### ImportHistoryEntry — table `import_history_entry`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| import_job_id | INTEGER | no | — | UQ, FK → import_job.id |
| event_type | VARCHAR(50) | no | — | — |
| sheet_name | VARCHAR(50) | yes | — | — |
| row_number | INTEGER | yes | — | — |
| message | TEXT | yes | — | — |
| status_snapshot | JSON | yes | — | — |
| recorded_at | DATETIME | no | pk_model_now | UQ |
| created_by | VARCHAR(100) | yes | — | — |
| created_at | DATETIME | no | pk_model_now | — |

Indexes: ix_import_history_entry_import_job_id ['import_job_id'] unique=False; ix_import_history_entry_recorded_at ['recorded_at'] unique=False.

Relationships: job -> ImportJob uselist=False.

### ImportJob — table `import_job`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| upload_id | INTEGER | no | — | IX, FK → import_upload.id |
| started_at | DATETIME | yes | — | — |
| finished_at | DATETIME | yes | — | — |
| status | VARCHAR(20) | no | queued | UQ |
| current_sheet | VARCHAR(50) | yes | — | — |
| current_row | INTEGER | yes | 0 | — |
| total_rows | INTEGER | yes | 0 | — |
| processed_rows | INTEGER | yes | 0 | — |
| error_message | TEXT | yes | — | — |
| import_stats | JSON | yes | — | — |
| initiated_by | INTEGER | yes | — | FK → user.id |
| created_at | DATETIME | no | pk_model_now | — |
| updated_at | DATETIME | no | pk_model_now (on update pk_model_now) | — |

Indexes: ix_import_job_status ['status'] unique=False.

Relationships: upload -> ImportUpload uselist=False; history_entries -> ImportHistoryEntry uselist=True.

### ImportUpload — table `import_upload`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| upload_id | VARCHAR(36) | no | — | UQ, IX |
| filename | VARCHAR(255) | no | — | — |
| stored_filename | VARCHAR(255) | no | — | — |
| size_bytes | INTEGER | no | — | — |
| uploaded_by | INTEGER | yes | — | FK → user.id |
| uploaded_at | DATETIME | no | pk_model_now | — |
| status | VARCHAR(20) | no | uploaded | UQ |
| notes | TEXT | yes | — | — |
| created_at | DATETIME | no | pk_model_now | — |
| updated_at | DATETIME | no | pk_model_now (on update pk_model_now) | — |

Indexes: ix_import_upload_upload_id ['upload_id'] unique=True; ix_import_upload_status ['status'] unique=False.

Relationships: import_job -> ImportJob uselist=False.

### Invoice — table `invoice`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_code | VARCHAR(50) | yes | — | — |
| client_name | VARCHAR(100) | yes | — | — |
| invoice_no | VARCHAR(50) | yes | — | — |
| is_manual | BOOLEAN | yes | False | — |
| date | DATE | yes | — | — |
| total_amount | FLOAT | yes | 0 | — |
| balance | FLOAT | yes | 0 | — |
| status | VARCHAR(20) | yes | OPEN | — |
| is_cash | BOOLEAN | yes | False | — |
| created_at | VARCHAR(50) | yes | — | — |
| created_by | VARCHAR(80) | yes | — | — |
| is_void | BOOLEAN | yes | False | — |
| note | VARCHAR(500) | yes | — | — |

Relationships: entries -> Entry uselist=True; direct_sales -> DirectSale uselist=True.

### Material — table `material`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| code | VARCHAR(50) | no | — | — |
| name | VARCHAR(100) | no | — | — |
| category_id | INTEGER | yes | — | UQ, FK → material_category.id |
| unit_price | FLOAT | yes | 0 | — |
| total | FLOAT | yes | 0 | — |
| unit | VARCHAR(20) | yes | Bags | — |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_material_created_at ['created_at'] unique=False; ix_material_category_id ['category_id'] unique=False.

Relationships: category -> MaterialCategory uselist=False.

### MaterialCategory — table `material_category`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | — |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_material_category_created_at ['created_at'] unique=False.

Relationships: materials -> Material uselist=True.

### MaterialReturn — table `material_return`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_name | VARCHAR(100) | yes | — | — |
| return_type | VARCHAR(20) | yes | normal | — |
| amount | FLOAT | yes | 0 | — |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| note | VARCHAR(500) | yes | — | — |
| payment_id | INTEGER | yes | — | UQ, FK → payment.id |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_material_return_date_posted ['date_posted'] unique=False; ix_material_return_is_void ['is_void'] unique=False; ix_material_return_payment_id ['payment_id'] unique=False.

Relationships: items -> MaterialReturnItem uselist=True; payment -> Payment uselist=False.

### MaterialReturnItem — table `material_return_item`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| material_return_id | INTEGER | no | — | UQ, FK → material_return.id |
| material_name | VARCHAR(100) | yes | — | — |
| qty | FLOAT | yes | 0 | — |
| unit_rate | FLOAT | yes | 0 | — |
| rent_rate | FLOAT | yes | 0 | — |
| price_at_time | FLOAT | yes | 0 | — |
| discount_reversed | FLOAT | yes | 0 | — |

Indexes: ix_material_return_item_material_return_id ['material_return_id'] unique=False.

Relationships: material_return -> MaterialReturn uselist=False.

### MigrationMapping — table `migration_mapping`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| template_type | VARCHAR(40) | no | — | UQ |
| legacy_reference | VARCHAR(150) | no | — | UQ |
| entity_type | VARCHAR(50) | no | — | — |
| entity_id | INTEGER | no | — | — |
| run_id | INTEGER | no | — | UQ, FK → migration_run.id |
| created_at | DATETIME | no | pk_model_now | — |

Indexes: ix_migration_mapping_template_type ['template_type'] unique=False; ix_migration_mapping_legacy_reference ['legacy_reference'] unique=False; ix_migration_mapping_run_id ['run_id'] unique=False.

### MigrationRow — table `migration_row`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| run_id | INTEGER | no | — | UQ, FK → migration_run.id |
| source_sheet | VARCHAR(80) | no | — | — |
| source_row | INTEGER | no | — | — |
| legacy_reference | VARCHAR(150) | yes | — | UQ |
| row_hash | VARCHAR(64) | no | — | UQ |
| status | VARCHAR(30) | no | READY | UQ |
| entity_type | VARCHAR(50) | yes | — | — |
| new_entity_id | INTEGER | yes | — | — |
| data_json | TEXT | no | — | — |
| error_json | TEXT | yes | [] | — |
| imported_at | DATETIME | yes | — | — |

Indexes: ix_migration_row_run_id ['run_id'] unique=False; ix_migration_row_legacy_reference ['legacy_reference'] unique=False; ix_migration_row_status ['status'] unique=False; ix_migration_row_row_hash ['row_hash'] unique=False.

Relationships: run -> MigrationRun uselist=False.

### MigrationRun — table `migration_run`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| run_key | VARCHAR(36) | no | — | UQ, IX |
| template_type | VARCHAR(40) | no | — | UQ |
| filename | VARCHAR(255) | no | — | — |
| source_hash | VARCHAR(64) | no | — | UQ |
| status | VARCHAR(30) | no | VALIDATED | UQ |
| mode | VARCHAR(20) | no | VALIDATION_ONLY | — |
| summary_json | TEXT | yes | {} | — |
| uploaded_by | VARCHAR(100) | yes | — | — |
| created_at | DATETIME | no | pk_model_now | UQ |
| imported_at | DATETIME | yes | — | — |

Indexes: ix_migration_run_created_at ['created_at'] unique=False; ix_migration_run_run_key ['run_key'] unique=True; ix_migration_run_source_hash ['source_hash'] unique=False; ix_migration_run_template_type ['template_type'] unique=False; ix_migration_run_status ['status'] unique=False.

Relationships: rows -> MigrationRow uselist=True.

### Payment — table `payment`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_id | INTEGER | yes | — | UQ, FK → client.id |
| client_name | VARCHAR(100) | yes | — | — |
| amount | FLOAT | yes | 0 | — |
| amount_minor | BIGINT | yes | — | — |
| method | VARCHAR(50) | yes | — | — |
| payment_type | VARCHAR(30) | yes | Receipt | UQ |
| source_type | VARCHAR(50) | yes | — | UQ |
| source_id | INTEGER | yes | — | UQ |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| photo_path | VARCHAR(200) | yes | — | — |
| photo_url | VARCHAR(500) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | UQ |
| note | VARCHAR(500) | yes | — | — |
| discount | FLOAT | yes | 0 | — |
| discount_minor | BIGINT | yes | — | — |
| discount_reason | VARCHAR(200) | yes | — | — |
| bank_name | VARCHAR(100) | yes | — | — |
| account_name | VARCHAR(100) | yes | — | — |
| account_no | VARCHAR(50) | yes | — | — |
| payment_account_id | INTEGER | yes | — | FK → account.id |
| idempotency_key | VARCHAR(64) | yes | — | UQ, IX |
| idempotency_payload_hash | VARCHAR(64) | yes | — | UQ |
| revision | INTEGER | yes | 1 | — |
| created_by | VARCHAR(80) | yes | — | — |
| updated_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | UQ |

Indexes: ix_payment_is_void ['is_void'] unique=False; ix_payment_idempotency_key ['idempotency_key'] unique=True; ix_payment_updated_at ['updated_at'] unique=False; ix_payment_created_at ['created_at'] unique=False; ix_payment_date_posted ['date_posted'] unique=False; ix_payment_client_id ['client_id'] unique=False; ix_payment_payment_type ['payment_type'] unique=False; ix_payment_idempotency_payload_hash ['idempotency_payload_hash'] unique=False; ix_payment_source_type ['source_type'] unique=False; ix_payment_source_id ['source_id'] unique=False.

Relationships: client -> Client uselist=False; payment_account -> Account uselist=False; waive_off_rows -> WaiveOff uselist=True.

### PendingBill — table `pending_bill`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| client_code | VARCHAR(50) | yes | — | — |
| client_name | VARCHAR(100) | yes | — | — |
| bill_no | VARCHAR(50) | yes | — | — |
| bill_kind | VARCHAR(10) | yes | UNKNOWN | UQ |
| nimbus_no | VARCHAR(50) | yes | — | — |
| amount | FLOAT | yes | 0 | — |
| reason | VARCHAR(200) | yes | — | — |
| photo_url | VARCHAR(200) | yes | — | — |
| photo_path | VARCHAR(200) | yes | — | — |
| is_paid | BOOLEAN | yes | False | — |
| is_cash | BOOLEAN | yes | False | — |
| is_manual | BOOLEAN | yes | False | — |
| created_at | VARCHAR(50) | yes | — | — |
| created_by | VARCHAR(80) | yes | — | — |
| is_void | BOOLEAN | yes | False | — |
| note | VARCHAR(500) | yes | — | — |
| risk_override | VARCHAR(20) | yes | — | — |
| source_module | VARCHAR(50) | yes | — | UQ |
| source_table | VARCHAR(50) | yes | — | UQ |
| source_id | INTEGER | yes | — | UQ |
| source_bill_no | VARCHAR(50) | yes | — | UQ |
| transaction_type | VARCHAR(50) | yes | — | UQ |

Indexes: ix_pending_bill_bill_kind ['bill_kind'] unique=False; ix_pending_bill_source_table ['source_table'] unique=False; ix_pending_bill_transaction_type ['transaction_type'] unique=False; ix_pending_bill_source_module ['source_module'] unique=False; ix_pending_bill_source_id ['source_id'] unique=False; ix_pending_bill_source_bill_no ['source_bill_no'] unique=False.

Relationships: reminders -> FollowUpReminder uselist=True; contact_logs -> FollowUpContact uselist=True.

### ReconBasket — table `recon_basket`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| bill_no | VARCHAR(50) | yes | — | — |
| inv_date | DATE | yes | — | — |
| inv_client | VARCHAR(100) | yes | — | — |
| fin_client | VARCHAR(100) | yes | — | — |
| inv_material | VARCHAR(100) | yes | — | — |
| inv_qty | FLOAT | yes | 0 | — |
| status | VARCHAR(20) | yes | — | — |
| match_score | INTEGER | yes | 0 | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_recon_basket_created_at ['created_at'] unique=False.

### RootBackupEmailHistory — table `root_backup_email_history`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| trigger_type | VARCHAR(30) | no | auto | UQ |
| status | VARCHAR(20) | no | failed | UQ |
| recipient_emails | VARCHAR(1000) | yes | — | — |
| subject | VARCHAR(300) | yes | — | — |
| attachment_name | VARCHAR(255) | yes | — | — |
| attachment_size_kb | INTEGER | yes | — | — |
| backup_path | VARCHAR(1000) | yes | — | — |
| message | VARCHAR(1000) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_root_backup_email_history_trigger_type ['trigger_type'] unique=False; ix_root_backup_email_history_status ['status'] unique=False; ix_root_backup_email_history_created_at ['created_at'] unique=False.

### RootBackupSettings — table `root_backup_settings`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| enabled | BOOLEAN | no | False | — |
| frequency | VARCHAR(20) | no | hourly | — |
| recipient_emails | VARCHAR(1000) | yes | — | — |
| include_full_raw_xlsx | BOOLEAN | no | True | — |
| include_sqlite_db | BOOLEAN | no | True | — |
| subject_prefix | VARCHAR(120) | no | PWARE Root Backup | — |
| keep_history_count | INTEGER | no | 200 | — |
| last_sent_at | DATETIME | yes | — | — |
| last_status | VARCHAR(20) | yes | — | — |
| last_message | VARCHAR(500) | yes | — | — |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | UQ |

Indexes: ix_root_backup_settings_updated_at ['updated_at'] unique=False.

### RootRecoveryCode — table `root_recovery_code`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| username | VARCHAR(80) | no | root | UQ |
| code_hash | VARCHAR(255) | no | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| used_at | DATETIME | yes | — | UQ |
| generated_by | VARCHAR(80) | yes | — | — |
| note | VARCHAR(300) | yes | — | — |

Indexes: ix_root_recovery_code_username ['username'] unique=False; ix_root_recovery_code_created_at ['created_at'] unique=False; ix_root_recovery_code_used_at ['used_at'] unique=False.

### SaleDeliveryPerson — table `sale_delivery_persons`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| sale_id | INTEGER | no | — | UQ, FK → direct_sale.id |
| delivery_person_id | INTEGER | no | — | UQ, FK → delivery_person.id |
| bags_delivered | FLOAT | yes | 0 | — |
| rent_amount | FLOAT | yes | 0 | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_sale_delivery_persons_created_at ['created_at'] unique=False; ix_sale_delivery_persons_delivery_person_id ['delivery_person_id'] unique=False; ix_sale_delivery_persons_sale_id ['sale_id'] unique=False; ix_sale_delivery_persons_is_void ['is_void'] unique=False.

Relationships: sale -> DirectSale uselist=False; delivery_person -> DeliveryPerson uselist=False.

### SchemaVersion — table `schema_version`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| version | INTEGER | yes | 1 | — |
| applied_at | DATETIME | yes | pk_model_now | — |

### Service — table `service`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| code | VARCHAR(50) | no | — | — |
| name | VARCHAR(100) | no | — | — |
| unit | VARCHAR(20) | yes | Pcs | — |
| unit_price | FLOAT | yes | 0 | — |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| converted_from_material_id | INTEGER | yes | — | — |

Indexes: ix_service_created_at ['created_at'] unique=False.

### Settings — table `settings`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| currency | VARCHAR(10) | yes | PKR | — |
| company_name | VARCHAR(100) | yes | [seeded company name] | — |
| company_address | VARCHAR(200) | yes | [seeded company address] | — |
| company_phone | VARCHAR(50) | yes | [seeded company phone] | — |
| company_email | VARCHAR(100) | yes | — | — |
| tax_rate | FLOAT | yes | 0 | — |
| invoice_prefix | VARCHAR(10) | yes | INV- | — |
| bill_prefix | VARCHAR(10) | yes | # | — |
| ui_theme | VARCHAR(20) | yes | dark_navy | — |
| allow_global_negative_stock | BOOLEAN | no | False | — |
| google_client_id | VARCHAR(500) | yes | — | — |
| google_client_secret | VARCHAR(500) | yes | — | — |
| google_refresh_token | VARCHAR(1000) | yes | — | — |
| google_access_token | VARCHAR(1000) | yes | — | — |
| google_token_expiry | VARCHAR(50) | yes | — | — |
| google_sender_email | VARCHAR(200) | yes | — | — |

### StaffEmail — table `staff_email`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| email | VARCHAR(200) | no | — | IX |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_staff_email_created_at ['created_at'] unique=False.

### Supplier — table `supplier`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | — |
| transferred_to_id | INTEGER | yes | — | FK → supplier.id |
| phone | VARCHAR(20) | yes | — | — |
| address | VARCHAR(200) | yes | — | — |
| opening_balance | FLOAT | yes | 0 | — |
| opening_balance_date | DATETIME | yes | pk_model_now | UQ |
| is_active | BOOLEAN | yes | True | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_supplier_created_at ['created_at'] unique=False; ix_supplier_opening_balance_date ['opening_balance_date'] unique=False.

Relationships: payments -> SupplierPayment uselist=True; grns -> GRN uselist=True.

### SupplierPayment — table `supplier_payment`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| supplier_id | INTEGER | no | — | UQ, FK → supplier.id |
| amount | FLOAT | yes | 0 | — |
| amount_minor | BIGINT | yes | — | — |
| method | VARCHAR(50) | yes | — | — |
| payment_type | VARCHAR(30) | yes | Payment | UQ |
| source_type | VARCHAR(50) | yes | — | UQ |
| source_id | INTEGER | yes | — | UQ |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| note | VARCHAR(500) | yes | — | — |
| is_void | BOOLEAN | yes | False | UQ |
| bank_name | VARCHAR(100) | yes | — | — |
| account_name | VARCHAR(100) | yes | — | — |
| account_no | VARCHAR(50) | yes | — | — |
| payment_account_id | INTEGER | yes | — | FK → account.id |
| manual_bill_no | VARCHAR(50) | yes | — | — |
| auto_bill_no | VARCHAR(50) | yes | — | — |
| idempotency_key | VARCHAR(64) | yes | — | UQ, IX |
| idempotency_payload_hash | VARCHAR(64) | yes | — | UQ |
| revision | INTEGER | yes | 1 | — |
| created_by | VARCHAR(80) | yes | — | — |
| updated_by | VARCHAR(80) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| updated_at | DATETIME | yes | pk_model_now (on update pk_model_now) | UQ |

Indexes: ix_supplier_payment_supplier_id ['supplier_id'] unique=False; ix_supplier_payment_source_type ['source_type'] unique=False; ix_supplier_payment_idempotency_payload_hash ['idempotency_payload_hash'] unique=False; ix_supplier_payment_date_posted ['date_posted'] unique=False; ix_supplier_payment_source_id ['source_id'] unique=False; ix_supplier_payment_updated_at ['updated_at'] unique=False; ix_supplier_payment_idempotency_key ['idempotency_key'] unique=True; ix_supplier_payment_is_void ['is_void'] unique=False; ix_supplier_payment_payment_type ['payment_type'] unique=False; ix_supplier_payment_created_at ['created_at'] unique=False.

Relationships: supplier -> Supplier uselist=False; payment_account -> Account uselist=False.

### SystemLock — table `system_lock`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| name | VARCHAR(100) | no | — | UQ, IX |
| status | VARCHAR(20) | no | unlocked | UQ |
| owner | VARCHAR(100) | yes | — | — |
| acquired_at | DATETIME | yes | — | UQ |
| ttl_seconds | INTEGER | yes | 3600 | — |
| note | VARCHAR(500) | yes | — | — |

Indexes: ix_system_lock_acquired_at ['acquired_at'] unique=False; ix_system_lock_status ['status'] unique=False; ix_system_lock_name ['name'] unique=True.

### TenantWipeBackupHistory — table `tenant_wipe_backup_history`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| tenant_name | VARCHAR(120) | yes | — | — |
| performed_by | VARCHAR(80) | yes | — | — |
| targets | VARCHAR(1000) | yes | — | — |
| backup_filename | VARCHAR(255) | yes | — | — |
| backup_path | VARCHAR(1000) | yes | — | — |
| wipe_status | VARCHAR(20) | yes | pending | UQ |
| note | VARCHAR(500) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |

Indexes: ix_tenant_wipe_backup_history_created_at ['created_at'] unique=False; ix_tenant_wipe_backup_history_wipe_status ['wipe_status'] unique=False.

### User — table `user`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| username | VARCHAR(80) | no | — | — |
| password_hash | VARCHAR(200) | yes | — | — |
| password_plain | VARCHAR(200) | yes | — | — |
| role | VARCHAR(20) | yes | user | — |
| status | VARCHAR(20) | yes | active | — |
| can_view_stock | BOOLEAN | yes | True | — |
| can_view_daily | BOOLEAN | yes | True | — |
| can_view_history | BOOLEAN | yes | True | — |
| can_import_export | BOOLEAN | yes | False | — |
| can_manage_directory | BOOLEAN | yes | False | — |
| can_view_dashboard | BOOLEAN | yes | True | — |
| can_manage_grn | BOOLEAN | yes | True | — |
| can_manage_bookings | BOOLEAN | yes | True | — |
| can_manage_payments | BOOLEAN | yes | True | — |
| can_manage_sales | BOOLEAN | yes | True | — |
| can_view_delivery_rent | BOOLEAN | yes | True | — |
| can_manage_pending_bills | BOOLEAN | yes | True | — |
| can_view_reports | BOOLEAN | yes | True | — |
| can_manage_notifications | BOOLEAN | yes | True | — |
| can_view_client_ledger | BOOLEAN | yes | True | — |
| can_view_supplier_ledger | BOOLEAN | yes | True | — |
| can_view_decision_ledger | BOOLEAN | yes | True | — |
| can_manage_clients | BOOLEAN | yes | False | — |
| can_manage_suppliers | BOOLEAN | yes | False | — |
| can_manage_materials | BOOLEAN | yes | False | — |
| can_manage_delivery_persons | BOOLEAN | yes | False | — |
| can_access_settings | BOOLEAN | yes | False | — |
| can_manage_accounts | BOOLEAN | yes | False | — |
| can_view_cash_flow | BOOLEAN | yes | False | — |
| restrict_backdated_edit | BOOLEAN | yes | False | — |
| access_mode | VARCHAR(20) | yes | read_write | — |
| created_at | DATETIME | yes | pk_model_now | — |

### UserLoginSession — table `user_login_session`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| sid | VARCHAR(40) | no | — | UQ, IX |
| user_id | INTEGER | no | — | UQ |
| username | VARCHAR(80) | yes | — | UQ |
| role | VARCHAR(20) | yes | — | — |
| ip | VARCHAR(80) | yes | — | UQ |
| user_agent | VARCHAR(300) | yes | — | — |
| created_at | DATETIME | yes | pk_model_now | UQ |
| last_seen_at | DATETIME | yes | pk_model_now | UQ |
| ended_at | DATETIME | yes | — | UQ |

Indexes: ix_user_login_session_last_seen_at ['last_seen_at'] unique=False; ix_user_login_session_ended_at ['ended_at'] unique=False; ix_user_login_session_created_at ['created_at'] unique=False; ix_user_login_session_ip ['ip'] unique=False; ix_user_login_session_user_id ['user_id'] unique=False; ix_user_login_session_sid ['sid'] unique=True; ix_user_login_session_username ['username'] unique=False.

### WaiveOff — table `waive_off`

| Column | Type | Null | Default | Flags |
|---|---|---|---|---|
| id | INTEGER | no | — | PK |
| payment_id | INTEGER | yes | — | UQ, FK → payment.id |
| client_code | VARCHAR(50) | yes | — | UQ |
| client_name | VARCHAR(100) | yes | — | UQ |
| bill_no | VARCHAR(50) | yes | — | UQ |
| amount | FLOAT | yes | 0 | — |
| reason | VARCHAR(300) | yes | — | — |
| date_posted | DATETIME | yes | pk_model_now | UQ |
| created_by | VARCHAR(80) | yes | — | — |
| note | VARCHAR(500) | yes | — | — |
| is_void | BOOLEAN | yes | False | UQ |

Indexes: ix_waive_off_is_void ['is_void'] unique=False; ix_waive_off_bill_no ['bill_no'] unique=False; ix_waive_off_payment_id ['payment_id'] unique=False; ix_waive_off_date_posted ['date_posted'] unique=False; ix_waive_off_client_code ['client_code'] unique=False; ix_waive_off_client_name ['client_name'] unique=False.

Relationships: payment -> Payment uselist=False.

### migration_history — table `migration_history`

Schema bookkeeping only: records which numbered migrations have been applied and the current schema version. No ORM model and no business UI. Never wiped by business-data resets.


---

## Appendix B. Registered URL map and permission flags

314 active routes. “Permission” is the feature flag enforced by the endpoint permission map; “—” means the route relies on login plus its own role/admin guard (or is public/static). Methods are the registered HTTP verbs. “OR” means any one of the listed flags grants access.


### Blueprint: accounts

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /accounts/ | accounts.dashboard | — |
| GET | /accounts/<int:account_id>/data | accounts.account_data | — |
| POST | /accounts/<int:account_id>/delete | accounts.delete_account | — |
| GET,POST | /accounts/<int:account_id>/edit | accounts.edit_account | — |
| GET,POST | /accounts/<int:account_id>/reconcile | accounts.reconcile_account | — |
| POST | /accounts/<int:account_id>/toggle | accounts.toggle_account | — |
| GET | /accounts/accounts | accounts.manage_accounts | — |
| GET,POST | /accounts/accounts/add | accounts.add_account | — |
| POST | /accounts/api/categories/add | accounts.api_add_account_category | — |
| GET | /accounts/api/kpi/client_payments | accounts.api_client_payments_today | — |
| GET | /accounts/api/kpi/company_money | accounts.api_company_money | — |
| GET | /accounts/api/kpi/expenditures | accounts.api_expenditures_today | — |
| GET | /accounts/api/kpi/receipts | accounts.api_receipts_today | — |
| GET | /accounts/api/kpi/supplier_payments | accounts.api_supplier_payments_today | — |
| GET | /accounts/audit | accounts.audit_trail | — |
| GET,POST | /accounts/categories | accounts.manage_categories | — |
| POST | /accounts/categories/<int:category_id>/delete | accounts.delete_account_category_route | — |
| POST | /accounts/categories/<int:category_id>/edit | accounts.edit_account_category_route | — |
| POST | /accounts/categories/<int:category_id>/toggle | accounts.toggle_account_category_route | — |
| POST | /accounts/categories/add | accounts.add_account_category | — |
| GET | /accounts/expenditures | accounts.expenditures | — |
| GET | /accounts/kpi/bank_accounts | accounts.kpi_bank_accounts | — |
| GET | /accounts/kpi/cash_accounts | accounts.kpi_cash_accounts | — |
| GET | /accounts/kpi/cash_money | accounts.kpi_cash_money | — |
| GET | /accounts/kpi/client_payments | accounts.kpi_client_payments | — |
| GET | /accounts/kpi/company_money | accounts.kpi_company_money | — |
| GET | /accounts/kpi/expenditures | accounts.kpi_expenditures | — |
| GET | /accounts/kpi/receipts | accounts.kpi_receipts | — |
| GET | /accounts/kpi/supplier_payments | accounts.kpi_supplier_payments | — |
| GET | /accounts/ledger/<int:account_id> | accounts.account_ledger | — |
| GET | /accounts/payments/clients | accounts.client_payments | — |
| GET | /accounts/payments/clients/<int:id>/data | accounts.client_payment_data | — |
| POST | /accounts/payments/clients/restore/<int:id> | accounts.client_payment_restore | — |
| POST | /accounts/payments/clients/save | accounts.client_payment_save | — |
| POST | /accounts/payments/clients/void/<int:id> | accounts.client_payment_void | — |
| GET | /accounts/payments/suppliers | accounts.supplier_payments | — |
| GET | /accounts/payments/suppliers/<int:id>/data | accounts.supplier_payment_data | — |
| POST | /accounts/payments/suppliers/<int:id>/delete | accounts.supplier_payment_delete | — |
| POST | /accounts/payments/suppliers/<int:id>/restore | accounts.supplier_payment_restore | — |
| POST | /accounts/payments/suppliers/save | accounts.supplier_payment_save | — |
| GET | /accounts/receipts | accounts.receipts | — |
| GET | /accounts/reconciliations | accounts.reconciliations | — |
| POST | /accounts/transactions/<int:tx_id>/delete | accounts.delete_account_transaction | — |
| POST | /accounts/transactions/<int:tx_id>/edit | accounts.edit_account_transaction | — |
| POST | /accounts/transactions/<int:tx_id>/void | accounts.void_transaction | can_manage_sales |
| POST | /accounts/transactions/new | accounts.add_transaction | — |
| GET | /accounts/transfers | accounts.transfers | — |
| GET,POST | /accounts/transfers/add | accounts.add_transfer | — |

### Blueprint: admin

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /admin/ | admin.dashboard | — |
| GET | /admin/api/health | admin.api_health_check | — |
| GET | /admin/api/modules | admin.api_get_modules | — |
| GET | /admin/modules | admin.modules_list | — |

### Blueprint: api

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /api/audit/financial-integrity | api.api_financial_integrity_audit | can_access_settings |
| GET | /api/check_bill/<path:bill_no> | api.check_bill_api | — |
| GET | /api/client_booking_status/<client_code> | api.api_client_booking_status | — |
| GET | /api/client_financial_summary/<client_code> | api.api_client_financial_summary | can_view_client_ledger |
| GET | /api/client_next_code | api.api_client_next_code | — |
| GET | /api/clients/search | api.api_clients_search | — |
| GET | /api/grn_consumption | api.api_grn_consumption | — |
| GET | /api/last_sold_price | api.api_last_sold_price | — |
| GET | /api/ledger_revision | api.api_ledger_revision | — |
| GET | /api/material_next_code | api.api_material_next_code | can_manage_materials |
| GET | /api/notifications/contact_history/<int:bill_id> | api.api_notifications_contact_history | can_manage_notifications |
| GET | /api/notifications/due | api.api_notifications_due | can_manage_notifications |
| GET | /api/supplier_balance/<int:id> | api.api_supplier_balance | can_view_supplier_ledger |
| GET | /api/suppliers/search | api.api_suppliers_search | — |
| GET,POST | /api/ui/theme | api.ui_theme_preference_api | — |

### Blueprint: auth

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET,POST | /login | auth.login | — |
| GET | /logout | auth.logout | — |
| GET | /root/backup-settings | auth.root_backup_settings | — |
| POST | /root/backup-settings/history/clear | auth.root_backup_settings_history_clear | — |
| GET | /root/backup-settings/history/download/<int:history_id> | auth.root_backup_settings_history_download | — |
| POST | /root/backup-settings/save | auth.root_backup_settings_save | — |
| POST | /root/backup-settings/send-now | auth.root_backup_settings_send_now | — |
| GET,POST | /root/recovery | auth.root_recovery | — |
| GET,POST | /root/recovery_codes | auth.root_recovery_codes | — |

### Blueprint: core

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | / | core.index | can_view_dashboard |

### Blueprint: data_lab

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET,POST | /data_lab/ | data_lab.upload | — |
| GET | /data_lab/basket | data_lab.view_basket | — |
| POST | /data_lab/correct_bill | data_lab.correct_bill | — |
| POST | /data_lab/legacy_import | data_lab.legacy_import | — |

### Blueprint: import_export

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /import_export/ | import_export.import_export_page | — |
| GET,POST | /import_export/app_upgrade | import_export.app_upgrade | — |
| POST | /import_export/app_upgrade/migrate | import_export.app_upgrade_migrate | — |
| POST | /import_export/app_upgrade/rollback | import_export.app_upgrade_rollback | — |
| POST | /import_export/app_upgrade/start | import_export.app_upgrade_start | — |
| GET | /import_export/app_upgrade/status/<job_id> | import_export.app_upgrade_status | — |
| POST | /import_export/email_file | import_export.email_file | — |
| POST | /import_export/execute_import | import_export.execute_import | — |
| GET | /import_export/export | import_export.export_data | — |
| GET | /import_export/export_excel_all | import_export.export_excel_all | — |
| GET | /import_export/full_db_export | import_export.full_db_export | — |
| POST | /import_export/full_db_import | import_export.full_db_import | — |
| GET | /import_export/full_db_import_report/<report_name> | import_export.full_db_import_report | — |
| GET | /import_export/full_raw_export | import_export.full_raw_export | — |
| POST | /import_export/full_raw_import | import_export.full_raw_import | — |
| GET,POST | /import_export/full_raw_import_history | import_export.full_raw_import_history | can_import_export |
| GET | /import_export/full_raw_import_history_export | import_export.full_raw_import_history_export | — |
| GET | /import_export/full_raw_import_report/<report_name> | import_export.full_raw_import_report | can_import_export |
| GET | /import_export/master/export | import_export.export_master | — |
| POST | /import_export/master/import | import_export.import_master | — |
| POST | /import_export/master/import/start | import_export.master_import_start | — |
| GET | /import_export/master/import/status/<job_id> | import_export.master_import_status | — |
| POST | /import_export/preview | import_export.preview_import | — |
| GET | /import_export/template/<dataset> | import_export.get_template | — |
| GET | /import_export/tenant_db_export | import_export.tenant_db_export | can_import_export |
| POST | /import_export/tenant_db_restore | import_export.tenant_db_restore | can_import_export |
| POST | /import_export/transfer/export | import_export.transfer_export | can_import_export |
| POST | /import_export/transfer/import | import_export.transfer_import | can_import_export |

### Blueprint: inventory

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /inventory/daily_transactions | inventory.daily_transactions | — |
| GET | /inventory/inventory_log | inventory.inventory_log | — |
| GET | /inventory/stock_summary | inventory.stock_summary | — |

### Blueprint: ledgers

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /api/delivery_person_ledger/<int:id> | ledgers.delivery_person_ledger_api | can_view_delivery_rent OR can_view_client_ledger |
| GET | /api/delivery_persons/search | ledgers.delivery_person_search_api | can_view_delivery_rent OR can_view_client_ledger |
| POST | /client_booking_cancel/<int:client_id> | ledgers.client_booking_cancel | can_manage_bookings |
| POST | /client_booking_cancel_revert/<int:client_id>/<int:entry_id> | ledgers.client_booking_cancel_revert | can_manage_bookings |
| GET | /client_ledger/<int:id> | ledgers.client_ledger | can_view_client_ledger |
| POST | /client_toggle_active/<int:client_id> | ledgers.client_toggle_active | can_manage_clients |
| GET | /decision_ledger | ledgers.decision_ledger | can_view_decision_ledger |
| GET | /delivery_ledger/<int:id> | ledgers.delivery_person_ledger | can_view_delivery_rent OR can_view_client_ledger |
| GET | /delivery_person_ledger/<int:id> | ledgers.delivery_person_ledger | can_view_delivery_rent OR can_view_client_ledger |
| POST | /delivery_person_ledger/<int:id>/pay | ledgers.settle_delivery_person | can_manage_sales |
| POST | /delivery_person_opening_balance/<int:id> | ledgers.delivery_person_opening_balance | can_manage_delivery_persons |
| POST | /delivery_person_payments/<int:id>/edit | ledgers.edit_delivery_person_payment | can_manage_sales |
| POST | /delivery_person_payments/<int:id>/restore | ledgers.restore_delivery_person_payment | can_manage_sales |
| POST | /delivery_person_payments/<int:id>/void | ledgers.void_delivery_person_payment | can_manage_sales |
| GET | /download_client_clearance/<int:id> | ledgers.download_client_clearance | can_view_client_ledger |
| GET | /download_client_ledger/<int:id> | ledgers.download_client_ledger | can_view_client_ledger |
| GET | /download_delivery_person_ledger/<int:id> | ledgers.download_delivery_person_ledger | can_view_delivery_rent OR can_view_client_ledger |
| GET | /download_full_client_history/<int:id> | ledgers.download_full_client_history | can_view_client_ledger |
| GET | /financial_ledger/<int:client_id> | ledgers.financial_ledger_details | can_view_client_ledger |
| GET | /ledger | ledgers.ledger_page | can_view_client_ledger |
| GET | /ledger/<int:client_id> | ledgers.financial_ledger | can_view_client_ledger |
| GET | /material_ledger/<int:mat_id> | ledgers.material_ledger_page | can_view_history |

### Blueprint: legacy_migration

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /legacy-migration/ | legacy_migration.dashboard | — |
| GET | /legacy-migration/run/<int:run_id> | legacy_migration.preview | — |
| POST | /legacy-migration/run/<int:run_id>/dry-run | legacy_migration.dry_run | — |
| GET | /legacy-migration/run/<int:run_id>/errors.xlsx | legacy_migration.errors | — |
| POST | /legacy-migration/run/<int:run_id>/import | legacy_migration.do_import | — |
| GET | /legacy-migration/template/<kind> | legacy_migration.download | — |
| POST | /legacy-migration/upload | legacy_migration.upload | — |

### Blueprint: masters

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| POST | /add_client | masters.add_client | can_manage_clients |
| POST | /add_supplier | masters.add_supplier | can_manage_suppliers |
| GET | /api/supplier_ledger_shift_preview/<int:id> | masters.api_supplier_ledger_shift_preview | can_manage_suppliers |
| GET | /client-ledger/categories | masters.manage_client_categories | can_manage_clients |
| POST | /client-ledger/categories | masters.add_client_category | can_manage_clients |
| POST | /client-ledger/categories/<int:category_id>/delete | masters.delete_client_category | can_manage_clients |
| POST | /client-ledger/categories/<int:category_id>/rename | masters.rename_client_category | can_manage_clients |
| POST | /client-ledger/categories/<int:category_id>/toggle | masters.toggle_client_category | can_manage_clients |
| POST | /client-ledger/categories/add | masters.add_client_category | can_manage_clients |
| POST | /client_opening_balance/<int:id> | masters.client_opening_balance | can_manage_clients |
| GET | /clients | masters.clients | can_view_client_ledger |
| GET | /clients/<int:client_id>/modals | masters.client_modals | can_manage_clients |
| POST | /clients/activate_all | masters.activate_all_clients | can_manage_clients |
| POST | /delete_client/<int:id> | masters.delete_client | can_manage_clients |
| POST | /delete_supplier/<int:id> | masters.delete_supplier | can_manage_suppliers |
| GET | /delivery_persons | masters.delivery_persons_page | can_manage_delivery_persons |
| POST | /delivery_persons/add | masters.add_delivery_person | can_manage_delivery_persons |
| POST | /delivery_persons/edit/<int:id> | masters.edit_delivery_person | can_manage_delivery_persons |
| POST | /delivery_persons/toggle/<int:id> | masters.toggle_delivery_person | can_manage_delivery_persons |
| POST | /edit_client/<int:id> | masters.edit_client | can_manage_clients |
| POST | /edit_supplier/<int:id> | masters.edit_supplier | can_manage_suppliers |
| POST | /hard_delete_supplier/<int:id> | masters.hard_delete_supplier | can_manage_suppliers |
| GET | /materials | masters.materials | can_manage_materials |
| POST | /materials/activate_all | masters.activate_all_materials | can_manage_materials |
| POST | /materials/rename_label | masters.rename_material_label | can_manage_materials |
| POST | /reclaim_client/<int:id> | masters.reclaim_client | can_manage_clients |
| POST | /reclaim_supplier_ledger/<int:id> | masters.reclaim_supplier_ledger | can_manage_suppliers |
| GET | /services | masters.services | can_manage_materials |
| POST | /services/add | masters.add_service | can_manage_materials |
| POST | /services/delete/<int:id> | masters.delete_service | can_manage_materials |
| POST | /services/edit/<int:id> | masters.edit_service | can_manage_materials |
| POST | /services/merge | masters.merge_services | can_manage_materials |
| POST | /services/migrate | masters.migrate_rent_to_services | can_manage_materials |
| POST | /services/toggle/<int:id> | masters.toggle_service | can_manage_materials |
| POST | /shift_grn_supplier/<int:id> | masters.shift_grn_supplier | can_manage_suppliers |
| POST | /shift_supplier_ledger/<int:id> | masters.shift_supplier_ledger | can_manage_suppliers |
| POST | /shift_supplier_payment/<int:id> | masters.shift_supplier_payment | can_manage_suppliers |
| GET | /supplier_ledger_shift/<int:id> | masters.supplier_ledger_shift_page | can_manage_suppliers |
| POST | /supplier_opening_balance/<int:id> | masters.supplier_opening_balance | can_manage_suppliers |
| GET | /suppliers | masters.suppliers | can_view_supplier_ledger |
| POST | /transfer_client/<int:id> | masters.transfer_client | can_manage_clients |
| POST | /transfer_supplier/<int:id> | masters.transfer_supplier | can_manage_suppliers |

### Blueprint: misc

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| POST | /add_material | misc.add_material | can_manage_materials |
| POST | /add_pending_bill | misc.add_pending_bill | can_manage_pending_bills |
| POST | /add_supplier_payment | misc.add_supplier_payment | can_manage_suppliers |
| POST | /add_user | misc.add_user | — |
| POST | /admin/accounts_domain_wipe | misc.admin_accounts_domain_wipe | — |
| POST | /admin/rebuild_erp_consistency | misc.admin_rebuild_erp_consistency | — |
| POST | /bulk_update_material_unit | misc.bulk_update_material_unit | can_manage_materials |
| POST | /change_password | misc.change_password | can_access_settings |
| POST | /data_wipe_preview | misc.data_wipe_preview | — |
| POST | /delete_all_data | misc.delete_all_data | — |
| POST | /delete_material/<int:id> | misc.delete_material | can_manage_materials |
| POST | /delete_pending_bill/<int:id> | misc.delete_pending_bill | can_manage_pending_bills |
| POST | /delete_selected_data | misc.delete_selected_data | — |
| POST | /delete_supplier_payment/<int:id> | misc.delete_supplier_payment | can_manage_suppliers |
| POST | /delete_user/<int:id> | misc.delete_user | — |
| GET | /download_supplier_ledger/<int:id> | misc.download_supplier_ledger | can_view_supplier_ledger |
| GET | /download_supplier_payment/<int:payment_id> | misc.download_supplier_payment | can_view_supplier_ledger |
| GET,POST | /edit_grn/<int:id> | misc.edit_grn | can_manage_grn |
| POST | /edit_material/<int:id> | misc.edit_material | can_manage_materials |
| POST | /edit_pending_bill/<int:id> | misc.edit_pending_bill | can_manage_pending_bills |
| POST | /edit_supplier_payment/<int:id> | misc.edit_supplier_payment | can_manage_suppliers |
| POST | /edit_user_permissions/<int:id> | misc.edit_user_permissions | — |
| GET | /export_clients | misc.export_clients | — |
| GET | /export_grn | misc.export_grn | can_manage_grn |
| GET | /export_pending_bills | misc.export_pending_bills | can_manage_pending_bills |
| GET | /export_unpaid_transactions | misc.export_unpaid_transactions | can_view_reports |
| GET | /fix_system_issues | misc.fix_system_issues | — |
| GET | /generate_dummy_data | misc.generate_dummy_data | — |
| GET,POST | /import_pending_bills | misc.import_pending_bills | can_manage_pending_bills |
| POST | /material_categories/<int:id>/rename | misc.rename_material_category | can_manage_materials |
| POST | /material_categories/<int:id>/toggle | misc.toggle_material_category | can_manage_materials |
| POST | /material_categories/add | misc.add_material_category | can_manage_materials |
| POST | /merge_materials | misc.merge_materials | can_manage_materials |
| GET | /pay_supplier | misc.pay_supplier_page | can_manage_suppliers |
| POST | /reconcile_data | misc.reconcile_data | — |
| POST | /restore_supplier_payment/<int:id> | misc.restore_supplier_payment | can_manage_suppliers |
| GET | /supplier_ledger/<int:id> | misc.supplier_ledger | can_view_supplier_ledger |
| GET | /system_report | misc.system_report | — |
| POST | /toggle_bill_paid/<int:id> | misc.toggle_bill_paid | can_manage_pending_bills |
| POST | /toggle_user_status/<int:id> | misc.toggle_user_status | — |
| POST | /update_settings | misc.update_settings | — |
| GET | /uploads/<path:filename> | misc.uploaded_file | — |

### Blueprint: ops

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| POST | /add_record | ops.add_record | can_view_daily |
| POST | /delete_entry/<int:id> | ops.delete_entry | can_view_daily |
| GET | /delivery_rents | ops.delivery_rents_page | can_view_delivery_rent |
| POST | /delivery_rents/pay/<int:alloc_id> | ops.pay_delivery_rent | — |
| POST | /delivery_rents/void/<int:id> | ops.void_delivery_rent | can_manage_sales |
| GET | /dispatching | ops.dispatching | can_view_daily |
| POST | /edit_entry/<int:id> | ops.edit_entry | can_view_daily |
| GET,POST | /grn | ops.grn | can_manage_grn |
| POST | /import_dispatch_data | ops.import_dispatch_data | can_view_daily |
| GET | /tracking | ops.tracking | can_view_history |

### Blueprint: reports

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /api/current_payables | reports.current_payables_api | can_view_cash_flow |
| GET | /api/current_payables/<int:client_id> | reports.current_payable_detail_api | can_view_cash_flow |
| GET,POST | /cash_flow | reports.cash_flow | can_view_cash_flow |
| GET | /cash_flow/entry/<int:entry_id>.json | reports.cash_flow_entry_json | can_view_cash_flow |
| GET,POST | /cash_flow/meta/categories | reports.cash_flow_categories_meta | can_view_cash_flow |
| GET,POST | /cash_flow/meta/parties | reports.cash_flow_parties_meta | can_view_cash_flow |
| GET,POST | /cash_flow/meta/subcategories | reports.cash_flow_subcategories_meta | can_view_cash_flow |
| GET | /cash_flow_differences | reports.cash_flow_differences | can_view_cash_flow |
| GET | /cash_flow_differences/<int:rec_id> | reports.cash_flow_reconciliation_detail | can_view_cash_flow |
| GET | /current_payables | reports.unpaid_transactions_page | can_view_reports |
| GET,POST | /daily_reconciliation | reports.daily_reconciliation | can_view_cash_flow |
| GET | /daily_transactions | reports.daily_transactions_redirect | can_view_daily |
| GET | /export_current_payables | reports.export_current_payables | can_view_reports |
| GET | /financial_details | reports.financial_details | can_view_reports |
| GET | /financial_tracker | reports.financial_tracker | can_view_cash_flow |
| GET | /profit_reports | reports.profit_reports | can_view_reports |
| GET | /profit_reports/export/excel | reports.profit_report_excel | — |
| GET | /profit_reports/export/pdf | reports.profit_report_pdf | — |
| GET | /profit_reports/transaction/<string:document_type>/<int:document_id> | reports.profit_transaction_detail | — |
| GET | /stock_summary | reports.stock_summary_redirect | can_view_stock |
| GET | /unpaid_transactions | reports.unpaid_transactions_page | can_view_reports |

### Blueprint: sales

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| POST | /add_booking | sales.add_booking | can_manage_bookings |
| POST | /add_direct_sale | sales.add_direct_sale | can_manage_sales |
| POST | /add_material_return | sales.add_material_return | can_manage_sales |
| POST | /add_payment | sales.add_payment | can_manage_payments |
| POST | /add_sale | sales.add_sale | can_manage_sales |
| GET | /bookings | sales.bookings_page | can_manage_bookings |
| GET | /bookings/<int:booking_id>/edit-modal | sales.booking_edit_modal | can_manage_bookings |
| POST | /delete_bill/<string:type>/<int:id> | sales.delete_bill | can_manage_sales |
| POST | /delete_transaction/<string:type>/<int:id> | sales.delete_transaction | — |
| GET | /direct_sales | sales.direct_sales_page | can_manage_sales |
| GET | /direct_sales/<int:sale_id>/edit-modal | sales.direct_sale_edit_modal | can_manage_sales |
| POST | /direct_sales/hold | sales.hold_direct_sale | can_manage_sales |
| POST | /direct_sales/hold/<int:draft_id>/delete | sales.delete_direct_sale_draft | can_manage_sales |
| POST | /direct_sales/hold/<int:draft_id>/resume | sales.resume_direct_sale_draft | can_manage_sales |
| GET | /download_invoice/<path:bill_no> | sales.download_invoice | can_view_history |
| POST | /edit_bill/Booking/<int:id> | sales.edit_booking | can_manage_bookings |
| POST | /edit_bill/DirectSale/<int:id> | sales.edit_direct_sale | can_manage_sales |
| POST | /edit_bill/Payment/<int:id> | sales.edit_payment | can_manage_payments |
| POST | /edit_ledger_transaction/<string:trans_type>/<int:trans_id> | sales.edit_ledger_transaction | — |
| POST | /edit_material_return/<int:id> | sales.edit_material_return | — |
| GET | /material_returns | sales.material_returns_page | can_manage_sales |
| GET | /mixed_transactions | sales.mixed_transactions | can_view_reports |
| GET | /payments | sales.payments_page | can_manage_payments |
| GET | /pending_bills | sales.pending_bills | can_manage_pending_bills |
| GET | /pending_bills/<int:bill_id>/modals | sales.pending_bill_modals | can_manage_pending_bills |
| POST | /unvoid_transaction/<string:type>/<int:id> | sales.unvoid_transaction | can_manage_sales |
| GET | /view_bill/<path:bill_no> | sales.view_bill | can_view_history |
| GET | /view_bill_detail/<string:type>/<int:id> | sales.view_bill_detail | can_view_history |
| POST | /void_transaction/<string:type>/<int:id> | sales.void_transaction | can_manage_sales |

### Blueprint: system

| Methods | Path | Endpoint | Permission |
|---|---|---|---|
| GET | /debug/db | system.debug_db | — |
| GET | /notifications | system.notifications_page | can_manage_notifications |
| POST | /notifications/ack_reminder/<int:id> | system.notifications_ack_reminder | can_manage_notifications |
| POST | /notifications/add_email | system.notifications_add_email | can_manage_notifications |
| GET | /notifications/bill/<int:bill_id> | system.notifications_bill_detail | can_manage_notifications |
| POST | /notifications/close_reminder/<int:id> | system.notifications_close_reminder | can_manage_notifications |
| POST | /notifications/delete_email/<int:id> | system.notifications_delete_email | can_manage_notifications |
| POST | /notifications/log_contact/<int:bill_id> | system.notifications_log_contact | can_manage_notifications |
| POST | /notifications/mark_paid/<int:bill_id> | system.notifications_mark_paid | can_manage_notifications |
| POST | /notifications/resolve_due/<int:id> | system.notifications_resolve_due | can_manage_notifications |
| POST | /notifications/set_reminder/<int:bill_id> | system.notifications_set_reminder | can_manage_notifications |
| POST | /notifications/set_severity/<int:bill_id> | system.notifications_set_severity | can_manage_notifications |
| POST | /notifications/toggle_email/<int:id> | system.notifications_toggle_email | can_manage_notifications |
| GET | /notifications/upcoming | system.notifications_upcoming | can_manage_notifications |
| GET | /settings | system.settings | can_access_settings |
| GET | /settings/activity | system.activity_log_page | can_access_settings |
| GET | /settings/sessions | system.login_sessions_page | — |
| GET | /tenants | system.tenants_dashboard | — |
| GET | /tenants/<tenant_id>/backup_history | system.tenants_backup_history | — |
| POST | /tenants/<tenant_id>/delete | system.tenants_delete | — |
| POST | /tenants/<tenant_id>/reset_admin | system.tenants_reset_admin | — |
| POST | /tenants/<tenant_id>/reset_missing_passwords | system.tenants_reset_missing_passwords | — |
| POST | /tenants/<tenant_id>/status | system.tenants_update_status | — |
| POST | /tenants/<tenant_id>/update | system.tenants_update | — |
| GET | /tenants/backup_history/download/<int:history_id> | system.tenants_backup_history_download | — |
| POST | /tenants/backup_history/restore/<int:history_id> | system.tenants_backup_history_restore | — |
| POST | /tenants/create | system.tenants_create | — |
| GET | /void_audit | system.void_audit_page | can_access_settings |
| POST | /void_audit/restore/<string:entity>/<int:record_id> | system.restore_audit_record | can_access_settings |

### Permission flag registry

| Group | Flag | UI label | Default for new users |
|---|---|---|---|
| Operations | can_view_dashboard | Dashboard | yes |
| Operations | can_manage_grn | GRN (Receiving) | yes |
| Operations | can_view_stock | Stock Summary | yes |
| Operations | can_view_daily | Daily Breakdown | yes |
| Operations | can_view_history | History & Bills | yes |
| Sales & Billing | can_manage_bookings | Bookings | yes |
| Sales & Billing | can_manage_sales | Sales (Direct Sale / Returns / Void) | yes |
| Sales & Billing | can_manage_payments | Payments | yes |
| Sales & Billing | can_manage_pending_bills | Pending Bills | yes |
| Sales & Billing | can_view_delivery_rent | Delivery Rent + Driver Ledger | yes |
| Ledgers & Reports | can_view_client_ledger | Client Ledger / Clearance | yes |
| Ledgers & Reports | can_view_supplier_ledger | Supplier Ledger / Vouchers | yes |
| Ledgers & Reports | can_view_decision_ledger | Decision Ledger | yes |
| Ledgers & Reports | can_view_reports | Reports (Profit / Unpaid / Mixed) | yes |
| Ledgers & Reports | can_view_cash_flow | Cash Flow + Differences | no |
| Masters | can_manage_clients | Manage Clients (Add/Edit/Delete) | no |
| Masters | can_manage_suppliers | Manage Suppliers + Supplier Payments | no |
| Masters | can_manage_materials | Manage Materials + Categories | no |
| Masters | can_manage_delivery_persons | Manage Delivery Persons | no |
| Finance & System | can_manage_accounts | Financial Accounts (Khata module) | no |
| Finance & System | can_manage_notifications | Notifications / Reminders | yes |
| Finance & System | can_import_export | Import / Export Center | no |
| Finance & System | can_access_settings | Settings Access (page + activity log) | no |

`can_manage_directory` is a derived legacy flag (true when any of the four master-management flags is true); it is not shown as a separate checkbox.


### Legacy permission fallbacks

When a newer flag is absent on an old user row, the system falls back to the legacy flag below.

| Newer flag | Falls back to |
|---|---|
| can_manage_grn | can_view_stock |
| can_manage_bookings | can_view_history |
| can_manage_payments | can_view_history |
| can_manage_sales | can_view_history |
| can_view_delivery_rent | can_view_history |
| can_manage_pending_bills | can_view_history |
| can_view_reports | can_view_history |
| can_manage_notifications | can_view_history |
| can_view_client_ledger | can_view_history |
| can_view_supplier_ledger | can_view_history |
| can_view_decision_ledger | can_view_history |
| can_manage_clients | can_manage_directory |
| can_manage_suppliers | can_manage_directory |
| can_manage_materials | can_manage_directory |
| can_manage_delivery_persons | can_manage_directory |

### Endpoints by permission flag


**can_access_settings**: activity_log_page, api_financial_integrity_audit, change_password, restore_audit_record, settings, void_audit_page.

**can_import_export**: data_lab.correct_bill, data_lab.legacy_import, data_lab.upload, data_lab.view_basket, full_raw_import_history, full_raw_import_report, import_browse_history, import_cancel_job, import_delete_upload, import_export.email_file, import_export.execute_import, import_export.export_data, import_export.export_excel_all, import_export.export_master, import_export.full_raw_export, import_export.full_raw_import, import_export.get_template, import_export.import_export_page, import_export.import_master, import_export.master_import_start, import_export.master_import_status, import_export.preview_import, import_get_upload, import_job_history, import_job_progress, import_list_uploads, import_start_job, import_upload_file, tenant_db_export, tenant_db_restore, transfer_export, transfer_import.

**can_manage_bookings**: add_booking, booking_edit_modal, bookings_page, client_booking_cancel, client_booking_cancel_revert, edit_booking.

**can_manage_clients**: activate_all_clients, add_client, add_client_category, client_modals, client_opening_balance, client_toggle_active, delete_client, delete_client_category, edit_client, manage_client_categories, reclaim_client, rename_client_category, toggle_client_category, transfer_client.

**can_manage_delivery_persons**: add_delivery_person, delivery_person_opening_balance, delivery_persons_page, edit_delivery_person, toggle_delivery_person.

**can_manage_grn**: edit_grn, export_grn, grn.

**can_manage_materials**: activate_all_materials, add_material, add_material_category, add_service, api_material_next_code, bulk_update_material_unit, delete_material, delete_service, edit_material, edit_service, materials, merge_materials, merge_services, migrate_rent_to_services, rename_material_category, rename_material_label, services, toggle_material_category, toggle_service.

**can_manage_notifications**: api_notifications_contact_history, api_notifications_due, notifications_ack_reminder, notifications_add_email, notifications_bill_detail, notifications_close_reminder, notifications_delete_email, notifications_log_contact, notifications_mark_paid, notifications_page, notifications_resolve_due, notifications_set_reminder, notifications_set_severity, notifications_toggle_email, notifications_upcoming.

**can_manage_payments**: add_payment, edit_payment, payments_page.

**can_manage_pending_bills**: add_pending_bill, delete_pending_bill, edit_pending_bill, export_pending_bills, import_pending_bills, pending_bill_modals, pending_bills, toggle_bill_paid.

**can_manage_sales**: add_direct_sale, add_material_return, add_sale, delete_bill, delete_direct_sale_draft, direct_sale_edit_modal, direct_sales_page, edit_delivery_person_payment, edit_direct_sale, hold_direct_sale, material_returns_page, restore_delivery_person_payment, resume_direct_sale_draft, settle_delivery_person, unvoid_transaction, void_delivery_person_payment, void_delivery_rent, void_transaction.

**can_manage_suppliers**: add_supplier, add_supplier_payment, api_supplier_ledger_shift_preview, delete_supplier, delete_supplier_payment, edit_supplier, edit_supplier_payment, hard_delete_supplier, pay_supplier_page, reclaim_supplier_ledger, restore_supplier_payment, shift_grn_supplier, shift_supplier_ledger, shift_supplier_payment, supplier_ledger_shift_page, supplier_opening_balance, transfer_supplier.

**can_view_cash_flow**: cash_flow, cash_flow_categories_meta, cash_flow_differences, cash_flow_entry_json, cash_flow_parties_meta, cash_flow_reconciliation_detail, cash_flow_subcategories_meta, current_payable_detail_api, current_payables_api, daily_reconciliation, financial_tracker.

**can_view_client_ledger**: api_client_financial_summary, client_ledger, clients, download_client_clearance, download_client_ledger, download_full_client_history, financial_ledger, financial_ledger_details, ledger_page.

**can_view_daily**: add_record, daily_transactions_redirect, delete_entry, dispatching, edit_entry, import_dispatch_data, inventory.daily_transactions.

**can_view_dashboard**: index.

**can_view_decision_ledger**: decision_ledger.

**can_view_delivery_rent**: delivery_rents_page.

**can_view_delivery_rent OR can_view_client_ledger**: delivery_person_ledger, delivery_person_ledger_api, delivery_person_search_api, download_delivery_person_ledger.

**can_view_history**: download_invoice, inventory.inventory_log, material_ledger_page, tracking, view_bill, view_bill_detail.

**can_view_reports**: export_current_payables, export_unpaid_transactions, financial_details, mixed_transactions, profit_reports, unpaid_transactions_page.

**can_view_stock**: inventory.stock_summary, stock_summary_redirect.

**can_view_supplier_ledger**: api_supplier_balance, download_supplier_ledger, download_supplier_payment, supplier_ledger, suppliers.
