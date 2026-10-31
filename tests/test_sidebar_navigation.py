"""Tests for the smart collapsible sidebar navigation in layout.html."""
from __future__ import annotations

from html.parser import HTMLParser
from werkzeug.security import generate_password_hash

from models import User, db


class _SidebarParser(HTMLParser):
    """Extract sidebar top-level links and collapsible category groups."""

    def __init__(self):
        super().__init__()
        self.in_nav = False
        self.nav_depth = 0
        self.current_group_id = None
        self.submenu_depth = 0
        self.capture_btn = None
        self.capture_link = None
        self.groups = []
        self.group_by_id = {}
        self.top_links = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls = set((attr.get("class") or "").split())

        if tag == "nav" and attr.get("aria-label") == "Primary Navigation":
            self.in_nav = True
            self.nav_depth = 1
            return

        if not self.in_nav:
            return

        if tag == "nav":
            self.nav_depth += 1

        if tag == "button" and "nav-toggle-btn" in cls:
            gid = attr.get("aria-controls") or ""
            grp = {
                "id": gid,
                "label": "",
                "aria_expanded": attr.get("aria-expanded"),
                "is_open": "is-open" in cls,
                "has_active": "has-active" in cls,
                "submenu_show": False,
                "items": [],
            }
            self.groups.append(grp)
            self.group_by_id[gid] = grp
            self.capture_btn = grp
            return

        if tag == "div" and "nav-submenu" in cls:
            gid = attr.get("id") or ""
            self.current_group_id = gid
            self.submenu_depth = 1
            if gid in self.group_by_id:
                self.group_by_id[gid]["submenu_show"] = "show" in cls
            return

        if self.current_group_id and tag == "div":
            self.submenu_depth += 1

        if tag == "a" and "nav-link" in cls:
            item = {
                "href": attr.get("href") or "",
                "label": "",
                "active": "active" in cls,
            }
            self.capture_link = item
            if self.current_group_id and self.current_group_id in self.group_by_id:
                self.group_by_id[self.current_group_id]["items"].append(item)
            else:
                self.top_links.append(item)

    def handle_data(self, data):
        if self.capture_btn is not None:
            self.capture_btn["label"] += data
        if self.capture_link is not None:
            self.capture_link["label"] += data

    def handle_endtag(self, tag):
        if not self.in_nav:
            return
        if tag == "button" and self.capture_btn is not None:
            self.capture_btn["label"] = " ".join(self.capture_btn["label"].split())
            self.capture_btn = None
        elif tag == "a" and self.capture_link is not None:
            self.capture_link["label"] = " ".join(self.capture_link["label"].split())
            self.capture_link = None
        elif tag == "div" and self.current_group_id:
            self.submenu_depth -= 1
            if self.submenu_depth <= 0:
                self.current_group_id = None
                self.submenu_depth = 0
        elif tag == "nav":
            self.nav_depth -= 1
            if self.nav_depth <= 0:
                self.in_nav = False


def _parse_sidebar(html: str) -> _SidebarParser:
    parser = _SidebarParser()
    parser.feed(html)
    return parser


def _login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_admin_sidebar_categories_and_item_grouping(client):
    _login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    parsed = _parse_sidebar(resp.get_data(as_text=True))

    assert [lnk["label"] for lnk in parsed.top_links] == ["Dashboard"]
    assert parsed.top_links[0]["active"] is True

    category_labels = [g["label"] for g in parsed.groups]
    assert category_labels == [
        "Inventory",
        "Transactions",
        "Accounts",
        "Ledger",
        "Directory",
        "System",
    ]

    # On Dashboard (/), every collapsible category starts collapsed until needed.
    for grp in parsed.groups:
        assert grp["aria_expanded"] == "false", grp
        assert grp["submenu_show"] is False, grp

    by_label = {g["label"]: [it["label"] for it in g["items"]] for g in parsed.groups}
    assert by_label["Inventory"] == [
        "Stock Summary",
        "Daily Breakdown",
        "History",
    ]
    assert by_label["Transactions"] == [
        "GRN (Receiving)",
        "Bookings",
        "Sales",
        "Material Return",
        "Pending Bills",
        "Delivery Rent",
    ]
    assert by_label["Accounts"] == [
        "Payments",
        "Accounts",
        "Cash Flow",
        "Daily Reconciliation",
        "Financial Tracker",
        "Pay Supplier",
        "Profit Reports",
    ]
    assert by_label["Ledger"] == [
        "Supplier Ledger",
        "Client Ledger",
        "Client Categories",
        "Delivery Person Ledger",
        "Decision Ledger",
        "Current Payables",
        "Notifications",
    ]
    assert by_label["Directory"] == [
        "Material Brands",
        "Services & Charges",
        "Delivery Persons",
    ]
    assert by_label["System"] == [
        "Import & Export",
        "Legacy Data Migration",
        "Settings",
    ]


def test_only_active_category_auto_expands_per_route(client):
    _login(client)
    cases = [
        ("/inventory/stock_summary", "Inventory", "Stock Summary"),
        ("/direct_sales", "Transactions", "Sales"),
        ("/cash_flow", "Accounts", "Cash Flow"),
        ("/daily_reconciliation", "Accounts", "Daily Reconciliation"),
        ("/accounts/payments/suppliers", "Accounts", "Pay Supplier"),
        ("/suppliers", "Ledger", "Supplier Ledger"),
        ("/clients", "Ledger", "Client Ledger"),
        ("/delivery_persons", "Ledger", "Delivery Person Ledger"),
        ("/current_payables", "Ledger", "Current Payables"),
        ("/materials", "Directory", "Material Brands"),
        ("/delivery_persons?src=dir", "Directory", "Delivery Persons"),
        ("/settings", "System", "Settings"),
    ]
    for path, expected_group, expected_item in cases:
        resp = client.get(path)
        assert resp.status_code == 200, path
        parsed = _parse_sidebar(resp.get_data(as_text=True))
        open_groups = [g["label"] for g in parsed.groups if g["submenu_show"]]
        assert open_groups == [expected_group], (path, open_groups)

        grp = next(g for g in parsed.groups if g["label"] == expected_group)
        assert grp["aria_expanded"] == "true", path
        active_items = [it["label"] for it in grp["items"] if it["active"]]
        assert active_items == [expected_item], (path, active_items)


def test_restricted_user_hides_unauthorized_categories_and_links(app, client):
    with app.app_context():
        clerk = User(
            username="sidebar_clerk",
            password_hash=generate_password_hash("Clerk@12345"),
            role="user",
            status="active",
        )
        db.session.add(clerk)
        db.session.commit()

    _login(client, "sidebar_clerk", "Clerk@12345")
    resp = client.get("/")
    assert resp.status_code == 200
    parsed = _parse_sidebar(resp.get_data(as_text=True))

    labels = [g["label"] for g in parsed.groups]
    assert "Directory" not in labels
    assert "System" not in labels

    accounts_items = [
        it["label"]
        for g in parsed.groups
        if g["label"] == "Accounts"
        for it in g["items"]
    ]
    assert "Payments" in accounts_items
    assert "Cash Flow" not in accounts_items
    assert "Daily Reconciliation" not in accounts_items
