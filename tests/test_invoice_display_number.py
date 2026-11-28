"""What the client sees: the printed number on PDFs and in emails, both numbers on a PDF with XML.

``Invoice.display_number`` is the printed number, else the invoice number; without printed numbers
every template prints what it printed before.
"""

from datetime import date, timedelta

import pytest

from app import db
from app.models import BillingEntity, BillingEntityNumberSeries, Invoice, Settings
from app.services import InvoiceService
from app.utils import billing_entities as billing

pytestmark = [pytest.mark.integration]


@pytest.fixture
def issued(app, project, test_client, user):
    """An invoice issued from the series CLIENTA-{SEQ}, printed {SEQ}: CLIENTA-20, No. 20."""
    entity = BillingEntity(short_name="Acme Ltd", legal_name="ACME LTD")
    db.session.add(entity)
    db.session.flush()
    series = BillingEntityNumberSeries(
        billing_entity_id=entity.id, client_id=test_client.id, number_format="CLIENTA-{SEQ}", printed_format="{SEQ}"
    )
    db.session.add(series)
    db.session.flush()
    billing.set_last_seq(series, 0, 19)
    inv = Invoice(
        invoice_number="TMP",
        project_id=project.id,
        client_name=test_client.name,
        client_email="billing@example.com",
        due_date=date.today() + timedelta(days=30),
        created_by=user.id,
        client_id=test_client.id,
    )
    inv.billing_entity_id = entity.id
    billing.number_new_draft(inv)
    db.session.add(inv)
    db.session.commit()
    assert inv.display_number == inv.invoice_number  # a draft prints DRAFT-<id>
    InvoiceService().transition_status(inv, "issued")
    db.session.commit()
    return inv


def test_display_number_and_pdf_with_xml(app, issued):
    from app.utils.invoice_numbering import InvoicePdfView, pdf_display_number

    inv = issued
    settings = Settings.get_settings()
    settings.invoices_zugferd_pdf = False
    assert pdf_display_number(inv, settings) == "20"
    settings.invoices_zugferd_pdf = True
    assert pdf_display_number(inv, settings) == "20 (CLIENTA-20)"
    assert InvoicePdfView(inv, settings).display_number == "20 (CLIENTA-20)"
    assert InvoicePdfView(inv, settings).invoice_number == "CLIENTA-20"
    assert inv.to_dict()["printed_number"] == "20" and inv.to_dict()["number_series_id"] == inv.number_series_id


def test_html_template_prints_the_printed_number(app, issued):
    from app.utils.invoice_pdf_html import render_invoice_html

    inv = issued
    html = render_invoice_html("<p>No. {{ invoice.display_number }} / {{ invoice.invoice_number }}</p>", inv)
    assert "No. 20 / CLIENTA-20" in html


def test_without_printed_number_templates_print_the_same(app, invoice):
    from app.utils.invoice_pdf_html import render_invoice_html

    old = render_invoice_html("<p>{{ invoice.invoice_number }}</p>", invoice)
    new = render_invoice_html("<p>{{ invoice.display_number }}</p>", invoice)
    assert old == new and invoice.invoice_number in new


def test_invoice_email_prints_the_printed_number(app, issued):
    from unittest.mock import MagicMock, patch

    from app.utils.email import _build_invoice_email_payload

    with patch("app.utils.pdf_generator.InvoicePDFGenerator") as gen:
        gen.return_value = MagicMock(generate_pdf=MagicMock(return_value=b"%PDF"))
        _pdf, html_body, text_body, subject = _build_invoice_email_payload(issued)
    assert subject.startswith("Invoice 20 from")
    assert "Invoice Number: 20" in (text_body or "") or "20" in html_body
    assert "CLIENTA-20" not in html_body


def test_reminder_and_client_portal_use_the_printed_number(app, issued):
    from types import SimpleNamespace

    from app.services.invoice_reminder_service import _reminder_subject

    assert "invoice 20 " in _reminder_subject(issued, SimpleNamespace(days_offset=3))
    assert issued.to_dict()["invoice_number"] == "CLIENTA-20"  # the API keeps the number


def test_default_pdf_prints_the_printed_number(app, issued):
    from app.utils.pdf_generator import InvoicePDFGenerator

    settings = Settings.get_settings()
    generator = InvoicePDFGenerator(issued, settings=settings)
    context = generator._prepare_template_context()
    assert context["invoice"].display_number == "20"
    assert context["invoice"].invoice_number == "CLIENTA-20"
