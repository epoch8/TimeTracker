"""E8-006: an invoice prints and returns its own currency.

Invoice PDFs printed Settings.currency for every invoice, so a EUR invoice came out as "USD" when the
global setting was USD. The API accepted currency_code but did not return it. See design-docs/005.
"""

import io

import pytest

from app import db
from app.models import Settings

pytestmark = [pytest.mark.integration]


@pytest.fixture
def eur_invoice(app, invoice_with_items):
    invoice, _items = invoice_with_items
    settings = Settings.get_settings()
    settings.currency = "USD"
    invoice.currency_code = "EUR"
    db.session.commit()
    return invoice


def test_main_generator_uses_invoice_currency(app, eur_invoice):
    from app.utils.pdf_generator import InvoicePDFGenerator

    gen = InvoicePDFGenerator(eur_invoice, settings=Settings.get_settings())
    assert gen._format_currency(1234.5) == "1,234.50 EUR"


def test_fallback_generator_uses_invoice_currency(app, eur_invoice):
    from app.utils.pdf_generator_fallback import InvoicePDFGeneratorFallback

    gen = InvoicePDFGeneratorFallback(eur_invoice, settings=Settings.get_settings())
    assert gen._format_currency(1234.5) == "1,234.50 EUR"


def test_generator_falls_back_to_settings_currency(app, eur_invoice):
    from app.utils.pdf_generator import InvoicePDFGenerator

    class NoCurrency:
        currency_code = None

    gen = InvoicePDFGenerator(eur_invoice, settings=Settings.get_settings())
    gen.invoice = NoCurrency()
    assert gen._format_currency(1) == "1.00 USD"


def test_fallback_pdf_text_has_invoice_currency(app, eur_invoice):
    from pypdf import PdfReader

    from app.utils.pdf_generator_fallback import InvoicePDFGeneratorFallback

    pdf = InvoicePDFGeneratorFallback(eur_invoice, settings=Settings.get_settings()).generate_pdf()
    text = "".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "EUR" in text
    assert "USD" not in text


@pytest.fixture
def invoices_client(app, user):
    from app.models import ApiToken

    token, plain = ApiToken.create_token(user_id=user.id, name="invoices", scopes="read:invoices", expires_days=1)
    db.session.add(token)
    db.session.commit()
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {plain}"
    return c


def test_api_returns_invoice_currency(app, invoices_client, eur_invoice):
    resp = invoices_client.get(f"/api/v1/invoices/{eur_invoice.id}")
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    data = data.get("invoice", data)
    assert data["currency_code"] == "EUR"


def test_to_dict_has_currency(app, eur_invoice):
    assert eur_invoice.to_dict()["currency_code"] == "EUR"


# --- Part B: choosing the currency in the web forms -------------------------------------------------


def _create_form(project, test_client, **extra):
    from datetime import date, timedelta

    data = {
        "project_id": project.id,
        "client_name": test_client.name,
        "due_date": (date.today() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "tax_rate": "0",
    }
    data.update(extra)
    return data


def _usd_settings():
    settings = Settings.get_settings()
    settings.currency = "USD"
    db.session.commit()


def _newest_invoice():
    from app.models import Invoice

    return Invoice.query.order_by(Invoice.id.desc()).first()


def test_create_form_offers_currency_select(app, authenticated_client, project):
    _usd_settings()
    resp = authenticated_client.get("/invoices/create")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="currency_code"' in html
    assert 'data-currency="USD"' in html  # project option carries its client's default currency


def test_create_uses_chosen_currency(app, authenticated_client, project, test_client):
    _usd_settings()
    resp = authenticated_client.post("/invoices/create", data=_create_form(project, test_client, currency_code="eur"))
    assert resp.status_code in (200, 302)
    assert _newest_invoice().currency_code == "EUR"


def test_create_defaults_to_clients_latest_invoice_currency(app, authenticated_client, project, test_client, invoice):
    _usd_settings()
    invoice.currency_code = "RUB"
    db.session.commit()
    authenticated_client.post("/invoices/create", data=_create_form(project, test_client))
    newest = _newest_invoice()
    assert newest.id != invoice.id
    assert newest.currency_code == "RUB"


def test_create_without_history_uses_settings_currency(app, authenticated_client, project, test_client):
    _usd_settings()
    authenticated_client.post("/invoices/create", data=_create_form(project, test_client, currency_code="bad!"))
    assert _newest_invoice().currency_code == "USD"


def test_edit_draft_changes_currency(app, authenticated_client, invoice):
    _usd_settings()
    resp = authenticated_client.get(f"/invoices/{invoice.id}/edit")
    assert 'name="currency_code"' in resp.get_data(as_text=True)
    authenticated_client.post(
        f"/invoices/{invoice.id}/edit",
        data={
            "client_name": invoice.client_name,
            "due_date": invoice.due_date.strftime("%Y-%m-%d"),
            "tax_rate": "0",
            "currency_code": "GEL",
        },
    )
    db.session.refresh(invoice)
    assert invoice.currency_code == "GEL"


def test_edit_sent_invoice_keeps_currency(app, authenticated_client, invoice):
    invoice.status = "sent"
    invoice.currency_code = "EUR"
    db.session.commit()
    resp = authenticated_client.get(f"/invoices/{invoice.id}/edit")
    assert 'name="currency_code"' not in resp.get_data(as_text=True)
    authenticated_client.post(
        f"/invoices/{invoice.id}/edit",
        data={"due_date": invoice.due_date.strftime("%Y-%m-%d"), "notes": "x", "currency_code": "USD"},
    )
    db.session.refresh(invoice)
    assert invoice.currency_code == "EUR"


def test_currency_helpers(app, invoice):
    from app.utils.invoice_currency import (
        currency_choices,
        default_currencies_by_client,
        default_currency_for_client,
        normalize_currency_code,
    )

    assert normalize_currency_code(" eur ") == "EUR"
    assert normalize_currency_code("euro") is None
    invoice.currency_code = "RUB"
    db.session.commit()
    settings = Settings.get_settings()
    assert default_currency_for_client(invoice.client_id, settings) == "RUB"
    assert default_currencies_by_client(settings)[invoice.client_id] == "RUB"
    assert "RUB" in currency_choices(settings)


# --- E8-009: the "Add Item" row must not be a grid itself ------------------------------------------


def test_e8_009_added_item_row_is_not_a_grid(app, authenticated_client, invoice):
    import re

    html = authenticated_client.get(f"/invoices/{invoice.id}/edit").get_data(as_text=True)
    outer = re.search(r"row\.className = '([^']*invoice-item-row[^']*)'", html).group(1)
    assert "grid" not in outer.split()  # a grid here gives its two children one column of twelve each
