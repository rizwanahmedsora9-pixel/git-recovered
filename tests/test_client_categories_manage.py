"""Client ledger category master: create / rename / deactivate / delete."""
from __future__ import annotations

import pytest

from models import db, Client, ClientCategory


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _rows(app):
    with app.app_context():
        return {c.name: c for c in ClientCategory.query.all()}


def _client_category(app, code):
    with app.app_context():
        return Client.query.filter_by(code=code).one().category


def _add(client, name):
    return client.post(
        "/client-ledger/categories/add",
        data={"name": name},
        follow_redirects=True,
    )


def test_category_master_page_lists_general(app, client):
    login(client)
    resp = client.get("/client-ledger/categories")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Client Ledger Categories" in body
    assert "General" in body
    # The client pages must expose the entry point now.
    clients_page = client.get("/clients").get_data(as_text=True)
    assert "/client-ledger/categories" in clients_page
    assert "Manage Categories" in clients_page


def test_create_rename_and_use_custom_category(app, client):
    login(client)

    resp = _add(client, "Contractor")
    assert resp.status_code == 200
    assert "added" in resp.get_data(as_text=True)
    assert "Contractor" in _rows(app)

    # Duplicate (case/space insensitive) must not create a second row.
    resp = _add(client, "  contractor ")
    assert "already exists" in resp.get_data(as_text=True)
    with app.app_context():
        assert ClientCategory.query.filter(
            ClientCategory.name.ilike("contractor")
        ).count() == 1

    # A client can now be registered against the user-defined category.
    resp = client.post(
        "/add_client",
        data={"name": "ACME BUILDERS", "code": "ACM-001", "category": "Contractor"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert _client_category(app, "ACM-001") == "Contractor"

    # Renaming the master renames the stored client category string too.
    with app.app_context():
        cat_id = ClientCategory.query.filter_by(name="Contractor").one().id
    resp = client.post(
        f"/client-ledger/categories/{cat_id}/rename",
        data={"name": "Contractors"},
        follow_redirects=True,
    )
    assert "renamed" in resp.get_data(as_text=True)
    assert _client_category(app, "ACM-001") == "Contractors"
    assert "Contractors" in _rows(app) and "Contractor" not in _rows(app)


def test_general_category_is_protected(app, client):
    login(client)
    client.get("/client-ledger/categories")  # ensure master rows exist
    with app.app_context():
        general_id = ClientCategory.query.filter_by(name="General").one().id

    resp = client.post(
        f"/client-ledger/categories/{general_id}/toggle", follow_redirects=True
    )
    assert "cannot be deactivated" in resp.get_data(as_text=True)
    resp = client.post(
        f"/client-ledger/categories/{general_id}/delete", follow_redirects=True
    )
    assert "cannot be deleted" in resp.get_data(as_text=True)

    with app.app_context():
        general = db.session.get(ClientCategory, general_id)
        assert general is not None and general.is_active


def test_in_use_category_cannot_be_deactivated_or_deleted(app, client):
    login(client)
    _add(client, "Retail")
    client.post(
        "/add_client",
        data={"name": "RETAIL ONE", "code": "RET-001", "category": "Retail"},
        follow_redirects=True,
    )
    with app.app_context():
        cat_id = ClientCategory.query.filter_by(name="Retail").one().id

    resp = client.post(
        f"/client-ledger/categories/{cat_id}/toggle", follow_redirects=True
    )
    assert "used by 1 client" in resp.get_data(as_text=True)
    resp = client.post(
        f"/client-ledger/categories/{cat_id}/delete", follow_redirects=True
    )
    assert "cannot be deleted" in resp.get_data(as_text=True)

    with app.app_context():
        cat = db.session.get(ClientCategory, cat_id)
        assert cat is not None and cat.is_active


def test_unused_category_can_be_deactivated_and_deleted(app, client):
    login(client)
    _add(client, "Seasonal")
    with app.app_context():
        cat_id = ClientCategory.query.filter_by(name="Seasonal").one().id

    resp = client.post(
        f"/client-ledger/categories/{cat_id}/toggle", follow_redirects=True
    )
    assert "deactivated" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(ClientCategory, cat_id).is_active is False

    # Re-adding a deactivated name reactivates the existing row.
    resp = _add(client, "Seasonal")
    assert "re-activated" in resp.get_data(as_text=True)
    with app.app_context():
        assert ClientCategory.query.filter_by(name="Seasonal").count() == 1
        assert ClientCategory.query.filter_by(name="Seasonal").one().is_active

    # Deactivate again and delete the (unused) row.
    client.post(f"/client-ledger/categories/{cat_id}/toggle", follow_redirects=True)
    resp = client.post(
        f"/client-ledger/categories/{cat_id}/delete", follow_redirects=True
    )
    assert "deleted" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(ClientCategory, cat_id) is None


def test_category_names_are_validated(app, client):
    login(client)
    resp = _add(client, "   ")
    assert "required" in resp.get_data(as_text=True)
    resp = _add(client, "X" * 51)
    assert "50 characters" in resp.get_data(as_text=True)
