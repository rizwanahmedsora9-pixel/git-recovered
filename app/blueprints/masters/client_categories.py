from ._common import *  # noqa

CLIENT_CATEGORY_DEFAULT = 'General'


def _client_category_counts():
    """Return {casefolded name: number of client records using it}."""
    counts = {}
    rows = (
        db.session.query(func.lower(func.trim(Client.category)), func.count(Client.id))
        .group_by(func.lower(func.trim(Client.category)))
        .all()
    )
    for name, total in rows:
        counts[str(name or '').strip().casefold()] = int(total or 0)
    return counts


def _client_category_list():
    """All category master rows (active first) plus their usage counts."""
    ensure_client_category_options()
    counts = _client_category_counts()
    rows = ClientCategory.query.order_by(
        ClientCategory.is_active.desc(), ClientCategory.name.asc()
    ).all()
    for row in rows:
        row.client_count = counts.get(str(row.name or '').strip().casefold(), 0)
    return rows


def _normalized_category_name(raw):
    return (raw or '').strip()


def _find_category_by_name(name):
    return ClientCategory.query.filter(
        func.lower(func.trim(ClientCategory.name)) == name.strip().casefold()
    ).first()


def _rename_clients_category(old_name, new_name):
    """Keep the Client.category string column in sync with a rename."""
    clients = Client.query.filter(
        func.lower(func.trim(Client.category)) == str(old_name or '').strip().casefold()
    ).all()
    for client in clients:
        client.category = new_name
    return len(clients)


def _count_clients_using(name):
    return Client.query.filter(
        func.lower(func.trim(Client.category)) == str(name or '').strip().casefold()
    ).count()


@bp.route('/client-ledger/categories', methods=['GET'])
@login_required
def manage_client_categories():
    """Category master screen for the client ledger.

    Client.category stays a plain string for reports/imports, but this screen
    lets a user create, rename, deactivate and delete the categories that are
    offered in the client forms and filters.
    """
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))

    return render_template(
        'client_categories.html',
        categories=_client_category_list(),
        can_manage=_user_can('can_manage_clients'),
        default_category=CLIENT_CATEGORY_DEFAULT,
    )


@bp.route('/client-ledger/categories', methods=['POST'])
@bp.route('/client-ledger/categories/add', methods=['POST'])
@login_required
def add_client_category():
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))

    name = _normalized_category_name(request.form.get('name'))
    if not name:
        flash('Category name is required', 'danger')
        return redirect(url_for('manage_client_categories'))
    if len(name) > 50:
        flash('Category name must be 50 characters or fewer', 'danger')
        return redirect(url_for('manage_client_categories'))

    existing = _find_category_by_name(name)
    if existing:
        if not existing.is_active:
            existing.is_active = True
            db.session.commit()
            flash(f'Client category "{existing.name}" was re-activated', 'success')
        else:
            flash(f'Client category "{existing.name}" already exists', 'warning')
        return redirect(url_for('manage_client_categories'))

    db.session.add(ClientCategory(name=name, is_active=True))
    db.session.commit()
    flash(f'Client category "{name}" added', 'success')
    return redirect(url_for('manage_client_categories'))


@bp.route('/client-ledger/categories/<int:category_id>/rename', methods=['POST'])
@login_required
def rename_client_category(category_id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))

    category = db.session.get(ClientCategory, category_id)
    if not category:
        flash('Client category not found', 'danger')
        return redirect(url_for('manage_client_categories'))

    name = _normalized_category_name(request.form.get('name'))
    if not name:
        flash('Category name is required', 'danger')
        return redirect(url_for('manage_client_categories'))
    if len(name) > 50:
        flash('Category name must be 50 characters or fewer', 'danger')
        return redirect(url_for('manage_client_categories'))

    old_name = category.name
    if name.casefold() == str(old_name or '').strip().casefold():
        flash('Category name is unchanged', 'info')
        return redirect(url_for('manage_client_categories'))

    clash = _find_category_by_name(name)
    if clash and clash.id != category.id:
        flash(f'Client category "{clash.name}" already exists', 'danger')
        return redirect(url_for('manage_client_categories'))

    category.name = name
    moved = _rename_clients_category(old_name, name)
    db.session.commit()
    flash(
        f'Client category "{old_name}" renamed to "{name}"'
        + (f' — {moved} client(s) updated' if moved else ''),
        'success',
    )
    return redirect(url_for('manage_client_categories'))


@bp.route('/client-ledger/categories/<int:category_id>/toggle', methods=['POST'])
@login_required
def toggle_client_category(category_id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))

    category = db.session.get(ClientCategory, category_id)
    if not category:
        flash('Client category not found', 'danger')
        return redirect(url_for('manage_client_categories'))

    if category.name.strip().casefold() == CLIENT_CATEGORY_DEFAULT.casefold() and category.is_active:
        flash('The default General category cannot be deactivated', 'danger')
        return redirect(url_for('manage_client_categories'))

    if category.is_active:
        in_use = _count_clients_using(category.name)
        if in_use:
            flash(
                f'"{category.name}" is used by {in_use} client(s). Move them to '
                'another category before deactivating it.',
                'danger',
            )
            return redirect(url_for('manage_client_categories'))

    category.is_active = not bool(category.is_active)
    db.session.commit()
    flash(
        f'Client category "{category.name}" '
        + ('re-activated' if category.is_active else 'deactivated'),
        'success',
    )
    return redirect(url_for('manage_client_categories'))


@bp.route('/client-ledger/categories/<int:category_id>/delete', methods=['POST'])
@login_required
def delete_client_category(category_id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))

    category = db.session.get(ClientCategory, category_id)
    if not category:
        flash('Client category not found', 'danger')
        return redirect(url_for('manage_client_categories'))

    if category.name.strip().casefold() == CLIENT_CATEGORY_DEFAULT.casefold():
        flash('The default General category cannot be deleted', 'danger')
        return redirect(url_for('manage_client_categories'))

    in_use = _count_clients_using(category.name)
    if in_use:
        flash(
            f'"{category.name}" is used by {in_use} client(s) and cannot be deleted. '
            'Rename or deactivate it instead.',
            'danger',
        )
        return redirect(url_for('manage_client_categories'))

    db.session.delete(category)
    db.session.commit()
    flash(f'Client category "{category.name}" deleted', 'success')
    return redirect(url_for('manage_client_categories'))
