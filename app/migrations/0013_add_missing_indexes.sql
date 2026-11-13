-- 0013_add_missing_indexes.sql
--
-- Every foreign-key column that had no index.  A lookup such as "the lines of
-- this sale", "the GRNs of this supplier" or "the payments that hit this
-- account" was a full table scan, so the cost of opening a bill or a ledger
-- grew with the whole history instead of with the document.
--
-- Additive and idempotent: CREATE INDEX IF NOT EXISTS only, no data change, and
-- each one can be dropped again with DROP INDEX.  `app/services/schema.py`
-- (_PERFORMANCE_INDEXES) carries the same list for databases that boot before
-- the migration runner, so both entry points converge on the same schema.

CREATE INDEX IF NOT EXISTS ix_booking_receive_in_account_id       ON booking(receive_in_account_id);
CREATE INDEX IF NOT EXISTS ix_booking_item_booking_id             ON booking_item(booking_id);
CREATE INDEX IF NOT EXISTS ix_cash_flow_entry_account_tx_id       ON cash_flow_entry(account_tx_id);
CREATE INDEX IF NOT EXISTS ix_client_transferred_to_id            ON client(transferred_to_id);
CREATE INDEX IF NOT EXISTS ix_delivery_item_delivery_id           ON delivery_item(delivery_id);
CREATE INDEX IF NOT EXISTS ix_direct_sale_invoice_id              ON direct_sale(invoice_id);
CREATE INDEX IF NOT EXISTS ix_direct_sale_payment_account_id      ON direct_sale(payment_account_id);
CREATE INDEX IF NOT EXISTS ix_direct_sale_item_grn_item_id        ON direct_sale_item(grn_item_id);
CREATE INDEX IF NOT EXISTS ix_direct_sale_item_sale_id            ON direct_sale_item(sale_id);
CREATE INDEX IF NOT EXISTS ix_entry_invoice_id                    ON entry(invoice_id);
CREATE INDEX IF NOT EXISTS ix_fbm_rental_client_id                ON fbm_rental(client_id);
CREATE INDEX IF NOT EXISTS ix_fbm_rental_item_id                  ON fbm_rental(item_id);
CREATE INDEX IF NOT EXISTS ix_fbm_rental_payment_account_id       ON fbm_rental(payment_account_id);
CREATE INDEX IF NOT EXISTS ix_follow_up_contact_pending_bill_id   ON follow_up_contact(pending_bill_id);
CREATE INDEX IF NOT EXISTS ix_follow_up_contact_reminder_id       ON follow_up_contact(reminder_id);
CREATE INDEX IF NOT EXISTS ix_follow_up_reminder_pending_bill_id  ON follow_up_reminder(pending_bill_id);
CREATE INDEX IF NOT EXISTS ix_grn_payment_account_id              ON grn(payment_account_id);
CREATE INDEX IF NOT EXISTS ix_grn_supplier_id                     ON grn(supplier_id);
CREATE INDEX IF NOT EXISTS ix_grn_item_grn_id                     ON grn_item(grn_id);
CREATE INDEX IF NOT EXISTS ix_import_job_initiated_by             ON import_job(initiated_by);
CREATE INDEX IF NOT EXISTS ix_import_upload_uploaded_by           ON import_upload(uploaded_by);
CREATE INDEX IF NOT EXISTS ix_payment_payment_account_id          ON payment(payment_account_id);
CREATE INDEX IF NOT EXISTS ix_supplier_transferred_to_id          ON supplier(transferred_to_id);
CREATE INDEX IF NOT EXISTS ix_supplier_payment_payment_account_id ON supplier_payment(payment_account_id);
