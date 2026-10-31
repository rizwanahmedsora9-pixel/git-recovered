from ._common import *  # noqa


@bp.route('/client-ledger/categories', methods=['POST'])
@login_required
def add_client_category():
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))

    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Category name is required', 'danger')
        return redirect(url_for('clients'))
    if len(name) > 50:
        flash('Category name must be 50 characters or fewer', 'danger')
        return redirect(url_for('clients'))

    existing = ClientCategory.query.filter(
        func.lower(func.trim(ClientCategory.name)) == name.casefold()
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            db.session.commit()
        flash(f'Client category "{existing.name}" already exists', 'warning')
        return redirect(url_for('clients'))

    db.session.add(ClientCategory(name=name))
    db.session.commit()
    flash(f'Client category "{name}" added', 'success')
    return redirect(url_for('clients'))
