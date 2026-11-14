"""bills — split from sales.py."""
from ._common import *  # noqa
from app.services.urdu_names import urdu_name
from app.services.terms import get_selected_terms

# Optional marks on the Urdu sheet the print dialog can switch off.  The list
# lives in the sheet module itself so the page map, the route and the layout
# cannot drift apart; it maps onto ``bill_urdu.build_document(sections=...)``.
from app.services.urdu_receipt import SHEET_OPTIONS as _URDU_SECTION_KEYS

_TRUTHY = {'1', 'yes', 'true', 'on', 'show'}
_FALSY = {'0', 'no', 'false', 'off', 'hide'}


def _urdu_section_flags(args):
    """``{key: bool}`` for the Urdu receipt's page map.

    Every mark on the sheet defaults to **on**, so an old bookmark or a direct
    link that carries no section parameters still prints the approved full
    sheet.  A key may be repeated (the dialog posts a hidden ``0`` plus the
    checkbox's ``1`` so an unchecked box is never silently dropped); the last
    value wins.
    """
    flags = {key: True for key in _URDU_SECTION_KEYS}
    for key in _URDU_SECTION_KEYS:
        for raw in args.getlist(key):
            value = (raw or '').strip().lower()
            if value in _TRUTHY:
                flags[key] = True
            elif value in _FALSY:
                flags[key] = False
    return flags


_ALT_NAME_MODES = ('both', 'booked', 'delivered')


def _urdu_alt_name_mode(args):
    """How an alternate-material line is named on the Urdu receipt.

    ``both`` (the default, and what any old link without the parameter gets)
    prints "delivered (بجائے booked)"; ``booked`` and ``delivered`` print just
    that one name.  Display only - nothing is stored or recomputed.
    """
    value = (args.get('alt_name') or '').strip().lower()
    return value if value in _ALT_NAME_MODES else 'both'


def _material_urdu_names():
    """``{english material name (casefolded): urdu name}`` for printed bills.

    Materials carry an optional Urdu name (``Material.name_ur``) that the Urdu
    bill prints instead of the English brand name; anything without one keeps
    its English name so a bill is never left with a blank line.
    """
    try:
        rows = Material.query.filter(
            Material.name_ur.isnot(None), Material.name_ur != ''
        ).all()
    except Exception:
        return {}
    return {(r.name or '').strip().casefold(): r.name_ur.strip()
            for r in rows if (r.name or '').strip() and (r.name_ur or '').strip()}


def _field(item, *names, default=None):
    """Read *names* from a dict row or an ORM/namespace object."""
    for name in names:
        if isinstance(item, dict):
            value = item.get(name)
        else:
            value = getattr(item, name, None)
        if value not in (None, ''):
            return value
    return default


def _translate_material_ur(raw_name, names_map, alt_mode='both'):
    """Translate a material name (including alternate-material labels) to Urdu.

    ``alt_mode`` picks what an alternate-material label prints: ``both``
    (delivered, then the booked name in brackets), ``booked`` or ``delivered``.
    """
    text = str(raw_name or '').strip()
    if not text:
        return ''
    alt_match = re.match(r'^(.+?)\s*\(ALT for booked\s+(.+?)\)$', text, flags=re.IGNORECASE)
    if alt_match:
        deliv_raw = alt_match.group(1).strip()
        booked_raw = alt_match.group(2).strip()
        deliv_ur = urdu_name(deliv_raw, names_map.get(deliv_raw.casefold(), ''))
        booked_ur = urdu_name(booked_raw, names_map.get(booked_raw.casefold(), ''))
        if alt_mode == 'booked':
            return booked_ur
        if alt_mode == 'delivered':
            return deliv_ur
        return f"{deliv_ur} (بجائے {booked_ur})"
    return urdu_name(text, names_map.get(text.casefold(), ''))


def _with_urdu_item_names(items, lang, alt_mode='both'):
    """Return bill rows with ``name_ur`` filled from the material catalogue."""
    rows = []
    names = _material_urdu_names() if lang == 'ur' else {}
    for item in items or []:
        name = _field(item, 'name', 'product_name', 'material_name', 'material', 'mat_name',
                      default='')
        urdu = ''
        if lang == 'ur':
            urdu = str(_field(item, 'name_ur', default='') or '').strip()
            if not urdu:
                urdu = _translate_material_ur(name, names, alt_mode)
            else:
                urdu = urdu_name(name, urdu)
        rows.append({
            'name': name,
            'name_ur': urdu,
            'qty': _field(item, 'qty', default=0),
            'price_at_time': _field(item, 'price_at_time', default=0),
        })
    return rows


def _with_urdu_booked_summary(summary, alt_mode='both'):
    """Copy ``material_stock_summary`` rows with Urdu material names added."""
    if not summary:
        return []
    names = _material_urdu_names()
    rows = []
    for row in summary:
        item = dict(row) if isinstance(row, dict) else {
            'material': getattr(row, 'material', ''),
            'material_display': getattr(row, 'material_display', ''),
            'dispatched': getattr(row, 'dispatched', 0),
            'added': getattr(row, 'added', 0),
            'last_stock': getattr(row, 'last_stock', 0),
            'remaining': getattr(row, 'remaining', 0),
        }
        mat = str(item.get('material') or item.get('material_display') or '').strip()
        if not (item.get('material_ur') or '').strip():
            item['material_ur'] = _translate_material_ur(mat, names, alt_mode)
        rows.append(item)
    return rows


