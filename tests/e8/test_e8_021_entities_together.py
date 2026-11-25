"""E8-021: our legal entities (billing_entities) and client legal entities (client_entities) together.

The two modules do not know about each other; the invoice form shows From / Pay to, Bill to,
both (Bill to next to From) or neither, depending on which modules are on.
"""

from datetime import date, timedelta

import pytest

from app import db
from app.models import BillingEntity, ClientEntity, Invoice, PaymentAccount, Settings

pytestmark = [pytest.mark.integration]


@pytest.fixture
def seller(app):
    e = BillingEntity(short_name="Seller One", legal_name="SELLER ONE LLC", number_format="S1-{YYYY}-{MM}")
    db.session.add(e)
    db.session.commit()
    a = PaymentAccount(
        billing_entity_id=e.id, currency="EUR", iban="DE89370400440532013000", details_text="IBAN {IBAN}"
    )
    db.session.add(a)
    db.session.commit()
    return e, a


@pytest.fixture
def buyer(app, test_client):
    b = ClientEntity(client_id=test_client.id, legal_name="Buyer Two S.L.", vat_number="ESB00000000")
    db.session.add(b)
    db.session.commit()
    return b


def _modules_off(*ids):
    settings = Settings.get_settings()
    settings.disabled_module_ids = list(ids)
    db.session.commit()


def _form_html(client):
    return client.get("/invoices/new").get_data(as_text=True)


@pytest.mark.parametrize(
    "off, has_seller_block, has_bill_to",
    [
        ([], True, True),
        (["billing_entities"], False, True),
        (["client_entities"], True, False),
        (["billing_entities", "client_entities"], False, False),
    ],
)
def test_form_per_module_combination(
    app, authenticated_client, project, seller, buyer, off, has_seller_block, has_bill_to
):
    _modules_off(*off)
    html = _form_html(authenticated_client)
    assert ('id="billingSellerBlock"' in html) is has_seller_block
    assert ('id="billToBlock"' in html) is has_bill_to
    if has_seller_block:
        assert "data-bill-to-host" in html and 'id="billToSlot"' in html and 'id="billToPreviewSlot"' in html


@pytest.mark.parametrize(
    "off", [[], ["billing_entities"], ["client_entities"], ["billing_entities", "client_entities"]]
)
def test_create_invoice_per_module_combination(app, authenticated_client, project, test_client, seller, buyer, off):
    _modules_off(*off)
    entity, account = seller
    resp = authenticated_client.post(
        "/invoices/create",
        data={
            "project_id": project.id,
            "client_name": "Buyer Two S.L.",
            "due_date": (date.today() + timedelta(days=30)).strftime("%Y-%m-%d"),
            "tax_rate": "0",
            "currency_code": "EUR",
            "billing_entity_id": entity.id,
            "payment_account_id": account.id,
            "client_entity_id": buyer.id,
        },
    )
    assert resp.status_code == 302
    inv = Invoice.query.order_by(Invoice.id.desc()).first()
    assert (inv.billing_entity_id == entity.id) is ("billing_entities" not in off)
    assert (inv.client_entity_id == buyer.id) is ("client_entities" not in off)


def test_pdf_prints_seller_and_buyer(app, invoice, seller, buyer):
    from app.utils import billing_entities as billing
    from app.utils import client_entities as bill_to
    from app.utils.pdf_generator import InvoicePDFGenerator

    entity, account = seller
    billing.apply_to_invoice(invoice, entity, account)
    bill_to.apply_to_invoice(invoice, buyer)
    db.session.commit()
    html = InvoicePDFGenerator(invoice)._generate_html()
    assert "SELLER ONE LLC" in html
    assert "ESB00000000" in html
