from types import SimpleNamespace

from app.services.decision_ledger import _material_values


def lot(id, qty, rate):
    return SimpleNamespace(id=id, qty=qty, price_at_time=rate)


def test_booking_lots_keep_their_own_rates_and_allocations():
    items = [lot(1, 10, 100), lot(2, 5, 200)]
    assert _material_values(items, 4, {2: 4}) == (2000, 800, 1200, 0)


def test_legacy_deliveries_use_oldest_remaining_lots():
    items = [lot(1, 10, 100), lot(2, 5, 200)]
    assert _material_values(items, 12, {2: 2}) == (2000, 1400, 600, 0)


def test_excess_deliveries_are_not_given_an_invented_price():
    assert _material_values([lot(1, 2.5, 10)], 3, {}) == (25, 25, 0, 0.5)


def test_decision_ledger_shows_values_and_fractional_quantities(app, client):
    from models import Booking, BookingItem, Client, Entry, db

    with app.app_context():
        db.session.add(Client(name='Ledger Client', code='LED-1', is_active=True))
        booking = Booking(client_name='Ledger Client', is_void=False)
        booking.items = [BookingItem(material_name='Cement', qty=2.5, price_at_time=100)]
        db.session.add(booking)
        db.session.add(Entry(type='OUT', client='Ledger Client', client_code='LED-1',
                             material='Cement', qty=1.5, is_void=False))
        db.session.commit()
    client.post('/login', data={'username': 'Admin', 'password': 'Admin@fbm12345'})
    response = client.get('/decision_ledger')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Booked value' in html
    assert '250.00' in html and '150.00' in html and '100.00' in html
    assert '2.5' in html and '1.5' in html
