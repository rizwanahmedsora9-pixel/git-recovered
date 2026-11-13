"""cash — configuration-driven Cash Flow routes."""
from ._common import *  # noqa

logger = logging.getLogger(__name__)

from app.services.cash_flow_svc import (
    CF_DIR_IN,
    CF_DIR_OUT,
    CF_DIR_TRANSFER,
    CF_PARTY_TYPES,
    CF_SOURCE_LABELS,
    apply_running_balance,
    categories_for_direction,
    cf_used_category_ids,
    cf_used_party_ids,
    cf_used_subcategory_ids,
    collect_cash_flow_rows,
    compute_physical_cash_difference,
    delete_cf_category,
    delete_cf_party,
    delete_cf_subcategory,
    disable_cf_category,
    disable_cf_party,
    disable_cf_subcategory,
    enable_cf_category,
    enable_cf_party,
    enable_cf_subcategory,
    filter_cash_flow_rows,
    parse_physical_cash_amount,
    rename_cf_category,
    rename_cf_subcategory,
    restore_manual_cash_flow_entry,
    save_cf_category,
    save_cf_party,
    save_cf_subcategory,
    save_manual_cash_flow_entry,
    subcategories_for_category,
    summarize_cash_flow_rows,
    update_cf_party,
    update_manual_cash_flow_entry,
    void_manual_cash_flow_entry,
    _cf_account_label,
    _cf_company_accounts,
    _cf_ensure_indexes,
    _cf_normalize_direction,
    _cf_sort_key,
    _cf_type_label,
)


def _cf_redirect(**kwargs):
    params = {k: v for k, v in kwargs.items() if v not in (None, '')}
    return redirect(url_for('cash_flow', **params))


def _cf_filter_kwargs(source):
    return {
        'from_date': source.get('from_date'),
        'to_date': source.get('to_date'),
        'filter_type': source.get('filter_type') or 'all',
        'origin': source.get('origin') or 'all',
        'category': source.get('category') or '',
        'subcategory': source.get('subcategory') or '',
        'party_type': source.get('party_type') or '',
        'party': source.get('party') or '',
        'account_id': source.get('account_id') or '',
        'q': source.get('q') or '',
        'notes': source.get('notes') or '',
        'reference': source.get('reference') or '',
        'description': source.get('description') or '',
        'created_by': source.get('created_by') or '',
        'status': source.get('status') or 'active',
        'amount_min': source.get('amount_min') or '',
        'amount_max': source.get('amount_max') or '',
    }


def _cf_json_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


@bp.route('/cash_flow/meta/categories', methods=['GET', 'POST'])
@login_required
def cash_flow_categories_meta():
    if request.method == 'POST':
        payload = _cf_json_payload()
        try:
            cat, created = save_cf_category(
                payload.get('name') or payload.get('new_category_name'),
                payload.get('direction') or payload.get('new_category_direction') or 'both',
                notes=payload.get('notes') or payload.get('new_category_notes'),
                actor=current_user,
            )
            db.session.commit()
            return jsonify({
                'ok': True,
                'created': created,
                'category': {
                    'id': cat.id,
                    'name': cat.name,
                    'direction': cat.direction or 'both',
                    'notes': cat.notes or '',
                },
            })
        except ValueError as exc:
            db.session.rollback()
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow quick-add category failed')
            return jsonify({'ok': False, 'error': 'Unable to save category.'}), 500
    direction = _cf_normalize_direction(request.args.get('direction') or '')
    cats = categories_for_direction(direction or 'both')
    return jsonify({
        'categories': [
            {'id': c.id, 'name': c.name, 'direction': c.direction or 'both', 'notes': c.notes or ''}
            for c in cats
        ]
    })


@bp.route('/cash_flow/meta/subcategories', methods=['GET', 'POST'])
@login_required
def cash_flow_subcategories_meta():
    if request.method == 'POST':
        payload = _cf_json_payload()
        try:
            raw_parent = payload.get('category_id') or payload.get('new_sub_category_id')
            try:
                parent_id = int(raw_parent) if raw_parent not in (None, '') else None
            except (TypeError, ValueError):
                parent_id = None
            if not parent_id and payload.get('category_name'):
                cat_obj = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == payload.get('category_name').strip().lower()).first()
                if cat_obj:
                    parent_id = cat_obj.id
            sub, created = save_cf_subcategory(
                parent_id,
                payload.get('name') or payload.get('new_subcategory_name'),
                notes=payload.get('notes') or payload.get('new_subcategory_notes'),
                actor=current_user,
            )
            db.session.commit()
            return jsonify({
                'ok': True,
                'created': created,
                'subcategory': {
                    'id': sub.id,
                    'name': sub.name,
                    'category_id': sub.category_id,
                    'notes': sub.notes or '',
                },
            })
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow quick-add subcategory failed')
            return jsonify({'ok': False, 'error': 'Unable to save sub-category.'}), 500
    category_id = request.args.get('category_id', type=int)
    if not category_id:
        return jsonify({'subcategories': []})
    return jsonify({
        'subcategories': [
            {'id': s.id, 'name': s.name, 'category_id': s.category_id, 'notes': s.notes or ''}
            for s in subcategories_for_category(category_id)
        ]
    })


@bp.route('/cash_flow/meta/parties', methods=['GET', 'POST'])
@login_required
def cash_flow_parties_meta():
    if request.method == 'POST':
        payload = _cf_json_payload()
        try:
            party, created = save_cf_party(
                payload.get('name') or payload.get('new_party_name'),
                payload.get('party_type') or payload.get('new_party_type') or 'person',
                note=payload.get('note') or payload.get('new_party_note'),
                actor=current_user,
            )
            db.session.commit()
            return jsonify({
                'ok': True,
                'created': created,
                'party': {
                    'id': party.id,
                    'name': party.name,
                    'party_type': party.party_type or 'other',
                    'note': party.note or '',
                },
            })
        except ValueError as exc:
            db.session.rollback()
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow quick-add party failed')
            return jsonify({'ok': False, 'error': 'Unable to save party.'}), 500
    parties = CashFlowParty.query.filter_by(is_active=True).order_by(CashFlowParty.name).all()
    return jsonify({
        'parties': [
            {'id': p.id, 'name': p.name, 'party_type': p.party_type or 'other', 'note': p.note or ''}
            for p in parties
        ]
    })


@bp.route('/cash_flow/entry/<int:entry_id>.json')
@login_required
def cash_flow_entry_json(entry_id):
    entry = CashFlowEntry.query.get_or_404(entry_id)
    dest = getattr(entry, 'destination_account', None)
    audits = []
    for a in sorted(getattr(entry, 'audit_trail', []) or [], key=lambda x: x.id):
        audits.append({
            'action': a.action,
            'reason': a.reason or '',
            'changed_by': a.changed_by or '',
            'changed_at': a.changed_at.strftime('%Y-%m-%d %H:%M') if a.changed_at else '',
        })
    return jsonify({
        'id': entry.id,
        'direction': entry.direction,
        'amount': float(entry.amount or 0),
        'account_id': entry.account_id,
        'account_name': entry.account.name if entry.account else '',
        'destination_account_id': getattr(entry, 'destination_account_id', None),
        'destination_account_name': dest.name if dest else '',
        'category_id': entry.category_id,
        'category_name': entry.category.name if entry.category else '',
        'subcategory_id': entry.subcategory_id,
        'subcategory_name': entry.subcategory.name if entry.subcategory else '',
        'party_id': entry.party_id,
        'party_name': entry.party_name or '',
        'party_type': entry.party_type or 'other',
        'description': entry.description or '',
        'note': entry.note or '',
        'reference': getattr(entry, 'reference', None) or '',
        'date_posted': entry.date_posted.strftime('%Y-%m-%dT%H:%M') if entry.date_posted else '',
        'is_void': bool(entry.is_void),
        'void_reason': getattr(entry, 'void_reason', None) or '',
        'voided_by': getattr(entry, 'voided_by', None) or '',
        'created_by': entry.created_by or '',
        'updated_by': getattr(entry, 'updated_by', None) or '',
        'status': 'voided' if entry.is_void else 'active',
        'audit': audits,
    })


