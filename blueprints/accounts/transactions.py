"""transactions — split from accounts.py."""
from ._common import *  # noqa


def _audit_pending_account_transactions():
    """Append atomic structured audit events for newly posted ledger rows."""
    pending = [obj for obj in list(db.session.new) if isinstance(obj, AccountTransaction)]
    if not pending:
        return
    from utils.accounting_audit import record_accounting_audit
    db.session.flush()
    for tx in pending:
        record_accounting_audit(
            current_user, action='Create', entity_type='AccountTransaction', entity_id=tx.id,
            after={'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                   'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                   'description': tx.description, 'note': tx.note,
                   'source_type': tx.source_type, 'source_id': tx.source_id},
            amount_after=tx.amount,
            account_after_id=tx.to_account_id or tx.from_account_id,
            reason=tx.note,
        )


@accounts_bp.route('/transfers')
@login_required
def transfers():
    """View account transfers."""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    search = (request.args.get('q') or '').strip()
    date_from, date_to_excl = _parse_date_range()

    q = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type == 'Transfer',
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        q = q.filter(or_(AccountTransaction.description.ilike(like), AccountTransaction.note.ilike(like)))

    total_amount = q.with_entities(func.coalesce(func.sum(AccountTransaction.amount), 0)).scalar() or 0
    total_count = q.count()
    transfers = q.order_by(AccountTransaction.date_posted.desc()).paginate(page=page, per_page=per_page)

    return render_template('accounts/transfers.html', transfers=transfers,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1),
                           search=search, total_amount=total_amount, total_count=total_count)


@accounts_bp.route('/transactions/<int:tx_id>/void', methods=['POST'])
@login_required
def void_transaction(tx_id):
    """Legacy URL — permanently deletes the account entry."""
    return delete_account_transaction(tx_id)