def _direct_sale_bill_items(sale):
    """Build bill item rows for a DirectSale, preserving alternate booked labels."""
    if not sale:
        return []
    sale_entries = Entry.query.filter(
        Entry.source_module == 'sales',
        Entry.source_table == 'direct_sale',
        Entry.source_id == sale.id,
        Entry.is_void == False,
    ).order_by(Entry.id.asc()).all()

    if not sale_entries:
        refs = _direct_sale_bill_refs(sale)
        if refs:
            legacy_q = Entry.query.filter(
                Entry.bill_no.in_(refs),
                Entry.nimbus_no == 'Direct Sale',
                Entry.type == 'OUT',
                Entry.is_void == False,
            )
            client_code = (getattr(sale, 'client_code', None) or '').strip()
            client_name = (getattr(sale, 'client_name', None) or '').strip()
            if client_code:
                legacy_q = legacy_q.filter(or_(
                    Entry.client_code == client_code,
                    func.lower(func.trim(func.coalesce(Entry.client, ''))) == client_name.lower(),
                ))
            elif client_name:
                legacy_q = legacy_q.filter(
                    func.lower(func.trim(func.coalesce(Entry.client, ''))) == client_name.lower()
                )
            sale_entries = legacy_q.order_by(Entry.id.asc()).all()

    typed_entry_map = {}
    loose_entry_map = {}
    for e in sale_entries:
        mat = (e.material or '').strip()
        qty_key = round(float(e.qty or 0), 6)
        is_bk_entry = ((e.client_category or '').strip() == 'Booking Delivery')
        typed_entry_map.setdefault((mat, qty_key, is_bk_entry), []).append(e)
        loose_entry_map.setdefault((mat, qty_key), []).append(e)

    sale_items = list(sale.items or [])
    sale_item_ids = [it.id for it in sale_items if getattr(it, 'id', None)]
    alloc_booked_by_item = {}
    if sale_item_ids:
        alloc_rows = (
            BookingAllocation.query
            .filter(
                BookingAllocation.sale_item_id.in_(sale_item_ids),
                BookingAllocation.is_void == False,
            )
            .order_by(BookingAllocation.id.asc())
            .all()
        )
        for alloc in alloc_rows:
            bk_item = getattr(alloc, 'booking_item', None)
            bk_mat = (getattr(bk_item, 'material_name', None) or '').strip()
            if bk_mat and alloc.sale_item_id not in alloc_booked_by_item:
                alloc_booked_by_item[alloc.sale_item_id] = bk_mat

    category = normalize_sale_category(getattr(sale, 'category', None))
    used_entry_ids = set()

    def _pop_unused(bucket):
        while bucket:
            cand = bucket.pop(0)
            cid = getattr(cand, 'id', id(cand))
            if cid not in used_entry_ids:
                used_entry_ids.add(cid)
                return cand
        return None

    items = []
    for it in sale_items:
        delivered = (it.product_name or '').strip()
        qty_key = round(float(it.qty or 0), 6)
        is_booked_line = (
            category in ('Booking Delivery', 'Mixed Transaction')
            and float(it.price_at_time or 0) <= 0
        )
        entry = _pop_unused(typed_entry_map.get((delivered, qty_key, is_booked_line)))
        if entry is None and (is_booked_line or category not in ('Booking Delivery', 'Mixed Transaction')):
            entry = _pop_unused(loose_entry_map.get((delivered, qty_key)))

        booked_name = ''
        if entry and (entry.booked_material or '').strip():
            booked_name = entry.booked_material.strip()
        elif is_booked_line and getattr(it, 'id', None) in alloc_booked_by_item:
            booked_name = alloc_booked_by_item[it.id]

        name = it.product_name
        if booked_name and delivered and booked_name.casefold() != delivered.casefold():
            name = format_alt_material_label(booked_name, delivered)
        items.append({'name': name, 'qty': it.qty, 'price_at_time': (it.price_at_time or 0)})
    return items


def _find_receipt_account_tx(source_type, source_id):
    if not source_id:
        return None
    marker = f'[SRC:{source_type}:{source_id}]'
    return AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        or_(
            and_(AccountTransaction.source_type == source_type,
                 AccountTransaction.source_id == source_id),
            AccountTransaction.note.ilike(f'%{marker}%'),
        )
    ).order_by(AccountTransaction.id.desc()).first()


def _extract_method_from_note(note):
    match = re.search(
        r'(?:^|\s)Method:\s*([A-Za-z0-9 _-]+?)(?=\s*(?:\||\[SRC:|$))',
        str(note or ''),
    )
    return match.group(1).strip() if match else ''


