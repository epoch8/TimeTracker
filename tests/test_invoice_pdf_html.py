"""HTML invoice template renderer (app/utils/invoice_pdf_html.py)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app.utils import invoice_pdf_html as ipdf


def _invoice(items=None, **overrides):
    client = NS(name="Acme Retail GmbH", vat_id="DE123456789", address=None, email=None, custom_fields={})
    items = items or [
        NS(
            id=1,
            position=0,
            description="Consulting\n\nFor period 01.12.2026-31.12.2026",
            quantity=Decimal("1"),
            unit_price=Decimal("16666.66"),
            total_amount=Decimal("16666.66"),
        ),
    ]
    data = dict(
        invoice_number="NW-2027-001",
        issue_date=date(2027, 1, 1),
        due_date=date(2027, 1, 31),
        client=client,
        client_name="Acme Retail GmbH",
        client_address="Hauptstrasse 5\n10115 Berlin",
        client_email="billing@acme.example",
        items=items,
        extra_goods=[],
        expenses=[],
        subtotal=sum(i.total_amount for i in items),
        tax_rate=Decimal("0"),
        tax_amount=Decimal("0"),
        discount_amount=Decimal("0"),
        total_amount=sum(i.total_amount for i in items),
        currency_code="EUR",
        notes="services under the agreement of July 9, 2025",
        terms="Bank charges are paid by the sender.",
    )
    data.update(overrides)
    return NS(**data)


def _settings():
    return NS(
        company_name="Northwind Engineering LLC",
        company_tax_id="400000000",
        company_address="12 Harbor Street\nTbilisi",
        company_email="",
        company_phone="",
        company_website="",
        company_bank_info="Bank of Example\nIBAN GE00XX0000000000000000",
        currency="EUR",
        invoice_terms="",
    )


@pytest.mark.unit
def test_format_amount_uses_space_thousands():
    assert ipdf.format_amount(Decimal("16666.66")) == "16 666.66"
    assert ipdf.format_amount(0) == "0.00"
    assert ipdf.format_amount(None) == "0.00"


@pytest.mark.unit
def test_format_quantity_drops_trailing_zeros():
    assert ipdf.format_quantity(Decimal("96.00")) == "96"
    assert ipdf.format_quantity(Decimal("24.50")) == "24.5"


@pytest.mark.unit
def test_text_helpers_escape_and_keep_breaks():
    assert str(ipdf.text_lines("a <b>\nc")) == "a &lt;b&gt;<br>c"
    assert str(ipdf.text_paragraphs("one\n\ntwo\nthree")) == "<p>one</p><p>two<br>three</p>"


@pytest.mark.unit
def test_resolve_template_name_rejects_unknown_and_paths(app):
    with app.app_context():
        assert "contract" in ipdf.available_templates()
        assert ipdf.resolve_template_name("contract") == "contract"
        assert ipdf.resolve_template_name("../../base") == ipdf.DEFAULT_TEMPLATE
        assert ipdf.resolve_template_name("does-not-exist") == ipdf.DEFAULT_TEMPLATE


@pytest.mark.unit
def test_renderer_is_off_by_default(app):
    with app.app_context():
        app.config.pop("INVOICE_PDF_RENDERER", None)
        assert ipdf.html_renderer_enabled() is False
        app.config["INVOICE_PDF_RENDERER"] = "html"
        assert ipdf.html_renderer_enabled() is True


@pytest.mark.unit
def test_contract_template_renders_parties_and_totals(app):
    with app.test_request_context():
        html = ipdf.render_invoice_html(_invoice(), settings=_settings(), template_name="contract")
    assert "Invoice № NW-2027-001" in html
    assert "Acme Retail GmbH" in html and "tax number DE123456789" in html
    assert "Northwind Engineering LLC" in html and "Identification Number: 400000000" in html
    assert "Payment purpose: services under the agreement of July 9, 2025" in html
    assert "EUR 16 666.66" in html
    assert "01/01/2027" in html
    # one fixed-fee line: no hours / rate columns
    assert ">Hours<" not in html


@pytest.mark.unit
def test_contract_template_shows_hours_for_hourly_lines(app):
    items = [
        NS(
            id=1,
            position=0,
            description="ML engineering",
            quantity=Decimal("96"),
            unit_price=Decimal("70"),
            total_amount=Decimal("6720"),
        ),
        NS(
            id=2,
            position=1,
            description="Review",
            quantity=Decimal("24.5"),
            unit_price=Decimal("45"),
            total_amount=Decimal("1102.50"),
        ),
    ]
    with app.test_request_context():
        html = ipdf.render_invoice_html(_invoice(items=items), settings=_settings())
    assert ">Hours<" in html
    assert ">24.5<" in html
    assert "6 720.00" in html


@pytest.mark.unit
def test_render_invoice_pdf_returns_pdf_bytes(app):
    pytest.importorskip("weasyprint")
    with app.test_request_context():
        pdf = ipdf.render_invoice_pdf(_invoice(), settings=_settings())
    assert pdf[:5] == b"%PDF-"


@pytest.mark.integration
def test_preview_route_renders_real_invoice(authenticated_client, invoice_with_items):
    invoice, _items = invoice_with_items
    resp = authenticated_client.get(f"/invoices/{invoice.id}/pdf-html")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f"Invoice № {invoice.invoice_number}" in body
    assert "Development work" in body and "Testing work" in body
    assert ">Hours<" in body


@pytest.mark.integration
def test_preview_route_downloads_pdf(authenticated_client, invoice_with_items):
    pytest.importorskip("weasyprint")
    invoice, _items = invoice_with_items
    resp = authenticated_client.get(f"/invoices/{invoice.id}/pdf-html?download=1")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:5] == b"%PDF-"


@pytest.mark.integration
def test_export_uses_html_renderer_when_switched_on(app, authenticated_client, invoice_with_items):
    pytest.importorskip("weasyprint")
    invoice, _items = invoice_with_items
    app.config["INVOICE_PDF_RENDERER"] = "html"
    try:
        resp = authenticated_client.get(f"/invoices/{invoice.id}/export/pdf")
    finally:
        app.config["INVOICE_PDF_RENDERER"] = "designer"
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-"
    from io import BytesIO

    from pypdf import PdfReader

    page = PdfReader(BytesIO(resp.data)).pages[0]
    fonts = [str(f.get_object().get("/BaseFont")) for f in page["/Resources"]["/Font"].values()]
    assert any("Montserrat" in f for f in fonts)
