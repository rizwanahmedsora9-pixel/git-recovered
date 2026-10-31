"""Gross booking material valuation for the decision ledger."""

def _material_values(items, delivered_qty, allocated_by_item):
    """Value deliveries at their booking rates; estimate unallocated legacy rows FIFO.

    Quantities without a booking rate are left unpriced rather than inventing one.
    Values are gross line prices, before discounts.
    """
    booked_value = sum(float(i.qty or 0) * float(i.price_at_time or 0) for i in items)
    remaining_delivery = max(0.0, delivered_qty)
    delivered_value = 0.0
    used = {}
    for item in items:
        qty = min(max(0.0, allocated_by_item.get(item.id, 0)), remaining_delivery)
        used[item.id] = qty
        delivered_value += qty * float(item.price_at_time or 0)
        remaining_delivery -= qty
    # Older dispatches may not have allocation records. Use remaining booking lots FIFO.
    for item in items:
        qty = min(max(0.0, float(item.qty or 0) - used.get(item.id, 0)), remaining_delivery)
        delivered_value += qty * float(item.price_at_time or 0)
        remaining_delivery -= qty
    return booked_value, delivered_value, booked_value - delivered_value, remaining_delivery