def bill_document_source(bill_no, *, hint_type=None, hint_id=None,
                         hint_client_code=None, hint_client_name=None,
                         hint_entry_id=None):
    """Collect everything the Urdu sheet needs for one bill reference.

    Shared by the download/print route and the page behind the printed QR, so
    a scanned bill and a re-printed bill are assembled by one code path and
    cannot disagree.  Returns a dict, or ``None`` when the reference matches
    no bill at all.
    """
    hint_type = (hint_type or '').strip().lower()
    hint_id = str(hint_id or '').strip()
    booking, payment, invoice, sale, grn, pending = _lookup_bill(
        bill_no,
        hint_type=hint_type,
        hint_id=hint_id,
        hint_client_code=hint_client_code,
        hint_client_name=hint_client_name,
        hint_entry_id=hint_entry_id
    )
    candidates_map = _bill_lookup_candidates_map(booking, payment, invoice, sale, grn, pending)
    effective_candidates = _effective_collision_candidates(candidates_map)
    ambiguous = not hint_type and not hint_id and len(effective_candidates) > 1

    bill_obj = None
    grn_bd = None
    bill_type = ''
    items = []

    client = None
    client_balance = 0
    previous_balance = 0
    recent_deliveries = []
    material_ledger_recent = []
    material_stock_summary = []
    direct_sale_rent_reconciliation = None
    delivery_people = None

    if booking or payment or invoice or sale or pending:
        bill_obj_temp = booking or payment or invoice or sale or pending
        c_name = getattr(bill_obj_temp, 'client_name', None)
        c_code = getattr(bill_obj_temp, 'client_code', None)
        if c_code: client = Client.query.filter_by(code=c_code).first()
        if not client and c_name: client = Client.query.filter_by(name=c_name).first()

        if client:
            cutoff_dt = _bill_cutoff_dt_for_snapshot(
                booking=booking,
                payment=payment,
                invoice=invoice,
                sale=sale,
                pending=pending
            )
            client_balance = _client_balance_as_of(client, cutoff_dt=cutoff_dt)

            effect = 0
            if booking: effect = (booking.amount or 0) - (booking.paid_amount or 0)
            elif sale: effect = (sale.amount or 0) - (getattr(sale, 'discount', 0) or 0) - (sale.paid_amount or 0)
            elif payment: effect = -(payment.amount or 0)
            elif invoice: effect = invoice.balance or 0
            elif pending: effect = pending.amount or 0

            previous_balance = client_balance - effect

            is_booking_flow = False
            if booking:
                is_booking_flow = True
            elif sale and normalize_sale_category(getattr(sale, 'category', None)) in ['Booking Delivery', 'Mixed Transaction']:
                is_booking_flow = True
            elif invoice and getattr(invoice, 'direct_sales', None):
                for ds in invoice.direct_sales:
                    if normalize_sale_category(ds.category) in ['Booking Delivery', 'Mixed Transaction']:
                        is_booking_flow = True
                        break

            delivery_query = Entry.query.filter(
                (Entry.client_code == client.code) | (Entry.client == client.name),
                Entry.type == 'OUT',
                Entry.is_void == False
            )
            if is_booking_flow:
                delivery_query = delivery_query.filter(Entry.client_category == 'Booking Delivery')
            delivery_rows = delivery_query.all()
            if cutoff_dt:
                filtered_rows = []
                for d in delivery_rows:
                    d_dt = _parse_dt_safe(f"{d.date} {d.time}") or _parse_dt_safe(d.date) or datetime.min
                    if d_dt <= cutoff_dt:
                        filtered_rows.append(d)
                delivery_rows = filtered_rows
            recent_deliveries = sorted(
                delivery_rows,
                key=lambda d: (
                    _parse_dt_safe(f"{d.date} {d.time}") or _parse_dt_safe(d.date) or datetime.min,
                    d.id or 0
                ),
                reverse=True
            )[:5]
            if is_booking_flow:
                material_ledger_recent = _material_ledger_recent(
                    client,
                    only_booking=True,
                    limit_per_material=5,
                    cutoff_dt=cutoff_dt
                )

            bill_refs = []
            if booking:
                bill_refs = [booking.manual_bill_no, booking.auto_bill_no, f"BK-{booking.id}"]
            elif payment:
                bill_refs = [payment.manual_bill_no, payment.auto_bill_no, f"PAY-{payment.id}"]
            elif sale:
                bill_refs = [sale.manual_bill_no, sale.auto_bill_no, f"DS-{sale.id}", f"CSH-{sale.id}"]
                if getattr(sale, 'invoice', None) and sale.invoice and getattr(sale.invoice, 'invoice_no', None):
                    bill_refs.append(sale.invoice.invoice_no)
            elif invoice:
                bill_refs = [invoice.invoice_no]
                if getattr(invoice, 'direct_sales', None):
                    for ds in invoice.direct_sales:
                        bill_refs.extend([ds.manual_bill_no, ds.auto_bill_no, f"DS-{ds.id}", f"CSH-{ds.id}"])
            elif pending:
                bill_refs = [pending.bill_no]

            if is_booking_flow:
                material_stock_summary = _material_ledger_current_summary(material_ledger_recent, bill_refs)
            else:
                material_stock_summary = []

    if booking:
        bill_obj = booking
        bill_type = 'Booking'
        items = booking.items
        if not (getattr(booking, 'driver_name', None) or '').strip():
            inferred_driver = _infer_driver_name_from_refs(
                [booking.manual_bill_no, booking.auto_bill_no, f"BK-{booking.id}"],
                allow_booking=True
            )
            if inferred_driver:
                setattr(booking, 'driver_name', inferred_driver)
        if getattr(booking, 'receive_in_account', None) is not None:
            setattr(booking, '_resolved_payment_account', booking.receive_in_account)
        if (booking.paid_amount or 0) > 0 or getattr(booking, 'receive_in_account_id', None):
            tx = _find_receipt_account_tx('Booking', booking.id)
            if tx:
                if getattr(booking, '_resolved_payment_account', None) is None and tx.to_account is not None:
                    setattr(booking, '_resolved_payment_account', tx.to_account)
                parsed_method = _extract_method_from_note(tx.note)
                if parsed_method:
                    setattr(booking, '_resolved_payment_method', parsed_method)
    elif payment:
        bill_obj = payment
        bill_type = 'Payment'
        if (getattr(payment, 'payment_type', '') or '').strip().lower() != 'waive-off' and (
            getattr(payment, 'payment_account', None) is None
            and not (getattr(payment, 'account_name', '') or '').strip()
            and not (getattr(payment, 'bank_name', '') or '').strip()
            and float(getattr(payment, 'amount', 0) or 0) > 0
        ):
            tx = _find_receipt_account_tx('Payment', payment.id)
            if tx and (tx.to_account or tx.from_account):
                setattr(payment, '_resolved_payment_account', tx.to_account or tx.from_account)
    elif sale:
        bill_obj = sale
        bill_type = 'DirectSale'
        if (
            float(getattr(sale, 'paid_amount', 0) or 0) > 0
            and getattr(sale, 'payment_account', None) is None
            and not (getattr(sale, 'account_name', '') or '').strip()
            and not (getattr(sale, 'bank_name', '') or '').strip()
        ):
            tx = _find_receipt_account_tx('DirectSale', sale.id)
            if tx and tx.to_account is not None:
                setattr(sale, '_resolved_payment_account', tx.to_account)
        if not (getattr(sale, 'note', None) or '').strip():
            if getattr(sale, 'invoice', None) and (getattr(sale.invoice, 'note', None) or '').strip():
                sale.note = sale.invoice.note.strip()
        items = _direct_sale_bill_items(sale)

        if not (sale.driver_name or '').strip():
            inferred_driver = _infer_driver_name_from_refs(_direct_sale_bill_refs(sale))
            if inferred_driver:
                sale.driver_name = inferred_driver
        delivery_people = []
        for alloc in (getattr(sale, 'delivery_person_allocations', None) or []):
            if getattr(alloc, 'is_void', False):
                continue
            dp = getattr(alloc, 'delivery_person', None)
            name = (getattr(dp, 'name', None) or '').strip()
            if name:
                delivery_people.append(name)
        if not delivery_people and (sale.driver_name or '').strip():
            delivery_people = [sale.driver_name.strip()]
        rent_row = DeliveryRent.query.filter_by(sale_id=sale.id, is_void=False).order_by(DeliveryRent.id.desc()).first()
        sale_items_payload = [
            {
                'product_name': it.product_name,
                'qty': it.qty,
                'price_at_time': it.price_at_time
            }
            for it in (sale.items or [])
        ]
        fallback_delivery_cost = float(rent_row.amount or 0) if rent_row else 0.0
        effective_delivery_cost = float(getattr(sale, 'delivery_rent_cost', 0) or 0)
        if effective_delivery_cost <= 0:
            effective_delivery_cost = fallback_delivery_cost
        calc_rec = _rent_reconciliation_from_items(
            sale_items_payload,
            delivery_rent_cost=effective_delivery_cost,
            client_name=sale.client_name
        )
        direct_sale_rent_reconciliation = {
            'rent_item_revenue': float(getattr(sale, 'rent_item_revenue', 0) or calc_rec['rent_item_revenue']),
            'delivery_rent_cost': float(getattr(sale, 'delivery_rent_cost', 0) or calc_rec['delivery_rent_cost']),
            'rent_variance_loss': float(getattr(sale, 'rent_variance_loss', 0) or calc_rec['rent_variance_loss'])
        }
    elif invoice:
        bill_obj = invoice
        bill_type = 'Invoice'
        invoice.amount = invoice.total_amount
        if not (getattr(invoice, 'note', None) or '').strip():
            ds_notes = [ds.note.strip() for ds in (getattr(invoice, 'direct_sales', None) or []) if (getattr(ds, 'note', None) or '').strip()]
            if ds_notes:
                invoice.note = ' | '.join(dict.fromkeys(ds_notes))
            elif getattr(invoice, 'entries', None):
                entry_notes = [e.note.strip() for e in invoice.entries if (getattr(e, 'note', None) or '').strip()]
                if entry_notes:
                    invoice.note = ' | '.join(dict.fromkeys(entry_notes))
        driver_name = ''
        if getattr(invoice, 'direct_sales', None):
            for ds in invoice.direct_sales:
                if (ds.driver_name or '').strip():
                    driver_name = ds.driver_name.strip()
                    break
            if not driver_name:
                refs = []
                for ds in invoice.direct_sales:
                    refs.extend(_direct_sale_bill_refs(ds))
                driver_name = _infer_driver_name_from_refs(list(set(refs)), allow_booking=True)
        if driver_name:
            setattr(invoice, 'driver_name', driver_name)
        delivery_people = []
        if getattr(invoice, 'direct_sales', None):
            for ds in invoice.direct_sales:
                for alloc in (getattr(ds, 'delivery_person_allocations', None) or []):
                    if getattr(alloc, 'is_void', False):
                        continue
                    dp = getattr(alloc, 'delivery_person', None)
                    name = (getattr(dp, 'name', None) or '').strip()
                    if name:
                        delivery_people.append(name)
        if not delivery_people and (getattr(invoice, 'driver_name', None) or '').strip():
            delivery_people = [invoice.driver_name.strip()]
        # Calculate discount from linked sales
        invoice_discount = 0
        if getattr(invoice, 'direct_sales', None):
            invoice_discount = sum((getattr(ds, 'discount', 0) or 0) for ds in invoice.direct_sales)
        invoice.discount = invoice_discount
        invoice.paid_amount = max(0, (invoice.total_amount or 0) - invoice_discount - (invoice.balance or 0))
        if getattr(invoice, 'direct_sales', None):
            active_sales = [ds for ds in invoice.direct_sales if not getattr(ds, 'is_void', False)]
            paid_sales = [
                ds for ds in active_sales
                if float(getattr(ds, 'paid_amount', 0) or 0) > 0
                or getattr(ds, 'payment_account_id', None)
                or (getattr(ds, 'account_name', '') or '').strip()
                or (getattr(ds, 'bank_name', '') or '').strip()
            ]
            target_sales = paid_sales or active_sales
            if target_sales:
                primary_ds = target_sales[0]
                if getattr(primary_ds, 'payment_account', None) is None and float(getattr(primary_ds, 'paid_amount', 0) or 0) > 0:
                    tx = _find_receipt_account_tx('DirectSale', primary_ds.id)
                    if tx and tx.to_account is not None:
                        setattr(primary_ds, '_resolved_payment_account', tx.to_account)
                acc_names = []
                for ds in target_sales:
                    acc_obj = getattr(ds, '_resolved_payment_account', None) or getattr(ds, 'payment_account', None)
                    nm = (
                        (getattr(acc_obj, 'name', '') or getattr(acc_obj, 'account_holder_name', '') or '').strip()
                        or (getattr(ds, 'account_name', '') or '').strip()
                        or (getattr(ds, 'bank_name', '') or '').strip()
                    )
                    if nm and nm not in acc_names:
                        acc_names.append(nm)
                primary_acc = getattr(primary_ds, '_resolved_payment_account', None) or getattr(primary_ds, 'payment_account', None)
                if len(acc_names) == 1 and primary_acc is not None:
                    setattr(invoice, '_resolved_payment_account', primary_acc)
                elif acc_names:
                    setattr(invoice, 'account_name', ' / '.join(acc_names))
                for attr, target_attr in (('payment_method', '_resolved_payment_method'),
                                        ('bank_name', 'bank_name'),
                                        ('account_no', 'account_no')):
                    val = (getattr(primary_ds, attr, '') or '').strip()
                    if val and not (getattr(invoice, target_attr, '') or '').strip():
                        setattr(invoice, target_attr, val)
                if not (getattr(invoice, 'account_name', '') or '').strip():
                    val = (getattr(primary_ds, 'account_name', '') or '').strip()
                    if val:
                        setattr(invoice, 'account_name', val)
        invoice.date_posted = _parse_dt_safe(getattr(invoice, 'created_at', None)) or (datetime.combine(invoice.date, datetime.min.time()) if invoice.date else None)
        if getattr(invoice, 'entries', None) and invoice.entries:
            entry_total_qty = sum(float(e.qty or 0) for e in invoice.entries)
            inferred_rate = (float(invoice.total_amount or 0) / entry_total_qty) if entry_total_qty > 0 else 0
            items = []
            for e in invoice.entries:
                name = (e.material or '').strip()
                if e.booked_material and e.material and e.booked_material.strip() != e.material.strip():
                    name = format_alt_material_label(e.booked_material, e.material)
                items.append({
                    'name': name,
                    'qty': e.qty,
                    'price_at_time': inferred_rate
                })
        elif getattr(invoice, 'direct_sales', None) and invoice.direct_sales:
            active_sales = [d for d in invoice.direct_sales if not getattr(d, 'is_void', False)]
            ds = active_sales[0] if active_sales else invoice.direct_sales[0]
            # Preserve item rates and alternate booked labels so line items match DirectSale receipts.
            items = _direct_sale_bill_items(ds)
    elif grn:
        bill_obj = grn
        bill_type = 'GRN'
        from app.services.grn_costing import grn_breakdown
        grn_bd = grn_breakdown(grn)
        grn.amount = grn_bd['supplier_bill_amount']
        grn.paid_amount = grn_bd['supplier_paid']
        items = grn.items
    elif pending:
        bill_obj = SimpleNamespace(
            manual_bill_no=pending.bill_no,
            auto_bill_no='',
            invoice_no='',
            date_posted=_parse_dt_safe(pending.created_at),
            client_name=pending.client_name,
            client_code=pending.client_code,
            amount=pending.amount or 0,
            paid_amount=0,
            reason=pending.reason or '',
            nimbus_no=pending.nimbus_no or '',
            method='',
            photo_path='',
            note=(pending.note or '').strip()
        )
        bill_type = 'PendingBill'
        items = []

    return {
        'ambiguous': ambiguous,
        'bill': bill_obj,
        'bill_type': bill_type,
        'entry_hint_id': hint_entry_id,
        'items': items,
        'client': client,
        'client_balance': client_balance,
        'previous_balance': previous_balance,
        'delivery_people': delivery_people,
        'grn_bd': grn_bd,
        # Context panels of the English receipt; the Urdu sheet ignores them,
        # but the template below still receives them unchanged.
        'recent_deliveries': recent_deliveries,
        'material_ledger_recent': material_ledger_recent,
        'material_stock_summary': material_stock_summary,
        'direct_sale_rent_reconciliation': direct_sale_rent_reconciliation,
    }


