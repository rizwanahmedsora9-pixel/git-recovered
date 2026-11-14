"""bills — split from sales.py."""
from ._common import *  # noqa
from ._bills_download_invoice import _direct_sale_bill_items

@bp.route('/view_bill_detail/<string:type>/<int:id>')
@login_required
def view_bill_detail(type, id):
    bill = None
    items = []
    if type == 'Booking':
        bill = Booking.query.get_or_404(id)
        items = bill.items
    elif type == 'Payment':
        bill = Payment.query.get_or_404(id)
    elif type == 'DirectSale':
        bill = DirectSale.query.get_or_404(id)
        if not (getattr(bill, 'note', None) or '').strip():
            if getattr(bill, 'invoice', None) and (getattr(bill.invoice, 'note', None) or '').strip():
                bill.note = bill.invoice.note.strip()
        items = _direct_sale_bill_items(bill)
    elif type == 'MaterialReturn':
        bill = MaterialReturn.query.get_or_404(id)
        items = bill.items
    else:
        return "Invalid Bill Type", 400

    all_clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    all_materials = Material.query.order_by(Material.name.asc()).all()
    return render_template('view_bill.html', bill=bill, type=type, items=items, clients=all_clients, materials=all_materials, pk_now=pk_now)