@bp.route('/cash_flow', methods=['GET', 'POST'])
@login_required
def cash_flow():
    _cf_ensure_indexes()
    source = request.form if request.method == 'POST' else request.args
    fresh_start_dt = pk_today()
    fresh_start_date = fresh_start_dt.strftime('%Y-%m-%d')
    from_date = source.get('from_date', fresh_start_date)
    to_date = source.get('to_date', fresh_start_date)
    filter_kwargs = _cf_filter_kwargs(source)
    filter_type = filter_kwargs['filter_type']
    opening_balance_input = source.get('opening_balance', '').strip()
    export_pdf = request.args.get('export_pdf', '')
    export_csv = request.args.get('export_csv', '')
    export_scope = (request.args.get('export_scope') or 'filtered').strip().lower()

    adjustment_date_input = to_date
    physical_cash_input = ''
    reconciliation_reason = ''
    action = ''
    if request.method == 'POST':
        adjustment_date_input = request.form.get('adjustment_date', to_date).strip() or to_date
        physical_cash_input = request.form.get('physical_cash_available', '').strip()
        reconciliation_reason = request.form.get('reconciliation_reason', '').strip()
        action = request.form.get('action', '').strip()

    def _back():
        return _cf_redirect(**{**filter_kwargs, 'from_date': from_date, 'to_date': to_date})

    if request.method == 'POST' and action == 'add_category':
        try:
            save_cf_category(
                request.form.get('new_category_name'),
                request.form.get('new_category_direction') or 'both',
                notes=request.form.get('new_category_notes'),
                actor=current_user,
            )
            db.session.commit()
            flash('Category saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow add category failed')
            flash(f'Unable to save category: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action == 'add_subcategory':
        try:
            save_cf_subcategory(
                request.form.get('new_sub_category_id', type=int),
                request.form.get('new_subcategory_name'),
                notes=request.form.get('new_subcategory_notes'),
                actor=current_user,
            )
            db.session.commit()
            flash('Sub-category saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow add subcategory failed')
            flash(f'Unable to save sub-category: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action == 'add_party':
        try:
            save_cf_party(
                request.form.get('new_party_name'),
                request.form.get('new_party_type') or 'person',
                note=request.form.get('new_party_note'),
                actor=current_user,
            )
            db.session.commit()
            flash('Party saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow add party failed')
            flash(f'Unable to save party: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action == 'disable_category':
        try:
            disable_cf_category(request.form.get('category_id', type=int))
            db.session.commit()
            flash('Category disabled. Historical rows keep the name.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'disable_subcategory':
        try:
            disable_cf_subcategory(request.form.get('subcategory_id', type=int))
            db.session.commit()
            flash('Sub-category disabled. Historical rows keep the name.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'enable_category':
        try:
            enable_cf_category(request.form.get('category_id', type=int))
            db.session.commit()
            flash('Category enabled.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'enable_subcategory':
        try:
            enable_cf_subcategory(request.form.get('subcategory_id', type=int))
            db.session.commit()
            flash('Sub-category enabled.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'update_party':
        try:
            update_cf_party(
                request.form.get('party_id', type=int),
                request.form.get('party_name'),
                party_type=request.form.get('party_type'),
                note=request.form.get('party_note'),
            )
            db.session.commit()
            flash('Party updated.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'disable_party':
        try:
            disable_cf_party(request.form.get('party_id', type=int))
            db.session.commit()
            flash('Party disabled. Historical rows keep the name.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'enable_party':
        try:
            enable_cf_party(request.form.get('party_id', type=int))
            db.session.commit()
            flash('Party enabled.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'rename_category':
        try:
            rename_cf_category(
                request.form.get('category_id', type=int),
                request.form.get('category_name'),
                direction=request.form.get('category_direction'),
                notes=request.form.get('category_notes'),
            )
            db.session.commit()
            flash('Category updated. Historical rows stay linked to the same category.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'rename_subcategory':
        try:
            rename_cf_subcategory(
                request.form.get('subcategory_id', type=int),
                request.form.get('subcategory_name'),
                notes=request.form.get('subcategory_notes'),
            )
            db.session.commit()
            flash('Sub-category updated.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'delete_category':
        try:
            delete_cf_category(request.form.get('category_id', type=int))
            db.session.commit()
            flash('Unused category deleted.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow delete category failed')
            flash(f'Unable to delete category: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action == 'delete_subcategory':
        try:
            delete_cf_subcategory(request.form.get('subcategory_id', type=int))
            db.session.commit()
            flash('Unused sub-category deleted.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow delete subcategory failed')
            flash(f'Unable to delete sub-category: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action == 'delete_party':
        try:
            delete_cf_party(request.form.get('party_id', type=int))
            db.session.commit()
            flash('Unused party deleted.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow delete party failed')
            flash(f'Unable to delete party: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action in ('delete_entry', 'void_entry'):
        entry_id = request.form.get('entry_id', type=int)
        entry = CashFlowEntry.query.get(entry_id) if entry_id else None
        try:
            void_manual_cash_flow_entry(
                entry, reason=request.form.get('void_reason'), actor=current_user,
            )
            db.session.commit()
            audit_log(current_user, 'cash_flow.entry.void', f'id={entry_id}')
            flash('Transaction voided. The financial effect was reversed and the history was kept.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow void failed')
            flash(f'Unable to void transaction: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action == 'restore_entry':
        entry_id = request.form.get('entry_id', type=int)
        entry = CashFlowEntry.query.get(entry_id) if entry_id else None
        try:
            restore_manual_cash_flow_entry(
                entry, reason=request.form.get('restore_reason'), actor=current_user,
            )
            db.session.commit()
            audit_log(current_user, 'cash_flow.entry.restore', f'id={entry_id}')
            flash('Transaction restored and the account effect was re-applied.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow restore failed')
            flash(f'Unable to restore transaction: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action in ('record_movement', 'edit_entry'):
        entry_id = request.form.get('entry_id', type=int) if action == 'edit_entry' else None
        payload = dict(
            direction=request.form.get('direction'),
            amount=request.form.get('amount', 0),
            account_id=request.form.get('cash_account_id', type=int) or request.form.get('from_account_id', type=int),
            destination_account_id=request.form.get('to_account_id', type=int),
            category_id=request.form.get('category_id', type=int),
            category_name=request.form.get('category_name'),
            subcategory_id=request.form.get('subcategory_id', type=int),
            subcategory_name=request.form.get('subcategory_name'),
            party_id=request.form.get('party_id', type=int),
            party_name=request.form.get('party_name'),
            party_type=request.form.get('party_type') or 'other',
            description=request.form.get('description'),
            note=request.form.get('movement_note') or request.form.get('note'),
            reference=request.form.get('reference'),
            actor=current_user,
            create_missing=True,
        )
        date_raw = (request.form.get('movement_date') or '').strip()
        payload['date_posted'] = resolve_posted_datetime(date_raw or None)
        try:
            if action == 'edit_entry':
                entry = CashFlowEntry.query.get(entry_id) if entry_id else None
                update_manual_cash_flow_entry(
                    entry, reason=request.form.get('edit_reason'), **payload,
                )
                db.session.commit()
                audit_log(current_user, 'cash_flow.entry.edit', f'id={entry_id}')
                flash('Cash flow transaction updated.', 'success')
            else:
                payload['idempotency_key'] = (request.form.get('idempotency_key') or '').strip() or None
                entry, created = save_manual_cash_flow_entry(**payload)
                db.session.commit()
                if created:
                    audit_log(current_user, 'cash_flow.record', f'dir={entry.direction}, amount={entry.amount}')
                    flash(f'{_cf_type_label(entry.direction)} Rs. {float(entry.amount or 0):,.0f} recorded.', 'success')
                else:
                    flash('This transaction was already saved.', 'info')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Cash flow save failed')
            flash(f'Unable to save cash flow transaction: {exc}', 'danger')
        return _back()

    if request.method == 'POST' and action in ('set_opening_override', 'clear_opening_override', 'reset_fresh_start'):
        if action == 'reset_fresh_start':
            session['cash_flow_fresh_start_cutoff'] = {
                'date': fresh_start_date,
                'at': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
                'user_set': True,
            }
            session.pop('cash_flow_today_opening_override', None)
            flash('Cash Flow fresh-start view reset. Existing entries are hidden from this report and today opening is Rs. 0.', 'success')
            return redirect(url_for('cash_flow', from_date=fresh_start_date, to_date=fresh_start_date, filter_type='all'))
        if action == 'clear_opening_override':
            session.pop('cash_flow_today_opening_override', None)
            flash('Today cash flow opening override cleared. Opening is back to the automatic carry-forward.', 'success')
        else:
            opening_override_amount = _money_round(request.form.get('today_opening_override', 0))
            session['cash_flow_today_opening_override'] = {
                'date': fresh_start_date,
                'amount': opening_override_amount,
            }
            flash(f'Today cash flow opening override set to Rs. {opening_override_amount:,.0f}. Source accounts were not changed.', 'success')
        return redirect(url_for('cash_flow', from_date=fresh_start_date, to_date=fresh_start_date, filter_type='all'))

    try:
        from_date_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
    except Exception:
        from_date_dt = fresh_start_dt
        from_date = fresh_start_date
    try:
        to_date_dt = datetime.strptime(to_date, '%Y-%m-%d').date()
    except Exception:
        to_date_dt = fresh_start_dt
        to_date = fresh_start_date

    fresh_start_clamped = False
    if to_date_dt < from_date_dt:
        to_date_dt = from_date_dt
        to_date = from_date_dt.strftime('%Y-%m-%d')

    today_opening_override = _cash_flow_today_opening_override(fresh_start_date)
    fresh_start_cutoff = _cash_flow_fresh_start_cutoff(fresh_start_date)
    # "Fresh start" (hide existing entries, opening Rs. 0) is opt-in only:
    # it activates when the user explicitly clicks the reset button today.
    # By default, TODAY behaves like any other date — automatic opening
    # carry-forward and all entries visible.
    _cutoff_info = session.get('cash_flow_fresh_start_cutoff') or {}
    hide_existing_today_entries = (
        from_date_dt == fresh_start_dt and bool(_cutoff_info.get('user_set'))
    )
    posted_after = fresh_start_cutoff if hide_existing_today_entries else None

    if opening_balance_input:
        try:
            opening_balance = float(opening_balance_input)
        except ValueError:
            opening_balance = 0.0
    elif from_date_dt == fresh_start_dt and today_opening_override is not None:
        opening_balance = today_opening_override
    elif hide_existing_today_entries:
        opening_balance = 0.0
    else:
        opening_balance = _automatic_cash_opening_balance(from_date_dt)

    filter_account_id = source.get('account_id', type=int)
    filter_status = (filter_kwargs.get('status') or 'active').strip().lower()
    all_rows = collect_cash_flow_rows(
        from_date, to_date,
        posted_after=posted_after,
        include_voided=(filter_status == 'voided'),
    )
    all_rows.sort(key=_cf_sort_key)

    display_rows = filter_cash_flow_rows(all_rows, {
        **filter_kwargs,
        'account_id': filter_account_id,
        'filter_type': filter_type,
        'status': filter_status,
    })
    if export_scope == 'all' and (export_csv == '1' or export_pdf == '1'):
        display_rows = list(all_rows)
        if filter_status == 'voided':
            display_rows = [r for r in display_rows if (r.get('status') or 'active') == 'voided']
        else:
            display_rows = [r for r in display_rows if (r.get('status') or 'active') == 'active']

    closing_balance = apply_running_balance(display_rows, opening_balance, account_id=filter_account_id)
    summary = summarize_cash_flow_rows(display_rows, account_id=filter_account_id)
    total_cash_in = summary['total_cash_in']
    total_cash_out = summary['total_cash_out']

    # Reconciliation must always compare physical cash against the FULL
    # (unfiltered, company-wide) closing for the period — never against a
    # closing narrowed by type/category/party/account display filters.
    active_all_rows = [r for r in all_rows if (r.get('status') or 'active') == 'active']
    reconciliation_closing = apply_running_balance(
        [dict(r) for r in active_all_rows], opening_balance
    )

    try:
        adjustment_date_dt = datetime.strptime(adjustment_date_input, '%Y-%m-%d').date()
    except Exception:
        adjustment_date_dt = datetime.strptime(to_date, '%Y-%m-%d').date()

    adjustment_entry = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=adjustment_date_dt).first()
    if request.method == 'POST' and action == 'delete':
        if adjustment_entry:
            audit = CashFlowReconciliationAudit(
                reconciliation_id=adjustment_entry.id,
                adjustment_date=adjustment_entry.adjustment_date,
                change_type='DELETE',
                old_physical_cash=adjustment_entry.physical_cash_available,
                old_difference=adjustment_entry.difference if adjustment_entry.difference is not None else adjustment_entry.amount,
                old_reason=adjustment_entry.reason or adjustment_entry.note,
                changed_by=_current_username(),
                changed_at=pk_model_now(),
            )
            db.session.add(audit)
            adjustment_entry.physical_cash_available = None
            adjustment_entry.calculated_closing = None
            adjustment_entry.difference = None
            adjustment_entry.reason = None
            adjustment_entry.amount = 0
            adjustment_entry.note = 'Reconciliation removed; audit trail retained.'
            adjustment_entry.edited_by = _current_username()
            adjustment_entry.edited_date = pk_model_now()
            adjustment_entry.edit_count = (adjustment_entry.edit_count or 0) + 1
            db.session.commit()
            flash(f'Reconciliation removed for {adjustment_date_dt.strftime("%Y-%m-%d")}. Audit trail retained.', 'success')
        return _back()

    if request.method == 'POST' and action == 'save_reconciliation':
        try:
            physical_cash_available = parse_physical_cash_amount(physical_cash_input)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return _back()
        difference = compute_physical_cash_difference(physical_cash_available, reconciliation_closing)
        username = _current_username()
        if not adjustment_entry:
            adjustment_entry = CashFlowDifferenceAdjustment(
                adjustment_date=adjustment_date_dt,
                created_by=username,
                created_at=pk_model_now(),
                edit_count=0,
            )
            db.session.add(adjustment_entry)
            db.session.flush()
            change_type = 'CREATE'
            old_physical_cash = None
            old_difference = None
            old_reason = None
        else:
            change_type = 'EDIT' if adjustment_entry.physical_cash_available is not None else 'CREATE'
            old_physical_cash = adjustment_entry.physical_cash_available
            old_difference = adjustment_entry.difference if adjustment_entry.difference is not None else adjustment_entry.amount
            old_reason = adjustment_entry.reason or adjustment_entry.note
            adjustment_entry.old_physical_cash = old_physical_cash
            adjustment_entry.edited_by = username
            adjustment_entry.edited_date = pk_model_now()

        adjustment_entry.physical_cash_available = physical_cash_available
        adjustment_entry.calculated_closing = _money_round(reconciliation_closing)
        adjustment_entry.difference = difference
        adjustment_entry.amount = difference
        adjustment_entry.reason = reconciliation_reason
        adjustment_entry.note = reconciliation_reason
        adjustment_entry.edit_count = (adjustment_entry.edit_count or 0) + 1
        db.session.add(CashFlowReconciliationAudit(
            reconciliation_id=adjustment_entry.id,
            adjustment_date=adjustment_date_dt,
            change_type=change_type,
            old_physical_cash=old_physical_cash,
            new_physical_cash=physical_cash_available,
            old_difference=old_difference,
            new_difference=difference,
            old_reason=old_reason,
            new_reason=reconciliation_reason,
            changed_by=username,
            changed_at=pk_model_now(),
        ))
        db.session.commit()
        flash(
            f'Reconciliation saved for {adjustment_date_dt.strftime("%Y-%m-%d")}. '
            f'Next day opening will be Rs. {physical_cash_available:,.0f}. Account balances were not changed.',
            'success',
        )
        return _back()

    adjustment_entry = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=adjustment_date_dt).first()
    physical_cash_available = adjustment_entry.physical_cash_available if adjustment_entry and adjustment_entry.physical_cash_available is not None else None
    adjustment_amount = float((adjustment_entry.difference if adjustment_entry and adjustment_entry.difference is not None else adjustment_entry.amount) or 0) if adjustment_entry else 0.0
    reconciliation_reason = (adjustment_entry.reason or adjustment_entry.note or '') if adjustment_entry else ''
    adjusted_closing_balance = physical_cash_available if physical_cash_available is not None else reconciliation_closing

    if export_csv == '1':
        buf = io.StringIO()
        import csv
        writer = csv.writer(buf)
        writer.writerow([
            'Date', 'Type', 'Account', 'Section', 'Category', 'Subcategory', 'Party',
            'Amount', 'Received', 'Spent', 'Transfer', 'Description', 'Notes',
            'Reference', 'Source', 'Created By', 'Status',
        ])
        for r in display_rows:
            writer.writerow([
                r.get('date'), r.get('tx_type_label'), r.get('account_display'),
                r.get('section_label'),
                r.get('category'), r.get('subcategory'), r.get('party_name'),
                r.get('amount'), r.get('cash_in') or '', r.get('cash_out') or '',
                r.get('transfer_amount') or '', r.get('description'), r.get('note'),
                r.get('reference'), r.get('origin_label'), r.get('created_by'),
                r.get('status'),
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename=cash_flow_{from_date}_{to_date}.csv'
        return resp

    cash_accounts = _cf_company_accounts(active_only=True)
    cash_accounts_list = [a for a in cash_accounts if (a.category or 'cash').lower() == 'cash']
    bank_accounts_list = [a for a in cash_accounts if (a.category or '').lower() == 'bank']
    cash_total = sum(_money_round(a.balance or 0) for a in cash_accounts_list)
    bank_total = sum(_money_round(a.balance or 0) for a in bank_accounts_list)

    # Per-account activity within the currently filtered rows (for cash/bank cards).
    account_activity = {}
    for r in display_rows:
        if (r.get('status') or 'active') != 'active':
            continue
        rtype = r.get('type')
        aid = r.get('account_id')
        if aid:
            act = account_activity.setdefault(aid, {'in': 0.0, 'out': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0})
            if rtype == CF_DIR_IN:
                act['in'] += float(r.get('cash_in') or 0)
            elif rtype == CF_DIR_OUT:
                act['out'] += float(r.get('cash_out') or 0)
            elif rtype == CF_DIR_TRANSFER:
                act['transfer_out'] += float(r.get('transfer_amount') or 0)
        aid2 = r.get('account_to_id')
        if aid2 and rtype == CF_DIR_TRANSFER:
            act2 = account_activity.setdefault(aid2, {'in': 0.0, 'out': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0})
            act2['transfer_in'] += float(r.get('transfer_amount') or 0)

    cf_categories = CashFlowCategory.query.order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all()
    cf_subcategories = CashFlowSubcategory.query.order_by(CashFlowSubcategory.name).all()
    cf_parties = CashFlowParty.query.filter_by(is_active=True).order_by(CashFlowParty.name).all()
    cf_parties_all = CashFlowParty.query.order_by(CashFlowParty.name).all()
    created_by_options = sorted({
        (r.get('created_by') or '').strip()
        for r in all_rows
        if (r.get('created_by') or '').strip()
    })
    account_options = [{'id': a.id, 'label': _cf_account_label(a)} for a in cash_accounts]
    source_options = [('all', 'All'), ('manual', 'Manual'), ('system', 'System-generated')]
    for key, label in CF_SOURCE_LABELS.items():
        if key == 'MANUAL_CASH_FLOW':
            continue
        source_options.append((key.lower(), label))

    today_str = fresh_start_date
    yesterday_str = (fresh_start_dt - timedelta(days=1)).strftime('%Y-%m-%d')
    this_week_str = (fresh_start_dt - timedelta(days=fresh_start_dt.weekday())).strftime('%Y-%m-%d')
    this_month_str = fresh_start_dt.replace(day=1).strftime('%Y-%m-%d')
    last_30_days_str = (fresh_start_dt - timedelta(days=30)).strftime('%Y-%m-%d')

    recent_reconciliations = (
        CashFlowDifferenceAdjustment.query
        .order_by(CashFlowDifferenceAdjustment.adjustment_date.desc())
        .limit(10)
        .all()
    )

    common = dict(
        rows=display_rows,
        cash_accounts=cash_accounts,
        cash_accounts_list=cash_accounts_list,
        bank_accounts_list=bank_accounts_list,
        cash_total=cash_total,
        bank_total=bank_total,
        account_activity=account_activity,
        account_options=account_options,
        cf_categories=cf_categories,
        cf_subcategories=cf_subcategories,
        cf_parties=cf_parties,
        cf_parties_all=cf_parties_all,
        used_category_ids=cf_used_category_ids(),
        used_subcategory_ids=cf_used_subcategory_ids(),
        used_party_ids=cf_used_party_ids(),
        default_movement_datetime=pk_now().strftime('%Y-%m-%dT%H:%M'),
        party_types=CF_PARTY_TYPES,
        source_options=source_options,
        created_by_options=created_by_options,
        today_str=today_str,
        yesterday_str=yesterday_str,
        this_week_str=this_week_str,
        this_month_str=this_month_str,
        last_30_days_str=last_30_days_str,
        breakdown_cat=summary['breakdown_cat'],
        breakdown_party=summary['breakdown_party'],
        breakdown_account=summary['breakdown_account'],
        filter_origin=filter_kwargs['origin'],
        filter_category=filter_kwargs['category'],
        filter_subcategory=filter_kwargs['subcategory'],
        filter_party_type=filter_kwargs['party_type'],
        filter_party=filter_kwargs['party'],
        filter_account_id=filter_account_id,
        filter_q=filter_kwargs['q'],
        filter_notes=filter_kwargs['notes'],
        filter_reference=filter_kwargs['reference'],
        filter_description=filter_kwargs['description'],
        filter_created_by=filter_kwargs['created_by'],
        filter_status=filter_status,
        filter_amount_min=filter_kwargs['amount_min'],
        filter_amount_max=filter_kwargs['amount_max'],
        from_date=from_date, to_date=to_date,
        filter_type=filter_type,
        opening_balance=opening_balance,
        opening_balance_input=opening_balance_input,
        adjustment_amount=adjustment_amount,
        physical_cash_available=physical_cash_available,
        reconciliation_reason=reconciliation_reason,
        recent_reconciliations=recent_reconciliations,
        show_delete_button=bool(adjustment_entry and adjustment_entry.physical_cash_available is not None),
        adjusted_closing_balance=adjusted_closing_balance,
        adjustment_date_input=adjustment_date_input,
        closing_balance=closing_balance,
        reconciliation_closing=reconciliation_closing,
        total_cash_in=total_cash_in,
        total_cash_out=total_cash_out,
        total_transfer_in=summary['total_transfer_in'],
        total_transfer_out=summary['total_transfer_out'],
        generated_at=pk_now().strftime('%Y-%m-%d %H:%M'),
        settings=None,
        fresh_start_date=fresh_start_date,
        fresh_start_cutoff=fresh_start_cutoff,
        fresh_start_clamped=fresh_start_clamped,
        today_opening_override=today_opening_override,
        is_fresh_start_view=hide_existing_today_entries,
    )

    if export_pdf == '1':
        rendered = render_template('cash_flow.html', pdf_mode=True, **common)
        pdf_resp = _try_render_weasy_pdf(
            rendered, f'cash_flow_{from_date}_{to_date}.pdf',
            lang=resolve_export_lang(), urdu_labels=True,
            doc_title=f'Cash Flow {from_date} to {to_date}',
        )
        if pdf_resp:
            return pdf_resp
        return Response(rendered, content_type='text/html')

    # Embedded reference layouts (references-images/):
    #   - "Transactions" tab   -> Financial Tracking Filter Matrix (layout #2)
    #   - "Daily Reconciliations" tab -> Daily Cash & Bank Reconciliation (layout #1)
    recon_day_arg = (request.args.get('recon_day') or '').strip()
    dr_ctx = _dr_build_context(recon_day_arg, embed=True)
    ft_ctx = _ft_build_context(request.args, prefix='ft_', embed=True, posted_after=posted_after)
    initial_tab = ''
    if recon_day_arg:
        initial_tab = 'reconcile'
    elif any(k.startswith('ft_') for k in request.args):
        initial_tab = 'transactions'

    return render_template(
        'cash_flow.html', pdf_mode=False,
        dr=dr_ctx, ft=ft_ctx, initial_tab=initial_tab,
        **common,
    )


@bp.route('/cash_flow_differences')
@login_required
def cash_flow_differences():
    from_date = request.args.get('from_date', '').strip()
    to_date = request.args.get('to_date', '').strip()
    workflow_filter = request.args.get('workflow_filter', 'all').strip()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = 25

    query = CashFlowDifferenceAdjustment.query
    if from_date:
        try:
            query = query.filter(CashFlowDifferenceAdjustment.adjustment_date >= datetime.strptime(from_date, '%Y-%m-%d').date())
        except Exception:
            from_date = ''
    if to_date:
        try:
            query = query.filter(CashFlowDifferenceAdjustment.adjustment_date <= datetime.strptime(to_date, '%Y-%m-%d').date())
        except Exception:
            to_date = ''
    if workflow_filter == 'new':
        query = query.filter(CashFlowDifferenceAdjustment.physical_cash_available.isnot(None))
    elif workflow_filter == 'legacy':
        query = query.filter(CashFlowDifferenceAdjustment.physical_cash_available.is_(None))
    else:
        workflow_filter = 'all'

    total_count = CashFlowDifferenceAdjustment.query.count()
    new_workflow_count = CashFlowDifferenceAdjustment.query.filter(CashFlowDifferenceAdjustment.physical_cash_available.isnot(None)).count()
    legacy_count = CashFlowDifferenceAdjustment.query.filter(CashFlowDifferenceAdjustment.physical_cash_available.is_(None)).count()
    total_audit_events = CashFlowReconciliationAudit.query.count()

    filtered_count = query.count()
    pages = max(1, (filtered_count + per_page - 1) // per_page)
    page = min(page, pages)
    reconciliations = query.order_by(
        CashFlowDifferenceAdjustment.adjustment_date.desc(),
        CashFlowDifferenceAdjustment.id.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()
    for rec in reconciliations:
        rec.display_opening_balance = _automatic_cash_opening_balance(rec.adjustment_date)
        rec.display_cash_in, rec.display_cash_out = _cash_flow_in_out_between(rec.adjustment_date, rec.adjustment_date)
        if rec.calculated_closing is None:
            rec.display_calculated_closing = rec.display_opening_balance + rec.display_cash_in - rec.display_cash_out
        else:
            rec.display_calculated_closing = rec.calculated_closing

    return render_template(
        'cash_flow_differences.html',
        reconciliations=reconciliations,
        total_count=total_count,
        new_workflow_count=new_workflow_count,
        legacy_count=legacy_count,
        total_audit_events=total_audit_events,
        from_date=from_date,
        to_date=to_date,
        workflow_filter=workflow_filter,
        page=page,
        pages=pages,
    )


def _dr_build_context(day_value, embed=False):
    """Build the Daily Cash & Bank Reconciliation board context (layout #1).

    Shared by the standalone /daily_reconciliation page and the embedded
    "Daily Reconciliations" tab on /cash_flow.
    """
    from app.services import cash_day_recon as recon

    today_str = pk_today().strftime('%Y-%m-%d')
    day_str = (day_value or today_str).strip() or today_str
    try:
        datetime.strptime(day_str, '%Y-%m-%d')
    except ValueError:
        day_str = today_str

    positions = recon.account_positions_for_date(day_str)
    day_lock = recon.get_day_lock(day_str)
    shift_closes = recon.shift_closes_for_date(day_str)
    total_expected = _money_round(sum(p['expected_closing'] for p in positions))
    counted_positions = [p for p in positions if p['effective_counted'] is not None]
    counted_values = [p['effective_counted'] for p in counted_positions]
    total_counted = _money_round(sum(counted_values)) if counted_values else None
    # The difference may only compare like with like: counted balances against
    # the expected closing of the *same* accounts.  Comparing a single counted
    # account against the expected total of every account produced a huge
    # meaningless negative number, which made the whole difference mechanism
    # look broken.
    expected_counted = _money_round(
        sum(p['expected_closing'] for p in counted_positions)
    ) if counted_positions else None
    net_difference = (
        _money_round(total_counted - expected_counted)
        if total_counted is not None else None
    )
    prev_str = (datetime.strptime(day_str, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
    next_str = (datetime.strptime(day_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    from app.services import recon_counter

    posting_by_account = recon_counter.difference_summary(day_str)
    for position in positions:
        position['posting'] = posting_by_account.get(position['account_id'])

    return dict(
        day_str=day_str,
        prev_str=prev_str,
        next_str=next_str,
        counter_less=recon_counter.counter_accounts()[0],
        counter_excess=recon_counter.counter_accounts()[1],
        counter_total=recon_counter.counter_balance_total(),
        posting=_dr_posting_summary(day_str),
        positions=positions,
        day_lock=day_lock,
        shift_closes=shift_closes,
        latest_shift_close=shift_closes[0] if shift_closes else None,
        total_expected=total_expected,
        total_counted=total_counted,
        expected_counted=expected_counted,
        net_difference=net_difference,
        counted_accounts=len(counted_positions),
        accounts_total=len(positions),
        embed=embed,
    )


def _dr_posting_summary(day_str):
    """What the finalised day's differences did to the counter accounts.

    Plain primitives only — this dict is also handed to ``jsonify`` for the
    board's AJAX save, so it must never carry ORM objects.
    """
    from app.services import recon_counter as rc
    from models import CashDayDifference

    # Everything the finalised day sent to the counters, resolved or not: the
    # day's story must not shrink as differences are explained away.  Only a
    # reopened (reversed) posting leaves this set.
    rows = (
        CashDayDifference.query
        .filter(
            CashDayDifference.position_date == rc.day_of(day_str),
            CashDayDifference.posted_status.in_([rc.STATUS_POSTED, rc.STATUS_RESOLVED]),
        )
        .order_by(CashDayDifference.id.asc())
        .all()
    )
    less_total = _money_round(sum(float(r.difference or 0) for r in rows if (r.difference or 0) < 0))
    excess_total = _money_round(sum(float(r.difference or 0) for r in rows if (r.difference or 0) > 0))
    unresolved = [r for r in rows if r.posted_status == rc.STATUS_POSTED]
    return {
        'count': len(rows),
        'open_count': len(unresolved),
        'less_total': less_total,
        'excess_total': excess_total,
        'net': _money_round(less_total + excess_total),
        'posted_of_day': _money_round(sum(abs(float(r.difference or 0)) for r in rows)),
        'rows': [
            {
                'id': r.id, 'account_id': r.account_id, 'account_name': r.account_name,
                'kind': r.kind, 'difference': _money_round(r.difference or 0),
                'posted_status': r.posted_status,
                'posted_out_tx_id': r.posted_out_tx_id, 'posted_in_tx_id': r.posted_in_tx_id,
            }
            for r in rows
        ],
    }


def _safe_return_to():
    """Relative-only return_to target posted by embedded reconciliation forms."""
    rt = (request.form.get('return_to') or '').strip()
    if rt.startswith('/') and not rt.startswith('//'):
        return rt
    return None


def _dr_wants_json():
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.accept_mimetypes.best == 'application/json'
    )


def _dr_json_context(day_str):
    ctx = _dr_build_context(day_str)
    counter_less = ctx.get('counter_less')
    counter_excess = ctx.get('counter_excess')
    return {
        'day_str': ctx['day_str'],
        'total_expected': ctx['total_expected'],
        'total_counted': ctx['total_counted'],
        'expected_counted': ctx['expected_counted'],
        'net_difference': ctx['net_difference'],
        'counted_accounts': ctx['counted_accounts'],
        'accounts_total': ctx['accounts_total'],
        'counter_total': ctx.get('counter_total', 0),
        'counter_less_name': counter_less.name if counter_less else 'Counter Less Account',
        'counter_excess_name': counter_excess.name if counter_excess else 'Counter Excess Account',
        'posting': ctx.get('posting'),
        'positions': ctx['positions'],
    }


@bp.route('/daily_reconciliation', methods=['GET', 'POST'])
@login_required
def daily_reconciliation():
    """Daily Cash & Bank Reconciliation board (reference layout #1)."""
    from app.services import cash_day_recon as recon

    today_str = pk_today().strftime('%Y-%m-%d')
    day_str = (request.form.get('day') or request.args.get('day') or today_str).strip() or today_str
    try:
        datetime.strptime(day_str, '%Y-%m-%d')
    except ValueError:
        day_str = today_str

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        counted_raw = (request.form.get('counted') or '').strip()
        # fetch(FormData) omits the submit button. Treat a counted amount +
        # account as save_count so Enter / unfocused Save still persist.
        if not action and counted_raw and request.form.get('account_id'):
            action = 'save_count'
        wants_json = _dr_wants_json()
        ok = True
        message = ''
        status = 'success'
        try:
            if action == 'save_count':
                pos = recon.save_count(
                    day_str,
                    request.form.get('account_id'),
                    counted_raw,
                    actor=current_user,
                )
                posted_messages = getattr(pos, 'posting_messages', None) or []
                message = (
                    'Counted balance saved. ' + ' '.join(posted_messages)
                    if posted_messages else 'Counted balance saved.'
                )
                status = 'success'
            elif action == 'refresh_differences':
                checked = recon.refresh_differences(day_str, actor=current_user)
                if checked:
                    message = ('Saved counts checked against current expected balances; '
                               'Accounts and differences report updated.')
                else:
                    message = (f'Day {day_str} is finalised, so its differences are fixed. '
                               'Unlock the day to correct them.')
                    status = 'warning'
            elif action == 'close_shift':
                shift_close = recon.close_shift(
                    day_str,
                    request.form.get('shift_name'),
                    actor=current_user,
                    note=request.form.get('shift_note') or '',
                )
                message = (
                    f'Shift “{shift_close.shift_name}” closed and recorded. '
                    'The day remains open for the next shift.'
                )
                status = 'success'
            elif action == 'lock':
                lock = recon.lock_day(day_str, actor=current_user, note=request.form.get('note') or '')
                posted_messages = getattr(lock, 'posting_messages', None) or []
                message = (
                    f'Day {day_str} finalized and locked. '
                    + (' '.join(posted_messages) + ' ' if posted_messages else '')
                    + 'Locked totals become the next day opening.'
                )
                status = 'success'
            elif action == 'unlock':
                removed = recon.unlock_day(
                    day_str, actor=current_user,
                    reason=request.form.get('note') or '',
                )
                message = (
                    f'Day {day_str} unlocked for editing. Edit or clear counts below to update Accounts.'
                    + (f' {removed} counter-account posting(s) reversed.' if removed else '')
                )
                status = 'warning'
            elif action == 'clear_count':
                recon.delete_count(day_str, request.form.get('account_id'), actor=current_user)
                message = 'Counted balance cleared and any posted difference reversed.'
                status = 'warning'
            else:
                raise ValueError('Unsupported reconciliation action. Reload the page and try again.')
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            ok = False
            # Surface the real reason ("Counted amount is required", "Day is
            # locked", ...) instead of a generic message the user cannot act on.
            message = str(exc) or (
                'Enter a valid counted amount.' if action == 'save_count'
                else 'Unable to update reconciliation.'
            )
            status = 'danger'
        except Exception:
            db.session.rollback()
            logger.exception('Daily reconciliation save failed')
            ok = False
            message = 'Unable to save reconciliation. Reload the page and try again.'
            status = 'danger'
        if wants_json:
            payload = _dr_json_context(day_str)
            payload['posting'] = _dr_posting_summary(day_str)
            payload.update({'ok': ok, 'message': message, 'error': None if ok else message, 'status': status})
            return jsonify(payload), (200 if ok else 400)
        flash(message, status)
        rt = _safe_return_to()
        if rt:
            return redirect(rt)
        return redirect(url_for('daily_reconciliation', day=day_str))

    return render_template(
        'daily_reconciliation.html',
        dr=_dr_build_context(day_str),
    )


@bp.route('/daily_reconciliation/differences/<int:difference_id>/resolve', methods=['POST'])
@login_required
def daily_reconciliation_resolve_difference(difference_id):
    """Resolve one posted difference (its own URL, so the report stays readable)."""
    from models import CashDayDifference

    difference = db.session.get(CashDayDifference, difference_id)
    back = url_for('daily_reconciliation_differences')
    if difference is None:
        flash('Difference not found.', 'danger')
        return redirect(back)
    return _dr_resolve_difference(actor=current_user, difference=difference)


@bp.route('/daily_reconciliation/differences', methods=['GET', 'POST'])
@login_required
def daily_reconciliation_differences():
    """Reporting-only discrepancy snapshots, with independent card pagination.

    Filters (all optional, combined with AND so the report can be narrowed
    down by hand): a date range — typed in or via ``preset`` — Less/Excess
    (``kind``), ``status``, ``account_id``, ``recorded_by``, ``source``, an
    absolute difference range (``amount_min``/``amount_max``), free text
    (``q``) and ``sort``.  Totals and the per-account summary always describe
    the filtered set, not just the visible page.  ``export_csv=1`` downloads
    every filtered row.  Nothing here writes to the difference history.
    """
    from models import CashDayDifference
    from app.services import recon_counter

    if request.method == 'POST' and (request.form.get('action') or '') == 'resolve':
        return _dr_resolve_difference(actor=current_user)

    if request.method == 'POST':
        from app.services import cash_day_recon as recon
        try:
            count = recon.import_saved_differences(actor=current_user)
            flash(f'Imported {count} current saved-count difference(s). Earlier edits cannot be reconstructed.', 'success')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Importing saved daily differences failed')
            flash(f'Unable to import saved differences: {exc or "please try again."}', 'danger')
        return redirect(url_for('daily_reconciliation_differences'))

    args = request.args
    today = pk_today()

    # --- Date range: an explicit preset wins over typed dates so a
    # bookmarked ?preset=last_30 stays relative; the page switches the
    # preset back to "custom" as soon as a date is edited by hand.
    preset = (args.get('preset') or '').strip().lower()
    if preset == 'today':
        from_date = to_date = today.isoformat()
    elif preset == 'yesterday':
        from_date = to_date = (today - timedelta(days=1)).isoformat()
    elif preset == 'last_7':
        from_date, to_date = (today - timedelta(days=6)).isoformat(), today.isoformat()
    elif preset == 'last_30':
        from_date, to_date = (today - timedelta(days=29)).isoformat(), today.isoformat()
    elif preset == 'this_month':
        from_date, to_date = today.replace(day=1).isoformat(), today.isoformat()
    elif preset == 'all':
        from_date = to_date = ''
    else:
        preset = 'custom'
        from_date = (args.get('from_date') or '').strip()
        to_date = (args.get('to_date') or '').strip()
    for value in (from_date, to_date):
        if value:
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                abort(400, 'Invalid date filter.')
    if from_date and to_date and from_date > to_date:
        abort(400, 'From date must not be after to date.')

    # --- Less / Excess and the other narrowing filters.  Unknown dropdown
    # values fall back to "all" instead of erroring; dates and amounts keep
    # the strict behaviour this page has always had.
    f_kind = (args.get('kind') or 'all').strip().lower()
    if f_kind not in ('less', 'excess'):
        f_kind = 'all'
    f_status = (args.get('status') or 'all').strip().lower()
    if f_status not in ('open', 'settled', 'superseded', 'cleared'):
        f_status = 'all'
    f_source = (args.get('source') or 'all').strip().lower()
    if f_source not in ('live', 'imported'):
        f_source = 'all'
    f_account_raw = (args.get('account_id') or '').strip()
    f_account_id = None
    if f_account_raw:
        try:
            f_account_id = int(f_account_raw)
        except ValueError:
            abort(400, 'Invalid account filter.')
    f_recorded_by = (args.get('recorded_by') or '').strip()
    f_q = (args.get('q') or '').strip()

    def _amount_arg(name):
        raw = (args.get(name) or '').strip()
        if not raw:
            return None, ''
        try:
            value = float(raw.replace(',', '').replace(' ', ''))
        except ValueError:
            abort(400, 'Invalid amount filter.')
        if value < 0:
            abort(400, 'Amount filters must not be negative.')
        return value, raw

    amount_min, amount_min_raw = _amount_arg('amount_min')
    amount_max, amount_max_raw = _amount_arg('amount_max')
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        abort(400, 'Minimum amount must not be greater than maximum amount.')

    sort_orders = {
        'newest': (CashDayDifference.recorded_at.desc(), CashDayDifference.id.desc()),
        'oldest': (CashDayDifference.recorded_at.asc(), CashDayDifference.id.asc()),
        'day_newest': (CashDayDifference.position_date.desc(),
                       CashDayDifference.recorded_at.desc(), CashDayDifference.id.desc()),
        'day_oldest': (CashDayDifference.position_date.asc(),
                       CashDayDifference.recorded_at.asc(), CashDayDifference.id.asc()),
        'largest': (func.abs(CashDayDifference.difference).desc(),
                    CashDayDifference.recorded_at.desc(), CashDayDifference.id.desc()),
        'smallest': (func.abs(CashDayDifference.difference).asc(),
                     CashDayDifference.recorded_at.desc(), CashDayDifference.id.desc()),
    }
    sort_labels = {
        'newest': 'newest recorded first', 'oldest': 'oldest recorded first',
        'day_newest': 'newest day first', 'day_oldest': 'oldest day first',
        'largest': 'largest difference first', 'smallest': 'smallest difference first',
    }
    f_sort = (args.get('sort') or 'newest').strip().lower()
    if f_sort not in sort_orders:
        f_sort = 'newest'

    def filtered(kind=None):
        q = CashDayDifference.query
        wanted = kind or (f_kind if f_kind != 'all' else None)
        if wanted:
            q = q.filter(CashDayDifference.kind == wanted)
        if from_date:
            q = q.filter(CashDayDifference.position_date >= date.fromisoformat(from_date))
        if to_date:
            q = q.filter(CashDayDifference.position_date <= date.fromisoformat(to_date))
        if f_status != 'all':
            q = q.filter(CashDayDifference.status == f_status)
        if f_account_id is not None:
            q = q.filter(CashDayDifference.account_id == f_account_id)
        if f_recorded_by:
            q = q.filter(CashDayDifference.recorded_by.ilike(f'%{f_recorded_by}%'))
        if f_source != 'all':
            q = q.filter(CashDayDifference.source == f_source)
        if amount_min is not None:
            q = q.filter(func.abs(CashDayDifference.difference) >= amount_min)
        if amount_max is not None:
            q = q.filter(func.abs(CashDayDifference.difference) <= amount_max)
        if f_q:
            like = f'%{f_q}%'
            q = q.filter(or_(
                CashDayDifference.account_name.ilike(like),
                CashDayDifference.recorded_by.ilike(like),
                CashDayDifference.closed_by.ilike(like),
            ))
        return q

    # Download every filtered row (both cards, current sort) for offline
    # analysis; pagination is deliberately ignored here.
    if args.get('export_csv') == '1':
        buf = io.StringIO()
        import csv
        writer = csv.writer(buf)
        writer.writerow(['Day', 'Account', 'Type', 'Status', 'Source', 'Recorded by',
                         'Recorded at', 'Expected', 'Counted', 'Difference',
                         'Closed by', 'Closed at', 'New expected', 'New counted'])
        for item in filtered().order_by(*sort_orders[f_sort]).all():
            writer.writerow([
                item.position_date.isoformat() if item.position_date else '',
                item.account_name, item.kind, item.status, item.source,
                item.recorded_by or '',
                item.recorded_at.strftime('%Y-%m-%d %H:%M:%S') if item.recorded_at else '',
                f'{item.expected:.2f}', f'{item.counted:.2f}', f'{item.difference:.2f}',
                item.closed_by or '',
                item.closed_at.strftime('%Y-%m-%d %H:%M:%S') if item.closed_at else '',
                '' if item.closed_expected is None else f'{item.closed_expected:.2f}',
                '' if item.closed_counted is None else f'{item.closed_counted:.2f}',
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = (
            f'attachment; filename=differences_{from_date or "all"}_to_{to_date or "all"}.csv')
        return resp

    def card(kind, page_name):
        page = max(1, args.get(page_name, 1, type=int) or 1)
        q = filtered(kind)
        total = q.count()
        pages = max(1, (total + 9) // 10)
        page = min(page, pages)
        rows = q.order_by(*sort_orders[f_sort]).offset((page - 1) * 10).limit(10).all()
        return dict(rows=rows, total=total, page=page, pages=pages)

    # Filtered totals across every page, plus a per-account roll-up, so the
    # pattern (which account / which kind / still open) can be read at a
    # glance without paging through rows.
    agg = filtered().with_entities(
        func.count(CashDayDifference.id),
        func.coalesce(func.sum(case((CashDayDifference.kind == 'less', 1), else_=0)), 0),
        func.coalesce(func.sum(case((CashDayDifference.kind == 'less',
                                     func.abs(CashDayDifference.difference)), else_=0)), 0),
        func.coalesce(func.sum(case((CashDayDifference.kind == 'excess', 1), else_=0)), 0),
        func.coalesce(func.sum(case((CashDayDifference.kind == 'excess',
                                     func.abs(CashDayDifference.difference)), else_=0)), 0),
        func.coalesce(func.sum(CashDayDifference.difference), 0),
        func.coalesce(func.sum(case((CashDayDifference.status == 'open', 1), else_=0)), 0),
        func.coalesce(func.sum(case((CashDayDifference.status == 'open',
                                     func.abs(CashDayDifference.difference)), else_=0)), 0),
    ).one()
    summary = dict(
        total=agg[0] or 0,
        less_count=int(agg[1] or 0), less_total=float(agg[2] or 0),
        excess_count=int(agg[3] or 0), excess_total=float(agg[4] or 0),
        net=float(agg[5] or 0),
        open_count=int(agg[6] or 0), open_total=float(agg[7] or 0),
    )
    account_summary = [
        dict(account_id=row[0], account_name=row[1], total=int(row[2] or 0),
             less_count=int(row[3] or 0), less_total=float(row[4] or 0),
             excess_count=int(row[5] or 0), excess_total=float(row[6] or 0),
             net=float(row[7] or 0), open_count=int(row[8] or 0))
        for row in filtered().with_entities(
            CashDayDifference.account_id, CashDayDifference.account_name,
            func.count(CashDayDifference.id),
            func.coalesce(func.sum(case((CashDayDifference.kind == 'less', 1), else_=0)), 0),
            func.coalesce(func.sum(case((CashDayDifference.kind == 'less',
                                         func.abs(CashDayDifference.difference)), else_=0)), 0),
            func.coalesce(func.sum(case((CashDayDifference.kind == 'excess', 1), else_=0)), 0),
            func.coalesce(func.sum(case((CashDayDifference.kind == 'excess',
                                         func.abs(CashDayDifference.difference)), else_=0)), 0),
            func.coalesce(func.sum(CashDayDifference.difference), 0),
            func.coalesce(func.sum(case((CashDayDifference.status == 'open', 1), else_=0)), 0),
        ).group_by(CashDayDifference.account_id, CashDayDifference.account_name)
         .order_by(func.sum(func.abs(CashDayDifference.difference)).desc(),
                   CashDayDifference.account_name.asc()).all()
    ]

    # Dropdown choices come from the recorded history itself (before
    # filtering, so a choice never disappears while it is selected).
    account_options = [
        dict(id=row[0], name=row[1]) for row in db.session.query(
            CashDayDifference.account_id, CashDayDifference.account_name)
        .distinct().order_by(CashDayDifference.account_name.asc()).all()
    ]
    # Carried by pagination / CSV links so paging never drops a filter.
    filter_args = {}
    if preset != 'custom':
        filter_args['preset'] = preset
    if from_date:
        filter_args['from_date'] = from_date
    if to_date:
        filter_args['to_date'] = to_date
    if f_kind != 'all':
        filter_args['kind'] = f_kind
    if f_status != 'all':
        filter_args['status'] = f_status
    if f_account_id is not None:
        filter_args['account_id'] = f_account_id
    if f_recorded_by:
        filter_args['recorded_by'] = f_recorded_by
    if f_source != 'all':
        filter_args['source'] = f_source
    if amount_min_raw:
        filter_args['amount_min'] = amount_min_raw
    if amount_max_raw:
        filter_args['amount_max'] = amount_max_raw
    if f_q:
        filter_args['q'] = f_q
    if f_sort != 'newest':
        filter_args['sort'] = f_sort

    for row in account_summary:
        row['filter_url'] = url_for('daily_reconciliation_differences',
                                     **{**filter_args, 'account_id': row['account_id']})

    account_label = next((o['name'] for o in account_options if o['id'] == f_account_id), '')
    active_labels = []
    if from_date or to_date:
        active_labels.append(f"Dates: {from_date or '…'} to {to_date or '…'}")
    if f_kind != 'all':
        active_labels.append('Less only' if f_kind == 'less' else 'Excess only')
    if f_status != 'all':
        active_labels.append(f'Status: {f_status}')
    if f_account_id is not None:
        active_labels.append(f'Account: {account_label or f_account_id}')
    if f_recorded_by:
        active_labels.append(f'Recorded by: {f_recorded_by}')
    if f_source != 'all':
        active_labels.append('Imported snapshots only' if f_source == 'imported' else 'Live counts only')
    if amount_min is not None or amount_max is not None:
        active_labels.append(
            f"Difference Rs. {amount_min_raw or '0'} – {amount_max_raw or 'any'}")
    if f_q:
        active_labels.append(f'“{f_q}”')

    less_card = card('less', 'less_page')
    excess_card = card('excess', 'excess_page')
    return render_template('daily_reconciliation_differences.html',
        less=less_card, excess=excess_card,
        from_date=from_date, to_date=to_date, preset=preset,
        f_kind=f_kind, f_status=f_status, f_account_id=f_account_id,
        f_recorded_by=f_recorded_by, f_source=f_source,
        amount_min=amount_min_raw, amount_max=amount_max_raw, f_q=f_q,
        f_sort=f_sort, sort_label=sort_labels[f_sort],
        show_less=f_kind in ('all', 'less'), show_excess=f_kind in ('all', 'excess'),
        summary=summary, account_summary=account_summary,
        account_options=account_options,
        filter_args=filter_args, active_labels=active_labels,
        counter_less=recon_counter.counter_accounts()[0],
        counter_excess=recon_counter.counter_accounts()[1],
        counter_total=recon_counter.counter_balance_total(),
        open_posted_total=recon_counter.open_difference_total(),
        resolution_kinds=recon_counter.RESOLUTION_KINDS,
        can_resolve=_dr_can_resolve(),
        counterpart_accounts=_dr_resolution_accounts())


def _dr_can_resolve():
    """Only an operator who may finalise a day may resolve its differences."""
    role = (getattr(current_user, 'role', '') or '').strip().lower()
    if role in ('admin', 'root'):
        return True
    return bool(getattr(current_user, 'can_manage_accounts', False))


def _dr_resolution_accounts():
    """Accounts that can absorb a resolved difference (never a system account)."""
    from app.services.system_accounts import is_reserved_account
    return [account for account in Account.query.filter(
        func.coalesce(Account.is_active, True) == True,  # noqa: E712
    ).order_by(Account.name.asc(), Account.id.asc()).all()
        if not is_reserved_account(account)]


def _dr_resolve_difference(*, actor=None, difference=None):
    """Close a *posted* difference by posting what really happened to the money.

    A finalised day's closing is a fact, so resolving never rewrites the count:
    it offsets the counter account with a real, attributed entry.
    """
    from app.services import recon_counter
    from models import CashDayDifference

    if difference is not None:
        row = difference
    else:
        diff_id = request.form.get('difference_id', type=int)
        row = db.session.get(CashDayDifference, diff_id) if diff_id else None
    back = url_for('daily_reconciliation_differences')
    if not _dr_can_resolve():
        flash('Only an administrator can resolve a difference.', 'danger')
        return redirect(back)
    if row is None:
        flash('Difference not found.', 'danger')
        return redirect(back)
    try:
        counterpart = request.form.get('counterpart_account_id', type=int)
        tx = recon_counter.resolve_difference(
            row,
            kind=request.form.get('resolution_kind'),
            counterpart=counterpart,
            note=request.form.get('resolution_note') or '',
            actor=actor,
        )
        db.session.commit()
        flash(
            f'Difference resolved: Rs. {abs(float(row.difference or 0)):,.2f} '
            f'({recon_counter.counter_name_for(row.kind)}) offset by entry '
            f'ATX-{tx.id}.', 'success'
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Resolving a daily reconciliation difference failed')
        flash(f'Unable to resolve the difference: {exc or "please try again."}', 'danger')
    return redirect(request.form.get('return_to') or back)


def _ft_date_preset(preset, today):
    if preset == 'today':
        return today, today
    if preset == 'yesterday':
        y = today - timedelta(days=1)
        return y, y
    if preset == 'this_week':
        return today - timedelta(days=today.weekday()), today
    if preset == 'last_30':
        return today - timedelta(days=30), today
    if preset == 'all':
        return date(2000, 1, 1), today
    return today.replace(day=1), today  # this_month default


def _ft_build_context(args, prefix='', embed=False, posted_after=None):
    """Build the Financial Tracking Filter Matrix context (reference layout #2).

    Shared by the standalone /financial_tracker page (prefix='') and the
    embedded "Transactions" tab on /cash_flow (prefix='ft_', to avoid
    clashing with the cash flow page's own query params).
    """
    today = pk_today()
    preset = (args.get(prefix + 'preset') or 'this_month').strip().lower()
    from_d, to_d = _ft_date_preset(preset, today)
    f_direction = (args.get(prefix + 'direction') or 'all').strip().lower()
    f_section = (args.get(prefix + 'section') or '').strip()
    f_category = (args.get(prefix + 'category') or '').strip()
    f_account = (args.get(prefix + 'account') or '').strip()
    f_client = (args.get(prefix + 'client') or '').strip()
    f_supplier = (args.get(prefix + 'supplier') or '').strip()
    f_partner = (args.get(prefix + 'partner') or '').strip()
    f_worker = (args.get(prefix + 'worker') or '').strip()
    f_lender = (args.get(prefix + 'lender') or '').strip()
    f_vehicle = (args.get(prefix + 'vehicle') or '').strip().lower()
    f_method = (args.get(prefix + 'method') or 'all').strip().lower()
    f_q = (args.get(prefix + 'q') or '').strip().lower()

    rows = collect_cash_flow_rows(
        from_d.strftime('%Y-%m-%d'), to_d.strftime('%Y-%m-%d'),
        posted_after=posted_after,
    )

    role_filters = {
        ('client',): f_client,
        ('supplier',): f_supplier,
        ('partner',): f_partner,
        ('worker',): f_worker,
        ('loan_lender', 'loan'): f_lender,
    }
    active_roles = {k: v for k, v in role_filters.items() if v and v != 'all'}

    filtered = []
    for r in rows:
        if (r.get('status') or 'active') != 'active':
            continue
        if f_direction in ('in', 'out') and r.get('type') != f_direction:
            continue
        if f_direction == 'transfer' and r.get('type') != CF_DIR_TRANSFER:
            continue
        if f_section and f_section.lower() != 'all':
            if (r.get('section_label') or '').strip().lower() != f_section.lower():
                continue
        if f_category and f_category.lower() not in (r.get('category') or '').lower():
            continue
        if f_account:
            try:
                aid = int(f_account)
            except ValueError:
                aid = -1
            if r.get('account_id') != aid and r.get('account_to_id') != aid:
                continue
        if active_roles:
            ptype = (r.get('party_type') or '').lower()
            pname = (r.get('party_name') or '')
            ok = False
            for roles, val in active_roles.items():
                if ptype in roles and (val.lower() == pname.lower() or val.lower() in pname.lower()):
                    ok = True
            if not ok:
                continue
        if f_vehicle and f_vehicle not in ' '.join([
            r.get('description') or '', r.get('note') or '', r.get('reference') or ''
        ]).lower():
            continue
        if f_method != 'all':
            method_val = ' '.join(filter(None, [
                r.get('method') or '',
                r.get('account_category') or '',
            ])).lower() or (r.get('origin_label') or '').lower()
            if f_method not in method_val:
                continue
        if f_q:
            blob = ' '.join([
                str(r.get('reference') or ''), str(r.get('description') or ''),
                str(r.get('note') or ''), str(r.get('party_name') or ''),
                str(r.get('category') or ''), str(r.get('account_display') or ''),
            ]).lower()
            if f_q not in blob:
                continue
        filtered.append(r)

    summary = summarize_cash_flow_rows(filtered)
    total_in = _money_round(summary['total_cash_in'] + summary['total_transfer_in'])
    total_out = _money_round(summary['total_cash_out'] + summary['total_transfer_out'])
    net = _money_round(total_in - total_out)

    # Dropdown options sourced from the data.
    all_rows_active = [r for r in rows if (r.get('status') or 'active') == 'active']
    section_opts = sorted({
        (r.get('section_label') or '').strip()
        for r in all_rows_active
        if (r.get('section_label') or '').strip()
    })
    cat_options = sorted({(r.get('category') or '').strip() for r in all_rows_active if (r.get('category') or '').strip()})
    account_options = [{'id': a.id, 'label': _cf_account_label(a)} for a in _cf_company_accounts(active_only=True)]

    def _party_opts(*ptypes):
        wanted = {p.lower() for p in ptypes}
        return sorted({
            (r.get('party_name') or '').strip()
            for r in all_rows_active
            if (r.get('party_type') or '').lower() in wanted and (r.get('party_name') or '').strip()
        })

    ctx = dict(
        preset=preset, from_d=from_d, to_d=to_d,
        f_direction=f_direction, f_section=f_section, f_category=f_category, f_account=f_account,
        f_client=f_client, f_supplier=f_supplier, f_partner=f_partner,
        f_worker=f_worker, f_lender=f_lender, f_vehicle=f_vehicle, f_method=f_method, f_q=f_q,
        rows=filtered, total_in=total_in, total_out=total_out, net=net,
        record_count=len(filtered),
        section_opts=section_opts,
        cat_options=cat_options, account_options=account_options,
        client_opts=_party_opts('client'), supplier_opts=_party_opts('supplier'),
        partner_opts=_party_opts('partner'), worker_opts=_party_opts('worker'),
        lender_opts=_party_opts('loan_lender', 'loan'),
        vehicle_opts=sorted({(r.get('vehicle') or '').strip() for r in all_rows_active if (r.get('vehicle') or '').strip()}),
        breakdown_cat=summary['breakdown_cat'],
        breakdown_account=summary['breakdown_account'],
        prefix=prefix,
        embed=embed,
    )
    return ctx


@bp.route('/financial_tracker', methods=['GET'])
@login_required
def financial_tracker():
    """Financial Tracking Filter Matrix (reference layout #2)."""
    ctx = _ft_build_context(request.args)
    from_d, to_d = ctx['from_d'], ctx['to_d']
    filtered = ctx['rows']

    if request.args.get('export_csv') == '1':
        buf = io.StringIO()
        import csv
        w = csv.writer(buf)
        w.writerow([
            'Date', 'Type', 'Account', 'Section', 'Category', 'Party', 'In', 'Out',
            'Transfer', 'Reference', 'Description', 'User',
        ])
        for r in filtered:
            w.writerow([
                r.get('date'), r.get('tx_type_label'), r.get('account_display'),
                r.get('section_label'),
                r.get('category'), r.get('party_name'), r.get('cash_in') or '',
                r.get('cash_out') or '', r.get('transfer_amount') or '',
                r.get('reference'), r.get('description'), r.get('created_by'),
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename=financial_tracker_{from_d}_{to_d}.csv'
        return resp

    return render_template('financial_tracker.html', ft=ctx)


@bp.route('/cash_flow_differences/<int:rec_id>')
@login_required
def cash_flow_reconciliation_detail(rec_id):
    reconciliation = CashFlowDifferenceAdjustment.query.get_or_404(rec_id)
    audit_trail = CashFlowReconciliationAudit.query.filter_by(
        adjustment_date=reconciliation.adjustment_date
    ).order_by(CashFlowReconciliationAudit.changed_at.asc(), CashFlowReconciliationAudit.id.asc()).all()
    return render_template(
        'cash_flow_reconciliation_detail.html',
        reconciliation=reconciliation,
        audit_trail=audit_trail,
    )