# ---------------------------------------------------------------------------
# Urdu bill data
# ---------------------------------------------------------------------------
# Both the "Urdu PDF / Urdu Print" action and the page behind the printed QR
# build their sheet from these two helpers, so a scanned bill lists exactly the
# materials, names and figures the paper bill does.

# Which bill number the QR token carries: the same one the sheet prints.
def bill_number_of(bill_type, bill):
    """The reference the Urdu sheet prints in its meta strip."""
    if bill_type == 'PendingBill':
        bill_no = (getattr(bill, 'bill_no', '') or '')
        if bill_no:
            return bill_no
    for name in ('manual_bill_no', 'auto_bill_no', 'invoice_no', 'bill_no'):
        value = (getattr(bill, name, '') or '').strip()
        if value:
            return value
    return ''


def _urdu_driver_name(bill):
    """``(urdu, saved latin)`` for the bill's delivery person."""
    driver_name = (getattr(bill, 'driver_name', None) or '').strip()
    if not driver_name:
        return '', ''
    driver = DeliveryPerson.query.filter(
        func.lower(func.trim(DeliveryPerson.name)) == driver_name.lower()
    ).first()
    return urdu_name(driver_name, getattr(driver, 'name_ur', None)), driver_name


def urdu_bill_data(source, *, show_terms=False, terms=None, alt_mode='both'):
    """Urdu names, items and terms for one :func:`bill_document_source` result.

    Pure read: it fills in the Urdu item names the sheet prints, translates the
    client and delivery names, and (when the user asked for them) collects the
    selected terms and conditions.  Every figure is left as stored - the
    document builder never recomputes a total.
    """
    bill_obj = source['bill']
    client = source['client']
    items = _with_urdu_item_names(source['items'], 'ur', alt_mode)
    delivery_people = source['delivery_people']
    client_name_ur = ''
    if client is not None:
        client_name_ur = (getattr(client, 'name_ur', None) or '').strip()
    driver_name_ur, _latin = _urdu_driver_name(bill_obj)
    client_name = (getattr(bill_obj, 'client_name', None)
                   or getattr(client, 'name', '') or '')
    client_name_ur = urdu_name(client_name, client_name_ur)
    if delivery_people:
        translated = []
        for name in delivery_people:
            driver = DeliveryPerson.query.filter(
                func.lower(func.trim(DeliveryPerson.name))
                == name.strip().lower()).first()
            translated.append(urdu_name(name, getattr(driver, 'name_ur', None)))
        delivery_people = translated
    return {
        'items': items,
        'client_name_ur': client_name_ur,
        'driver_name_ur': driver_name_ur,
        'delivery_people': delivery_people,
        'booked_summary': _with_urdu_booked_summary(
            source.get('material_stock_summary'), alt_mode
        ),
        'terms': get_selected_terms(terms) if show_terms else [],
    }