@accounts_bp.route('/transactions/<int:tx_id>/delete', methods=['POST'])
@login_required
def delete_account_transaction(tx_id):
    """Delete an account entry and clean its linked Cash Flow and source records."""
    tx = AccountTransaction.query.get_or_404(tx_id)
    try:
        from app.services.system_accounts import is_reserved_account, is_legacy_loan_account, account_is_reconciliation_source
        if account_is_reconciliation_source(tx) or (tx.source_type or '').strip().lower() == 'loanprincipal' or any(
            is_reserved_account(db.session.get(Account, account_id))
            or is_legacy_loan_account(db.session.get(Account, account_id))
            for account_id in (tx.from_account_id, tx.to_account_id) if account_id
        ):
            raise ValueError('Reconciliation and loan-register entries are system-managed; use their dedicated module.')
        if tx.reconciliation_id or (tx.source_type or '') == 'AccountReconciliation':
            raise ValueError('Reconciliation adjustments are immutable and cannot be deleted.')
        source_type = (tx.source_type or '').strip()
        source_id = tx.source_id
        note_txt = tx.note or ''
        reason_txt = (request.form.get('void_reason') or request.form.get('reason') or tx.note or '').strip()
        if not source_type:
            pay_match = re.search(r'\[SRC:(?:Payment|ClientRefund):(\d+)\]', note_txt, flags=re.IGNORECASE)
            sp_match = re.search(r'\[SRC:SupplierPayment:(\d+)\]', note_txt, flags=re.IGNORECASE)
            gp_match = re.search(r'\[SRC:GRN_PAYMENT:(\d+)\]', note_txt, flags=re.IGNORECASE)
            dp_match = re.search(r'\[SRC:DeliveryPersonPayment:(\d+)\]', note_txt, flags=re.IGNORECASE)
            cf_match = re.search(r'\[SRC:CashFlow:(\d+)\]', note_txt, flags=re.IGNORECASE)
            ds_match = re.search(r'\[SRC:DirectSale:(\d+)\]', note_txt, flags=re.IGNORECASE)
            bk_match = re.search(r'\[SRC:Booking:(\d+)\]', note_txt, flags=re.IGNORECASE)
            if pay_match:
                source_type, source_id = 'Payment', int(pay_match.group(1))
            elif sp_match:
                source_type, source_id = 'SupplierPayment', int(sp_match.group(1))
            elif gp_match:
                source_type, source_id = 'GRN_PAYMENT', int(gp_match.group(1))
            elif dp_match:
                source_type, source_id = 'DeliveryPersonPayment', int(dp_match.group(1))
            elif cf_match:
                source_type, source_id = 'CashFlowEntry', int(cf_match.group(1))
            elif ds_match:
                source_type, source_id = 'DirectSale', int(ds_match.group(1))
            elif bk_match:
                source_type, source_id = 'Booking', int(bk_match.group(1))

        from app.services.accounting import _remove_account_tx
        from app.services.payments_crud import _assert_day_open, _assert_period_open
        from utils.accounting_audit import record_accounting_audit

        linked_cf = CashFlowEntry.query.filter_by(account_tx_id=tx.id).first()
        if linked_cf and source_type not in ('Payment', 'SupplierPayment', 'GRN_PAYMENT', 'DeliveryPersonPayment', 'DirectSale', 'Booking'):
            source_type = 'CashFlowEntry'
            source_id = source_id or linked_cf.id

        if source_type == 'Payment' and source_id:
            from app.services.payments_crud import delete_client_payment
            payment = db.session.get(Payment, int(source_id))
            if payment and not payment.is_void:
                delete_client_payment(payment, actor=current_user)
            else:
                for account_id in {tx.from_account_id, tx.to_account_id}:
                    _assert_period_open(account_id, tx.date_posted, operation='deleted')
                    _assert_day_open(account_id, tx.date_posted, operation='deleted')
                _remove_account_tx(tx)
        elif source_type == 'SupplierPayment' and source_id:
            from app.services.payments_crud import delete_supplier_payment
            payment = db.session.get(SupplierPayment, int(source_id))
            if payment and not payment.is_void:
                delete_supplier_payment(payment, actor=current_user)
            else:
                for account_id in {tx.from_account_id, tx.to_account_id}:
                    _assert_period_open(account_id, tx.date_posted, operation='deleted')
                    _assert_day_open(account_id, tx.date_posted, operation='deleted')
                _remove_account_tx(tx)
        elif source_type == 'GRN_PAYMENT' and source_id:
            from app.services.grn_payments import void_grn_payment
            payment = db.session.get(SupplierPayment, int(source_id))
            if payment and not payment.is_void:
                void_grn_payment(payment, actor=current_user, reason=reason_txt)
            else:
                for account_id in {tx.from_account_id, tx.to_account_id}:
                    _assert_period_open(account_id, tx.date_posted, operation='deleted')
                    _assert_day_open(account_id, tx.date_posted, operation='deleted')
                _remove_account_tx(tx)
        elif source_type == 'DeliveryPersonPayment' and source_id:
            from app.services.driver_payments import delete_driver_payment
            payment = db.session.get(DeliveryPersonPayment, int(source_id))
            if payment and not payment.is_void:
                delete_driver_payment(payment, actor=current_user)
            else:
                for account_id in {tx.from_account_id, tx.to_account_id}:
                    _assert_period_open(account_id, tx.date_posted, operation='deleted')
                    _assert_day_open(account_id, tx.date_posted, operation='deleted')
                _remove_account_tx(tx)
        elif source_type == 'DirectSale' and source_id:
            for account_id in {tx.from_account_id, tx.to_account_id}:
                _assert_period_open(account_id, tx.date_posted, operation='deleted')
                _assert_day_open(account_id, tx.date_posted, operation='deleted')
            sale = db.session.get(DirectSale, int(source_id))
            if sale:
                sale.paid_amount = 0
                sale.payment_account_id = None
                from app.services.void_rebuild import _sync_direct_sale_pending_bill
                _sync_direct_sale_pending_bill(sale)
            before = {'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                      'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                      'description': tx.description, 'note': tx.note, 'is_void': False}
            record_accounting_audit(
                current_user, action='Delete', entity_type='AccountTransaction', entity_id=tx.id,
                before=before, after={**before, 'deleted': True},
                amount_before=tx.amount, amount_after=0,
                account_before_id=tx.from_account_id or tx.to_account_id,
                reason=reason_txt,
            )
            _remove_account_tx(tx)
        elif source_type == 'Booking' and source_id:
            for account_id in {tx.from_account_id, tx.to_account_id}:
                _assert_period_open(account_id, tx.date_posted, operation='deleted')
                _assert_day_open(account_id, tx.date_posted, operation='deleted')
            booking = db.session.get(Booking, int(source_id))
            if booking:
                booking.paid_amount = 0
                booking.receive_in_account_id = None
                from app.services.void_rebuild import _sync_booking_pending_bill
                _sync_booking_pending_bill(booking)
            before = {'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                      'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                      'description': tx.description, 'note': tx.note, 'is_void': False}
            record_accounting_audit(
                current_user, action='Delete', entity_type='AccountTransaction', entity_id=tx.id,
                before=before, after={**before, 'deleted': True},
                amount_before=tx.amount, amount_after=0,
                account_before_id=tx.from_account_id or tx.to_account_id,
                reason=reason_txt,
            )
            _remove_account_tx(tx)
        elif source_type == 'CashFlowEntry':
            from app.services.cash_flow_svc import void_manual_cash_flow_entry
            entry = linked_cf or (db.session.get(CashFlowEntry, int(source_id)) if source_id else None)
            if entry:
                void_manual_cash_flow_entry(
                    entry, reason=reason_txt,
                    actor=current_user,
                )
            else:
                for account_id in {tx.from_account_id, tx.to_account_id}:
                    _assert_period_open(account_id, tx.date_posted, operation='deleted')
                    _assert_day_open(account_id, tx.date_posted, operation='deleted')
                before = {'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                          'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                          'description': tx.description, 'note': tx.note, 'is_void': False}
                record_accounting_audit(
                    current_user, action='Delete', entity_type='AccountTransaction', entity_id=tx.id,
                    before=before, after={**before, 'deleted': True},
                    amount_before=tx.amount, amount_after=0,
                    account_before_id=tx.from_account_id or tx.to_account_id,
                    reason=reason_txt,
                )
                _remove_account_tx(tx)
        else:
            for account_id in {tx.from_account_id, tx.to_account_id}:
                _assert_period_open(account_id, tx.date_posted, operation='deleted')
                _assert_day_open(account_id, tx.date_posted, operation='deleted')
            before = {'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                      'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                      'description': tx.description, 'note': tx.note, 'is_void': False}
            record_accounting_audit(
                current_user, action='Delete', entity_type='AccountTransaction', entity_id=tx.id,
                before=before, after={**before, 'deleted': True},
                amount_before=tx.amount, amount_after=0,
                account_before_id=tx.from_account_id or tx.to_account_id,
                reason=reason_txt,
            )
            _remove_account_tx(tx)
        _audit_pending_account_transactions()
        db.session.commit()
        flash('Account entry deleted and balances reversed.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Delete account transaction failed')
        flash(f'Unable to delete transaction: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.dashboard'))