def urdu_bill_document(source, *, show_terms=False, terms=None):
    """The complete :mod:`app.services.bill_urdu` document for one bill.

    Built exactly as ``download_invoice?lang=ur`` builds it (same source data,
    same Urdu names, same terms, same QR payload), which is what lets the
    scanned page show the sheet that was printed.
    """
    from app.services import bill_urdu

    bill_obj = source['bill']
    bill_type = source['bill_type']
    prepared = urdu_bill_data(source, show_terms=show_terms, terms=terms)
    tx_code, tx_label, tx_note = _resolve_transaction_type(
        bill_type, bill_obj, entry_hint_id=source.get('entry_hint_id'))
    from app.services.bilingual import urdu_tx_label, urdu_tx_note
    tx_label = urdu_tx_label(tx_code, tx_label)
    tx_note = urdu_tx_note(tx_note)
    return bill_urdu.build_document(
        bill_type=bill_type,
        bill=bill_obj,
        items=prepared['items'],
        client=source['client'],
        previous_balance=source['previous_balance'],
        client_balance=source['client_balance'],
        delivery_people=prepared['delivery_people'],
        driver_name_ur=prepared['driver_name_ur'],
        client_name_ur=prepared['client_name_ur'],
        transaction_label=tx_label,
        transaction_note=tx_note,
        settings=Settings.query.first(),
        terms=prepared['terms'],
        grn_bd=source['grn_bd'],
        party_label='',
        booked_summary=prepared['booked_summary'],
    )


@bp.route('/download_invoice/<path:bill_no>')
@login_required
def download_invoice(bill_no):
    source = bill_document_source(
        bill_no,
        hint_type=request.args.get('src'),
        hint_id=request.args.get('src_id'),
        hint_client_code=request.args.get('client_code'),
        hint_client_name=request.args.get('client_name'),
        hint_entry_id=request.args.get('entry_id'),
    )
    if source.get('ambiguous'):
        flash('Multiple records match this bill reference. Please choose the '
              'exact document type first.', 'warning')
        return redirect(url_for('view_bill', bill_no=bill_no))
    if not source.get('bill'):
        flash('Bill not found for download', 'danger')
        return redirect(url_for('index'))
    bill_obj = source['bill']
    bill_type = source['bill_type']
    items = source['items']
    client = source['client']
    client_balance = source['client_balance']
    previous_balance = source['previous_balance']
    delivery_people = source['delivery_people']
    grn_bd = source['grn_bd']
    recent_deliveries = source['recent_deliveries']
    material_ledger_recent = source['material_ledger_recent']
    material_stock_summary = source['material_stock_summary']
    direct_sale_rent_reconciliation = source['direct_sale_rent_reconciliation']
    entry_hint_id = request.args.get('entry_id')

    action = (request.args.get('action') or 'download').lower()
    disposition = 'inline' if action in ['print', 'view'] else 'attachment'
    # The approved Urdu layout is a single A5 sheet by default.  ?copies=2
    # asks for the carbon-copy leaf (original + office copy on one A4).
    copies = 2 if (request.args.get('copies') or '').strip() == '2' else 1
    # Bills are single-language documents: ?lang=en (default) or ?lang=ur.
    lang = (request.args.get('lang') or 'en').strip().lower()
    if lang in ('ur', 'urdu', 'urd'):
        lang = 'ur'
    else:
        lang = 'en'

    # Urdu receipts ask before printing/downloading whether terms and
    # conditions should be shown, and which of them.  A direct link without
    # the answer lands on a small confirmation screen first; the bill view
    # page opens the same dialog as a modal so the normal flow is one click.
    show_terms_ur = False
    selected_terms_ur = []
    sections_ur = {key: True for key in _URDU_SECTION_KEYS}
    alt_mode_ur = 'both'
    if lang == 'ur' and 'tc' not in request.args:
        return render_template(
            'urdu_terms_confirm.html',
            bill_no=bill_no,
            action=action,
            copies=copies,
            src=request.args.get('src'),
            src_id=request.args.get('src_id'),
            client_code=request.args.get('client_code'),
            client_name=request.args.get('client_name'),
            entry_id=request.args.get('entry_id'),
        )

    if lang == 'ur':
        tc_raw = (request.args.get('tc') or '').strip().lower()
        want_tc = tc_raw in ('1', 'yes', 'true', 'on')
        tc_ids = []
        for raw in ','.join(request.args.getlist('tc_ids')).split(','):
            raw = raw.strip()
            if raw:
                try:
                    tc_ids.append(int(raw))
                except ValueError:
                    pass
        if want_tc:
            show_terms_ur = True
            selected_terms_ur = get_selected_terms(tc_ids)
        sections_ur = _urdu_section_flags(request.args)
        alt_mode_ur = _urdu_alt_name_mode(request.args)

    # Prefer saved Urdu names; otherwise use a best-effort transliteration.
    # Shared with the scanned-bill page so both show the same Urdu names.
    prepared = urdu_bill_data(source, show_terms=show_terms_ur,
                              terms=selected_terms_ur, alt_mode=alt_mode_ur)
    items = prepared['items']
    client_name_ur = prepared['client_name_ur']
    driver_name_ur = prepared['driver_name_ur']
    delivery_people = prepared['delivery_people']
    tx_code, tx_label, tx_note = _resolve_transaction_type(
        bill_type, bill_obj, entry_hint_id=source.get('entry_hint_id'))
    if lang == 'ur':
        from app.services.bilingual import urdu_tx_label, urdu_tx_note
        tx_label = urdu_tx_label(tx_code, tx_label)
        tx_note = urdu_tx_note(tx_note)
        # An Urdu bill is its own document (app/services/urdu_receipt.py), not
        # the English receipt with swapped labels: it has its own sheet,
        # header, meta strip, panel, table, summary and terms.  It is also a
        # single approved A5 sheet by default; ?copies=2 lays the same receipt
        # out twice on one A4 leaf.  Both "Urdu PDF" and "Urdu Print" return it
        # here.
        return _urdu_bill_response(
            bill_type=bill_type,
            bill=bill_obj,
            items=items,
            client=client,
            previous_balance=previous_balance,
            client_balance=client_balance,
            delivery_people=delivery_people,
            driver_name_ur=driver_name_ur,
            client_name_ur=client_name_ur,
            transaction_label=tx_label,
            transaction_note=tx_note,
            terms=selected_terms_ur,
            grn_bd=grn_bd,
            action=action,
            disposition=disposition,
            copies=copies,
            sections=sections_ur,
            booked_summary=prepared['booked_summary'],
        )
    section_map = {
        'Booking': 'BOOKING',
        'Payment': 'PAYMENT',
        'DirectSale': 'DIRECTSALE',
        'Invoice': 'INVOICE',
        'GRN': 'GRN',
        'PendingBill': 'PENDINGBILL'
    }
    section = section_map.get(bill_type, 'BILL')
    template_name = 'view_bill.html'
    if bill_type == 'Payment':
        template_name = 'payment_receipt.html'

    rendered = render_template(
        template_name,
        bill=bill_obj,
        type=bill_type,
        grn_bd=grn_bd,
        items=items,
        client=client,
        client_balance=client_balance,
        previous_balance=previous_balance,
        recent_deliveries=recent_deliveries,
        material_ledger_recent=material_ledger_recent,
        material_stock_summary=material_stock_summary,
        transaction_type_code=tx_code,
        transaction_type_label=tx_label,
        transaction_type_note=tx_note,
        direct_sale_rent_reconciliation=direct_sale_rent_reconciliation,
        delivery_people=delivery_people,
        client_name_ur=client_name_ur,
        driver_name_ur=driver_name_ur,
        pk_now=pk_now,
        auto_print=(action == 'print'),
        bill_lang=lang,
        show_terms_ur=show_terms_ur,
        selected_terms_ur=selected_terms_ur,
    )

    # Keep the two language downloads distinguishable on disk.  The suffix is
    # added after _download_filename() because that helper strips hyphens from
    # the section name.
    def _lang_name(ext):
        base = _download_filename(section, ext)
        stem, _, suffix = base.rpartition('.')
        return f"{stem}-{'UR' if lang == 'ur' else 'EN'}.{suffix}"

    if action != 'print':
        pdf_response = _try_render_weasy_pdf(
            rendered,
            _lang_name('pdf'),
            disposition=disposition,
            lang=lang
        )
        if pdf_response:
            return pdf_response

    if action == 'print':
        # Browser printing gets the same single-language layer as the PDF.
        rendered = _bilingual_print_html(rendered, lang=lang)

    response = make_response(rendered)
    fallback_name = _lang_name('html')
    response.headers['Content-Disposition'] = f'{disposition}; filename={fallback_name}'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