@accounts_bp.route('/transactions/<int:tx_id>/edit', methods=['POST'])
@login_required
def edit_account_transaction(tx_id):
    tx = AccountTransaction.query.get_or_404(tx_id)
    try:
        from app.services.system_accounts import is_reserved_account, is_legacy_loan_account, account_is_reconciliation_source
        if account_is_reconciliation_source(tx) or (tx.source_type or '').strip().lower() == 'loanprincipal' or any(
            is_reserved_account(db.session.get(Account, account_id))
            or is_legacy_loan_account(db.session.get(Account, account_id))
            for account_id in (tx.from_account_id, tx.to_account_id) if account_id
        ):
            raise ValueError('Reconciliation and loan-register entries are system-managed; use their dedicated module.')
        if tx.reconciliation_id or tx.source_type in ('Payment', 'SupplierPayment', 'AccountReconciliation') or re.search(r'\[SRC:(?:Payment|ClientRefund|SupplierPayment):\d+\]', tx.note or '', re.IGNORECASE):
            raise ValueError('Linked financial entries must be edited from their shared source form; reconciliation entries are immutable.')
        from app.services.system_accounts import is_reserved_account, is_legacy_loan_account
        for field in ('from_account_id', 'to_account_id'):
            submitted_id = request.form.get(field, type=int)
            if submitted_id and is_reserved_account(db.session.get(Account, submitted_id)):
                raise ValueError('Daily reconciliation counter accounts are system-only and cannot be used for manual Accounts entries.')
            if submitted_id and is_legacy_loan_account(db.session.get(Account, submitted_id)):
                raise ValueError('Legacy loan accounts are read-only. Record principal movements in the Loan Register.')
        from app.services.payments_crud import _assert_period_open
        for account_id in {tx.from_account_id, tx.to_account_id}:
            _assert_period_open(account_id, tx.date_posted, operation='edited')
        new_amount = _money_round(request.form.get('amount', tx.amount) or 0)
        if new_amount <= 0:
            raise ValueError('Amount must be greater than zero.')
        new_desc = (request.form.get('description') or tx.description or '').strip()
        new_note = (request.form.get('note') or tx.note or '').strip()
        date_raw = (request.form.get('date_posted') or '').strip()
        if date_raw:
            try:
                new_dt = datetime.strptime(date_raw, '%Y-%m-%dT%H:%M')
            except ValueError:
                new_dt = tx.date_posted
        else:
            new_dt = tx.date_posted

        linked_flow = (CashFlowEntry.query.filter_by(account_tx_id=tx.id).first()
                       if (tx.source_type or '') == 'CashFlowEntry' else None)
        if linked_flow:
            from app.services.cash_flow_svc import update_manual_cash_flow_entry
            update_manual_cash_flow_entry(
                linked_flow, direction=linked_flow.direction, amount=new_amount,
                account_id=(request.form.get('from_account_id', type=int) or linked_flow.account_id),
                destination_account_id=linked_flow.destination_account_id,
                category_id=linked_flow.category_id,
                subcategory_id=linked_flow.subcategory_id,
                party_id=linked_flow.party_id,
                party_name=linked_flow.party_name,
                party_type=linked_flow.party_type,
                description=new_desc, note=new_note,
                reference=linked_flow.reference, date_posted=new_dt,
                reason=(request.form.get('edit_reason') or new_note),
                actor=current_user, create_missing=False,
            )
            db.session.commit()
            flash('Expense and its Cash Flow classification were updated together.', 'success')
            return redirect(request.referrer or url_for('accounts.dashboard'))

        from app.services.accounting import _apply_account_tx_effect, _reverse_account_tx_effect
        from utils.accounting_audit import record_accounting_audit
        from utils.money import to_minor
        before = {'id': tx.id, 'amount': tx.amount, 'description': tx.description, 'note': tx.note,
                  'date_posted': tx.date_posted, 'from_account_id': tx.from_account_id,
                  'to_account_id': tx.to_account_id}
        if not tx.is_void:
            _reverse_account_tx_effect(tx)
        tx.amount = new_amount
        tx.amount_minor = to_minor(new_amount)
        tx.description = new_desc
        tx.note = new_note
        tx.date_posted = new_dt
        tx.is_void = False
        from_id = request.form.get('from_account_id', type=int)
        to_id = request.form.get('to_account_id', type=int)
        if from_id:
            tx.from_account_id = from_id
        if to_id:
            tx.to_account_id = to_id
        _apply_account_tx_effect(tx)
        after = {'id': tx.id, 'amount': tx.amount, 'description': tx.description, 'note': tx.note,
                 'date_posted': tx.date_posted, 'from_account_id': tx.from_account_id,
                 'to_account_id': tx.to_account_id}
        record_accounting_audit(
            current_user, action='Edit', entity_type='AccountTransaction', entity_id=tx.id,
            before=before, after=after, amount_before=before['amount'], amount_after=after['amount'],
            account_before_id=before['from_account_id'] or before['to_account_id'],
            account_after_id=after['from_account_id'] or after['to_account_id'], reason=new_note,
        )
        _audit_pending_account_transactions()
        db.session.commit()
        flash('Account entry updated.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Edit account transaction failed')
        flash(f'Unable to edit transaction: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.dashboard'))


@accounts_bp.route('/transactions/new', methods=['POST'])
@login_required
def add_transaction():
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    tx_mode = (request.form.get('tx_mode') or '').strip().lower()
    note = (request.form.get('note') or '').strip()
    method = (request.form.get('method') or 'Cash').strip()
    tx_date_raw = (request.form.get('date_posted') or '').strip()
    tx_date = pk_now()
    if tx_date_raw:
        try:
            tx_date = datetime.strptime(tx_date_raw, '%Y-%m-%dT%H:%M')
        except ValueError:
            tx_date = pk_now()

    try:
        from app.services.system_accounts import is_reserved_account, is_legacy_loan_account
        for field in ('receive_account_id', 'receive_from_account_id', 'pay_from_account_id', 'pay_to_account_id'):
            selected_id = request.form.get(field, type=int)
            selected_account = db.session.get(Account, selected_id) if selected_id else None
            if selected_account and is_reserved_account(selected_account):
                raise ValueError('Daily reconciliation counter accounts are system-only and cannot be used for manual Accounts entries.')
            if selected_account and is_legacy_loan_account(selected_account):
                raise ValueError('Legacy loan accounts are read-only. Record borrowed principal or repayments in the Loan Register.')
        if (request.form.get('receive_from_category') or '').strip().lower() == 'loan' or (request.form.get('pay_target') or '').strip().lower() == 'loan':
            raise ValueError('Record loan principal received or repaid in the Loan Register.')
        if tx_mode == 'receive':
            receive_account_id = request.form.get('receive_account_id', type=int)
            receive_from_category = (request.form.get('receive_from_category') or 'client_ledger').strip()
            client_input = (request.form.get('client_input') or '').strip()
            receive_from_account_id = request.form.get('receive_from_account_id', type=int)
            receive_source_label = (request.form.get('receive_source_label') or '').strip()
            amount = float(request.form.get('amount', 0) or 0)
            discount = float(request.form.get('discount', 0) or 0)

            if amount < 0:
                raise ValueError('Received amount cannot be negative.')
            if discount < 0:
                raise ValueError('Discount cannot be negative.')
            if (amount + discount) <= 0:
                raise ValueError('Received amount and discount cannot both be zero.')

            receive_account = Account.query.get(receive_account_id) if receive_account_id else None
            if amount > 0:
                if not _is_account_active(receive_account):
                    raise ValueError('Please select a valid destination account.')
                _validate_account_matches_method(receive_account, method, 'Destination')

            if receive_from_category == 'client_ledger':
                client = _resolve_client(client_input, active_only=True)
                if not client:
                    raise ValueError('Client not found or suspended. Please select a valid client from the dues list.')

                from app.services.payments_crud import save_client_payment
                payment, _ = save_client_payment(
                    client_code=client.code, client_name=client.name,
                    amount=amount, discount=discount,
                    discount_reason=('Accounts receive transaction' if discount > 0 else ''),
                    payment_type=('Receipt' if amount > 0 else 'Waive-Off'),
                    method=method or 'Cash',
                    payment_account_id=(receive_account.id if receive_account else None),
                    date_posted=tx_date, note=note, actor=current_user,
                )
                account_label = receive_account.name if receive_account else 'discount-only'
                audit_log(current_user, 'account.transaction.receive', f'source_category=client_ledger, client={client.name}, account={account_label}, amount={amount}, discount={discount}')

            elif receive_from_category == 'other_source':
                if amount <= 0:
                    raise ValueError('Received amount must be greater than zero.')
                if not receive_source_label:
                    raise ValueError('Please enter who or what this money was received from.')

                from app.services.accounting import _apply_account_tx_effect
                from utils.money import to_minor
                account_tx = AccountTransaction(
                    from_account_id=None,
                    to_account_id=receive_account.id,
                    amount=amount,
                    amount_minor=to_minor(amount),
                    description=f"Money received from {receive_source_label}",
                    note=note,
                    transaction_type='Receipt',
                    date_posted=tx_date
                )
                db.session.add(account_tx)
                db.session.flush()
                _apply_account_tx_effect(account_tx)
                audit_log(current_user, 'account.transaction.receive', f'source_category=other_source, source={receive_source_label}, to={receive_account.name}, amount={amount}')

            else:
                category_exists = AccountCategory.query.filter(
                    func.lower(func.trim(AccountCategory.name)) == receive_from_category.lower(),
                    AccountCategory.is_active == True
                ).first()
                if not category_exists:
                    raise ValueError('Please select a valid receive source category.')
                if amount <= 0:
                    raise ValueError('Received amount must be greater than zero.')

                from_account = Account.query.get(receive_from_account_id) if receive_from_account_id else None
                if not _is_account_active(from_account):
                    raise ValueError('Please select a valid source account.')
                if (from_account.source_category or '').strip().lower() != category_exists.name.lower():
                    raise ValueError('Selected source account does not belong to the chosen category.')
                if from_account.id == receive_account.id:
                    raise ValueError('Source and destination accounts cannot be the same.')
                # Allow loan accounts to go negative: do not enforce insufficient-balance check for Loan group
                is_loan_source = ((from_account.source_category or '').strip().lower() == 'loan') or ((category_exists.name or '').strip().lower() == 'loan')
                if not is_loan_source and float(from_account.balance or 0) < amount:
                    raise ValueError('Insufficient balance in selected source account.')

                from app.services.accounting import _apply_account_tx_effect
                from utils.money import to_minor
                account_tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=receive_account.id,
                    amount=amount,
                    amount_minor=to_minor(amount),
                    description=f"Funds received from account {from_account.name}",
                    note=note,
                    transaction_type='Transfer',
                    date_posted=tx_date
                )
                db.session.add(account_tx)
                db.session.flush()
                _apply_account_tx_effect(account_tx)
                audit_log(current_user, 'account.transaction.receive', f'source_category={category_exists.name}, from={from_account.name}, to={receive_account.name}, amount={amount}')

            _audit_pending_account_transactions()
            db.session.commit()
            flash('Receive transaction recorded successfully.', 'success')

        elif tx_mode == 'pay':
            # A GRN payment has its own per-expense system (one money field per
            # expense, its own route).  If this request ever reaches here, the
            # modal posted an expense box through the ordinary transaction
            # route: post nothing and hand the operator to the screen that
            # knows which expense each rupee belongs to.
            if (request.form.get('pay_target') or '').strip().lower() == 'grn_payment':
                grn_id = (request.form.get('grn_expense_grn_id', type=int)
                          or request.form.get('grn_payment_grn_id', type=int))
                flash('Pay each GRN expense in its own box on Pay GRN Expenses.', 'info')
                db.session.rollback()
                return redirect(url_for('accounts.grn_payments', **({
                    'grn_id': grn_id, 'mode': 'EXPENSES'} if grn_id else {})))
            from_account_id = request.form.get('pay_from_account_id', type=int)
            to_account_id = request.form.get('pay_to_account_id', type=int) or request.form.get('transfer_to_account_id', type=int)
            pay_target = (request.form.get('pay_target') or '').strip().lower()
            amount = float(request.form.get('amount', 0) or 0)

            if amount <= 0:
                raise ValueError('Payment amount must be greater than zero.')

            from_account = Account.query.get(from_account_id) if from_account_id else None
            if not _is_account_active(from_account):
                raise ValueError('Please select a valid source account.')
            _validate_account_matches_method(from_account, method, 'Source')
            if float(from_account.balance or 0) < amount:
                raise ValueError('Insufficient balance in selected source account.')

            if pay_target == 'company_transfer':
                to_account = Account.query.get(to_account_id) if to_account_id else None
                if not _is_account_active(to_account):
                    raise ValueError('Please select a valid destination account.')
                if to_account.id == from_account.id:
                    raise ValueError('Source and destination accounts cannot be the same.')

                from app.services.accounting import _apply_account_tx_effect
                from utils.money import to_minor
                tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=to_account.id,
                    amount=amount,
                    amount_minor=to_minor(amount),
                    description='Intra-company transfer',
                    note=note,
                    transaction_type='Transfer',
                    date_posted=tx_date
                )
                db.session.add(tx)
                db.session.flush()
                _apply_account_tx_effect(tx)
                audit_log(current_user, 'account.transaction.transfer', f'from={from_account.name}, to={to_account.name}, amount={amount}')
                flash('Transfer transaction recorded successfully.', 'success')

            elif pay_target == 'supplier':
                supplier_id = request.form.get('supplier_id', type=int)
                supplier_input = (request.form.get('supplier_input') or '').strip()
                supplier = Supplier.query.get(supplier_id) if supplier_id else None
                if not supplier and supplier_input:
                    supplier = _resolve_supplier(supplier_input)
                if not supplier:
                    raise ValueError('Please select a valid supplier.')

                if not supplier.is_active:
                    raise ValueError('The selected supplier is suspended.')
                from app.services.payments_crud import save_supplier_payment
                sp, _ = save_supplier_payment(
                    supplier_id=supplier.id, amount=amount, method=method or 'Cash',
                    payment_account_id=from_account.id, date_posted=tx_date,
                    note=note, actor=current_user,
                )
                audit_log(current_user, 'account.transaction.supplier_payment', f'from={from_account.name}, supplier={supplier.name}, amount={amount}')
                flash('Supplier payment recorded successfully.', 'success')
            elif pay_target == 'driver':
                # Accounts-section entry point for a driver service payment.
                # Delegates to the same core as the Driver section, so exactly
                # one financial transaction exists and it is automatically
                # visible in the driver ledger.
                driver_id = request.form.get('delivery_person_id', type=int)
                driver_input = (request.form.get('driver_input') or '').strip()
                driver = db.session.get(DeliveryPerson, driver_id) if driver_id else None
                if not driver and driver_input:
                    driver = DeliveryPerson.query.filter(
                        func.lower(func.trim(DeliveryPerson.name)) == driver_input.lower()
                    ).first()
                if not driver:
                    raise ValueError('Please select a valid delivery person / driver.')
                if not driver.is_active:
                    raise ValueError('The selected delivery person is inactive.')

                from app.services.driver_payments import settle_driver_fifo
                rows = settle_driver_fifo(
                    delivery_person_id=driver.id, amount_paid=amount,
                    method=method or 'Cash', payment_account_id=from_account.id,
                    reference=(request.form.get('reference') or '').strip(),
                    date_posted=tx_date, note=note,
                    idempotency_key=(request.form.get('idempotency_key') or '').strip() or None,
                    actor=current_user,
                )
                audit_log(current_user, 'account.transaction.driver_payment',
                          f'from={from_account.name}, driver={driver.name}, amount={amount}, rows={len(rows)}')
                flash('Driver service payment recorded. The driver ledger and the account balance were updated together.', 'success')
            elif pay_target == 'client_refund':
                # Refund issued to a client: record refund audit (Payment negative for client ledger),
                # create account transaction for cash out so cash flow reflects it, and keep everything atomic.
                client_id = request.form.get('client_id_refund', type=int)
                client_input = (request.form.get('client_input_refund') or '').strip()
                client = Client.query.get(client_id) if client_id else None
                if not client and client_input:
                    client = _resolve_client(client_input, active_only=True)
                if not client:
                    raise ValueError('Please select a valid client.')

                if not client.is_active:
                    raise ValueError('The selected client is suspended.')
                from app.services.payments_crud import save_client_payment
                payment, _ = save_client_payment(
                    client_code=client.code, client_name=client.name,
                    amount=amount, discount=0, payment_type='Refund',
                    method=method or 'Cash', payment_account_id=from_account.id,
                    date_posted=tx_date, note=note, actor=current_user,
                )
                audit_log(current_user, 'account.transaction.client_refund', f'from={from_account.name}, client={client.name}, amount={amount}')
                flash('Client refund recorded successfully.', 'success')
            elif pay_target == 'loan':
                # Repayment to a Loan account: credit the loan account (moves negative toward zero)
                to_account = Account.query.get(to_account_id) if to_account_id else None
                if not _is_account_active(to_account):
                    raise ValueError('Please select a valid loan account.')
                if (to_account.source_category or '').strip().lower() != 'loan':
                    raise ValueError('Selected destination is not a Loan account.')
                if to_account.id == from_account.id:
                    raise ValueError('Source and destination accounts cannot be the same.')

                from_account.balance = _money_round(float(from_account.balance or 0) - amount)
                to_account.balance = _money_round(float(to_account.balance or 0) + amount)

                tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=to_account.id,
                    amount=amount,
                    description=f'Loan repayment to {to_account.name}',
                    note=note,
                    transaction_type='Payment',
                    date_posted=tx_date
                )
                db.session.add(tx)
                audit_log(current_user, 'account.transaction.loan_payment', f'from={from_account.name}, to_loan={to_account.name}, amount={amount}')
                flash('Loan payment recorded successfully.', 'success')

            elif pay_target in ('personal_expense', 'other_expense'):
                # Categories, subcategories and parties are mastered in Cash
                # Flow, but the money movement is entered only from Accounts.
                # Reuse the paired posting service so there is exactly one
                # account movement and one linked classification row.
                target_label = (request.form.get('target_label') or '').strip()
                if not target_label:
                    raise ValueError('Purpose / description is required for an expense.')
                if from_account.id not in {a.id for a in _company_accounts()}:
                    raise ValueError('Select a valid company cash or bank account for the expense.')

                from app.services.cash_flow_svc import (
                    SRC_ACCOUNT_EXPENSE, save_manual_cash_flow_entry,
                )
                entry, created = save_manual_cash_flow_entry(
                    direction='out', amount=amount, account_id=from_account.id,
                    category_id=request.form.get('expense_category_id', type=int),
                    subcategory_id=request.form.get('expense_subcategory_id', type=int),
                    party_id=request.form.get('expense_party_id', type=int),
                    party_name='',
                    description=target_label, note=note,
                    reference=(request.form.get('reference') or '').strip(),
                    date_posted=tx_date,
                    idempotency_key=(request.form.get('idempotency_key') or '').strip() or None,
                    actor=current_user, create_missing=False,
                    require_party=True, allow_party_create=False,
                    entry_source=SRC_ACCOUNT_EXPENSE,
                )
                if created:
                    audit_log(
                        current_user, 'account.transaction.expense',
                        f'from={from_account.name}, category_id={entry.category_id}, '
                        f'party_id={entry.party_id}, amount={amount}, entry_id={entry.id}',
                    )
                    flash('Expense recorded with its Cash Flow category and recipient.', 'success')
                else:
                    flash('This expense was already saved.', 'info')

            else:
                target_label = (request.form.get('target_label') or '').strip()
                if not target_label:
                    if pay_target == 'loan':
                        target_label = 'Loan Payment'
                    elif pay_target == 'personal_expense':
                        target_label = 'Personal Expense'
                    else:
                        target_label = 'Other Payment'

                from app.services.accounting import _apply_account_tx_effect
                from utils.money import to_minor
                tx_type = 'Expense' if pay_target in ['personal_expense', 'other_expense'] else 'Payment'
                tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=None,
                    amount=amount,
                    amount_minor=to_minor(amount),
                    description=target_label,
                    note=note,
                    transaction_type=tx_type,
                    date_posted=tx_date
                )
                db.session.add(tx)
                db.session.flush()
                _apply_account_tx_effect(tx)
                audit_log(current_user, 'account.transaction.pay', f'from={from_account.name}, target={target_label}, amount={amount}')
                flash('Outgoing payment recorded successfully.', 'success')

            _audit_pending_account_transactions()
            db.session.commit()
        else:
            raise ValueError('Invalid transaction type selected.')

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Accounts transaction save failed')
        flash(f'Unable to save transaction: {exc}', 'danger')

    return redirect(url_for('accounts.dashboard'))