# ---------------------------------------------------------------------------
# Urdu bill
# ---------------------------------------------------------------------------
# The Urdu bill is a fixed sheet defined in ``app/services/urdu_receipt.py``;
# this route only collects the bill's data and hands it over.  Keeping the
# document out of ``view_bill.html`` is deliberate: the Urdu sheet must not
# inherit the English receipt's layout (or its black rules, ``INVOICE`` badge
# and English table header) when a label is translated.  The sheet is composed
# onto paper here too: ``copies=1`` (default) prints the single approved A5
# sheet, ``copies=2`` lays the receipt out twice on one A4 landscape leaf.

_URDU_SECTION = {
    'Booking': 'BOOKING',
    'Payment': 'PAYMENT',
    'DirectSale': 'DIRECTSALE',
    'Invoice': 'INVOICE',
    'GRN': 'GRN',
    'PendingBill': 'PENDINGBILL',
}


def _urdu_bill_response(*, bill_type, bill, items, client, previous_balance,
                        client_balance, delivery_people, driver_name_ur,
                        client_name_ur, transaction_label, transaction_note,
                        terms, grn_bd, action, disposition, copies=1,
                        sections=None, booked_summary=None):
    """Return the Urdu bill as a PDF (download) or as the print page.

    ``copies`` is the carbon-copy switch: 1 (the default) prints the single
    approved A5 sheet; 2 prints the same receipt twice on one A4 landscape
    leaf - original for the customer on the right, office copy on the left.
    ``sections`` switches off optional parts of the sheet (QR, shop note,
    previous balance, signature, delivery); anything absent stays on.
    """
    from app.services import bill_urdu
    from app.services.files_pdf import _safe_download_name
    from app.services.time_money import pk_now

    settings_obj = Settings.query.first()
    client_name = getattr(bill, 'client_name', '') or (
        getattr(client, 'name', '') if client is not None else '')
    party_label = ''

    document = bill_urdu.build_document(
        bill_type=bill_type,
        bill=bill,
        items=items,
        client=client,
        previous_balance=previous_balance,
        client_balance=client_balance,
        delivery_people=delivery_people,
        driver_name_ur=driver_name_ur,
        client_name_ur=client_name_ur,
        transaction_label=transaction_label,
        transaction_note=transaction_note,
        settings=settings_obj,
        terms=terms,
        grn_bd=grn_bd,
        party_label=party_label,
        sections=sections,
        booked_summary=booked_summary,
    )

    stamp = pk_now().strftime('%d-%m-%Y_%I-%M%p')
    section = _URDU_SECTION.get(bill_type, 'BILL')
    title = f"Urdu {bill_type or 'Bill'} - {document.get('bill_no') or ''}".strip()

    if action == 'print':
        html = bill_urdu.render_html(document, title=title,
                                     auto_print=False, copies=copies)
        response = make_response(html)
        name = _safe_download_name(f'{section}-{stamp}-UR.html', 'bill-ur.html')
        response.headers['Content-Disposition'] = f'{disposition}; filename={name}'
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response

    data = bill_urdu.render_pdf(document, title=title, copies=copies)
    if not data:
        # ReportLab is a hard dependency of the PDF path; if it is missing the
        # printable page is still a correct Urdu bill, so serve that instead of
        # an error.
        html = bill_urdu.render_html(document, title=title, copies=copies)
        response = make_response(html)
        name = _safe_download_name(f'{section}-{stamp}-UR.html', 'bill-ur.html')
        response.headers['Content-Disposition'] = f'{disposition}; filename={name}'
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        return response

    name = _safe_download_name(f'{section}-{stamp}-UR.pdf', 'bill-ur.pdf')
    response = make_response(data)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Length'] = str(len(data))
    response.headers['Content-Disposition'] = f'{disposition}; filename={name}'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