@accounts_bp.route('/transfers/add', methods=['GET', 'POST'])
@login_required
def add_transfer():
    """Add a new account transfer."""
    if request.method == 'POST':
        try:
            from utils.money import from_minor, to_minor
            from app.services.accounting import _apply_account_tx_effect
            from_account_id = request.form.get('from_account', type=int)
            to_account_id = request.form.get('to_account', type=int)
            amount_minor = to_minor(request.form.get('amount', 0), field='Transfer amount')
            if amount_minor <= 0:
                raise ValueError('Transfer amount must be greater than zero.')
            from_account = db.session.get(Account, from_account_id) if from_account_id else None
            to_account = db.session.get(Account, to_account_id) if to_account_id else None
            if not _is_account_active(from_account) or not _is_account_active(to_account):
                raise ValueError('Select valid active source and destination accounts.')
            from app.services.recon_counter import is_counter_account
            if is_counter_account(from_account) or is_counter_account(to_account):
                raise ValueError(
                    'Daily reconciliation counter accounts cannot take manual '
                    'transfers. Resolve the difference from the differences report.'
                )
            from app.services.system_accounts import is_legacy_loan_account
            if any(is_legacy_loan_account(account) for account in (from_account, to_account)):
                raise ValueError('Legacy loan accounts are read-only. Record principal movements in the Loan Register.')
            if from_account.id == to_account.id:
                raise ValueError('Source and destination accounts cannot be the same.')
            available_minor = int(from_account.balance_minor) if from_account.balance_minor is not None else to_minor(from_account.balance or 0)
            if available_minor < amount_minor:
                raise ValueError('Insufficient balance in the source account.')
            transaction = AccountTransaction(
                from_account_id=from_account.id, to_account_id=to_account.id,
                amount=float(from_minor(amount_minor)), amount_minor=amount_minor,
                description=(request.form.get('description') or 'Account transfer').strip(),
                note=(request.form.get('note') or '').strip(),
                transaction_type='Transfer', created_by=getattr(current_user, 'username', None),
                date_posted=pk_now(),
            )
            db.session.add(transaction)
            db.session.flush()
            _apply_account_tx_effect(transaction)
            _audit_pending_account_transactions()
            db.session.commit()
            flash('Transfer completed successfully!', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Account transfer failed')
            flash(f'Unable to record transfer: {exc}', 'danger')
        return redirect(url_for('accounts.transfers'))
    
    accounts = _active_accounts().filter(
        func.lower(func.trim(Account.category)).in_(['cash', 'bank'])
    ).order_by(Account.name.asc(), Account.id.asc()).all()
    return render_template(
        'accounts/add_transfer.html', accounts=accounts,
        account_options=[{'id': a.id, 'label': _account_option_label(a)} for a in accounts],
        account_data={str(a.id): {'name': a.name, 'balance': _money_round(a.balance)} for a in accounts},
    )


