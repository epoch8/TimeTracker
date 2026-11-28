"""Number series on the screens and on every path that issues an invoice.

Step 1 and the old form make DRAFT-<id> drafts (or "Set number now"); Mark as issued, Send, bulk
status, payments, Peppol, SDI and the API give the series number when the invoice leaves draft. A
printed number the client already has asks first (409 with the dialog's texts) and can be issued anyway.
The legal entity page edits the default series and the series by client.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.models import ApiToken, BillingEntity, BillingEntityNumberSeries, ClientEntity, Invoice, InvoiceItem, Settings
from app.models.audit_log import AuditLog
from app.utils import billing_entities as billing

pytestmark = [pytest.mark.routes]


@pytest.fixture
def entity(app):
    e = BillingEntity(short_name="Acme Ltd", legal_name="ACME LTD", country="GB")
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def bill_to(app, test_client):
    e = ClientEntity(client_id=test_client.id, legal_name="Client A Holding Ltd", country="DE")
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def series(app, entity, test_client):
    """CLIENTA-{SEQ}, printed {SEQ}, Next 20, for the client (any legal entity)."""
    s = BillingEntityNumberSeries(
        billing_entity_id=entity.id, client_id=test_client.id, number_format="CLIENTA-{SEQ}", printed_format="{SEQ}"
    )
    db.session.add(s)
    db.session.flush()
    billing.set_last_seq(s, 0, 19)
    db.session.commit()
    return s


def _draft(project, test_client, user, entity, number=None, printed=None, **kw):
    inv = Invoice(
        invoice_number=number or "TMP",
        project_id=project.id,
        client_name=test_client.name,
        due_date=date.today() + timedelta(days=30),
        created_by=user.id,
        client_id=test_client.id,
        issue_date=kw.pop("issue_date", date(2026, 11, 30)),
        **kw,
    )
    inv.billing_entity_id = entity.id
    if number is None:
        billing.number_new_draft(inv)
    if printed:
        inv.printed_number, inv.printed_number_edited = printed, True
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Work", quantity=1, unit_price=Decimal("100")))
    inv.calculate_totals()
    db.session.commit()
    return inv


def _issued(project, test_client, user, entity, number, printed=None):
    inv = _draft(project, test_client, user, entity, number=number)
    inv.printed_number = printed
    inv.status = "issued"
    billing.set_confirmed_repeat(inv, printed or number)
    db.session.commit()
    return inv


def _mark_issued(client, invoice, **extra):
    data = {"new_status": "issued", **extra}
    return client.post(f"/invoices/{invoice.id}/status", data=data)


def _token(user):
    token, plain = ApiToken.create_token(user_id=user.id, name="Series", scopes="read:invoices,write:invoices")
    db.session.add(token)
    db.session.commit()
    return {"Authorization": f"Bearer {plain}"}


# --- Mark as issued -------------------------------------------------------------------------------


def test_mark_as_issued_gives_the_series_number(app, authenticated_client, project, test_client, user, series):
    entity = series.billing_entity
    first = _draft(project, test_client, user, entity)
    second = _draft(project, test_client, user, entity)
    assert first.invoice_number == f"DRAFT-{first.id}"
    resp = _mark_issued(authenticated_client, second, expected_number="CLIENTA-20")
    assert resp.status_code == 200
    assert resp.get_json()["invoice_number"] == "CLIENTA-20"
    resp = _mark_issued(authenticated_client, first, expected_number="CLIENTA-20")
    assert resp.get_json()["invoice_number"] == "CLIENTA-21"
    page = authenticated_client.get(f"/invoices/{first.id}?view=full").get_data(as_text=True)
    assert "The draft said CLIENTA-20, but CLIENTA-20 was issued first from another draft." in page


def test_repeat_asks_and_issue_anyway_issues(app, authenticated_client, project, test_client, user, series):
    entity = series.billing_entity
    old = _issued(project, test_client, user, entity, "OLD-13", printed="13")
    inv = _draft(project, test_client, user, entity, printed="13")

    resp = _mark_issued(authenticated_client, inv)
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["error"] == "duplicate_printed_number"
    assert data["printed_number"] == "13"
    assert data["dialog"]["title"] == "No. 13 is already used for this client"
    assert data["dialog"]["issue"] == "Issue anyway" and data["dialog"]["change"] == "Change number"
    assert "OLD-13" in data["dialog"]["text"] and "(set by hand)" in data["dialog"]["text"]
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).status == "draft"

    resp = _mark_issued(authenticated_client, inv, confirm_duplicate_printed_number="13")
    assert resp.status_code == 200
    issued = db.session.get(Invoice, inv.id)
    assert (issued.status, issued.invoice_number, issued.printed_number) == ("issued", "CLIENTA-20", "13")
    log = AuditLog.query.filter_by(entity_type="Invoice", entity_id=inv.id, field_name="printed_number").one()
    assert "Printed number repeated, confirmed by" in log.change_description
    assert old.id == (log.get_entity_metadata() or {}).get("repeats_invoice_id")


def test_bulk_status_skips_a_repeat_and_issues_the_rest(
    app, admin_authenticated_client, project, test_client, admin_user, series
):
    entity = series.billing_entity
    _issued(project, test_client, admin_user, entity, "OLD-20", printed="20")
    repeat = _draft(project, test_client, admin_user, entity)  # would be CLIENTA-20, printed 20
    other = _draft(project, test_client, admin_user, entity, printed="X")
    resp = admin_authenticated_client.post(
        "/invoices/bulk-status",
        data={"invoice_ids[]": [str(repeat.id), str(other.id)], "status": "issued"},
        follow_redirects=True,
    )
    page = resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Invoice, repeat.id).status == "draft"
    issued = db.session.get(Invoice, other.id)
    assert issued.status == "issued" and issued.invoice_number == "CLIENTA-20"  # the skipped one took nothing
    assert "No. 20 is already used for this client" in page


def test_module_off_drafts_get_settings_numbers_in_bulk(
    app, admin_authenticated_client, project, test_client, admin_user, series
):
    entity = series.billing_entity
    a = _draft(project, test_client, admin_user, entity)
    b = _draft(project, test_client, admin_user, entity)
    settings = Settings.get_settings()
    settings.disabled_module_ids = ["billing_entities"]
    db.session.commit()
    admin_authenticated_client.post(
        "/invoices/bulk-status", data={"invoice_ids[]": [str(a.id), str(b.id)], "status": "issued"}
    )
    db.session.expire_all()
    numbers = {db.session.get(Invoice, i).invoice_number for i in (a.id, b.id)}
    assert len(numbers) == 2 and not any(n.startswith("DRAFT-") for n in numbers)


# --- payments, API, Peppol, SDI, email -----------------------------------------------------------------


def test_payment_on_a_draft_with_a_repeat_is_refused(app, project, test_client, user, series):
    from app.services.payment_service import PaymentService

    entity = series.billing_entity
    _issued(project, test_client, user, entity, "OLD-20", printed="20")
    inv = _draft(project, test_client, user, entity)
    result = PaymentService().create_payment(
        invoice_id=inv.id, amount=inv.total_amount, payment_date=date(2026, 12, 1), received_by=user.id
    )
    assert not result["success"]
    assert "issue the invoice with Mark as issued first" in result["message"]
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).status == "draft"
    assert db.session.get(Invoice, inv.id).payments.count() == 0
    # the session works afterwards
    assert billing.flush_status_change() is None
    db.session.commit()


def test_payment_on_a_draft_gives_the_number(app, project, test_client, user, series):
    from app.services.payment_service import PaymentService

    inv = _draft(project, test_client, user, series.billing_entity)
    result = PaymentService().create_payment(
        invoice_id=inv.id, amount=inv.total_amount, payment_date=date(2026, 12, 1), received_by=user.id
    )
    assert result["success"]
    db.session.expire_all()
    paid = db.session.get(Invoice, inv.id)
    assert (paid.status, paid.invoice_number) == ("paid", "CLIENTA-20")


def test_api_send_answers_409_and_issues_with_confirmation(app, client, project, test_client, user, series):
    entity = series.billing_entity
    _issued(project, test_client, user, entity, "OLD-20", printed="20")
    inv = _draft(project, test_client, user, entity)
    headers = _token(user)
    resp = client.post(
        f"/api/v1/invoices/{inv.id}/send-email", json={"recipient_email": "a@example.com"}, headers=headers
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error_code"] == "duplicate_printed_number"
    assert body["details"]["printed_number"] == "20"
    with patch("app.utils.email.mail.send"), patch("app.utils.pdf_generator.InvoicePDFGenerator") as gen:
        gen.return_value = MagicMock(generate_pdf=MagicMock(return_value=b"%PDF"))
        resp = client.post(
            f"/api/v1/invoices/{inv.id}/send-email",
            json={"recipient_email": "a@example.com", "confirm_duplicate_printed_number": "20"},
            headers=headers,
        )
    assert resp.status_code == 200
    db.session.expire_all()
    sent = db.session.get(Invoice, inv.id)
    assert (sent.status, sent.invoice_number) == ("sent", "CLIENTA-20")


def test_email_carries_the_number_and_a_failed_send_keeps_it(
    app, authenticated_client, project, test_client, user, series
):
    inv = _draft(project, test_client, user, series.billing_entity)
    seen = {}

    def _send(msg):
        seen["subject"] = msg.subject
        seen["attachment"] = msg.attachments[0].filename
        raise RuntimeError("mail server down")

    with patch("app.utils.email.mail.send", side_effect=_send), patch(
        "app.utils.pdf_generator.InvoicePDFGenerator"
    ) as gen:
        gen.return_value = MagicMock(generate_pdf=MagicMock(return_value=b"%PDF"))
        resp = authenticated_client.post(f"/invoices/{inv.id}/send-email", data={"recipient_email": "a@example.com"})
    assert resp.status_code == 502
    assert "Issued as CLIENTA-20, not sent" in resp.get_json()["error"]
    assert seen["attachment"] == "CLIENTA-20.pdf"  # the file is named by the number
    assert "20" in seen["subject"] and "DRAFT-" not in seen["subject"]
    db.session.expire_all()
    kept = db.session.get(Invoice, inv.id)
    assert (kept.status, kept.invoice_number) == ("issued", "CLIENTA-20")

    with patch("app.utils.email.mail.send"), patch("app.utils.pdf_generator.InvoicePDFGenerator") as gen:
        gen.return_value = MagicMock(generate_pdf=MagicMock(return_value=b"%PDF"))
        resp = authenticated_client.post(f"/invoices/{inv.id}/send-email", data={"recipient_email": "a@example.com"})
    assert resp.status_code == 200
    db.session.expire_all()
    again = db.session.get(Invoice, inv.id)
    assert (again.status, again.invoice_number) == ("sent", "CLIENTA-20")


@pytest.mark.parametrize("service_name", ["PeppolService", "SdiService"])
def test_e_invoice_is_built_after_the_number(app, project, test_client, user, series, service_name):
    import app.services as services

    inv = _draft(project, test_client, user, series.billing_entity)
    service_cls = getattr(services, service_name)
    seen = {}

    def _transmit(self, invoice, triggered_by_user_id=None):
        seen["number"] = invoice.invoice_number
        seen["status"] = invoice.status
        return True, None, "ok"

    with patch.object(service_cls, "_transmit", _transmit):
        success, _tx, _msg = service_cls().send_invoice(inv, triggered_by_user_id=user.id)
    assert success
    assert seen == {"number": "CLIENTA-20", "status": "issued"}
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).status == "sent"


@pytest.mark.parametrize("service_name", ["PeppolService", "SdiService"])
def test_e_invoice_with_a_repeat_is_not_sent(app, project, test_client, user, series, service_name):
    import app.services as services

    entity = series.billing_entity
    _issued(project, test_client, user, entity, "OLD-20", printed="20")
    inv = _draft(project, test_client, user, entity)
    service_cls = getattr(services, service_name)
    with patch.object(service_cls, "_transmit", side_effect=AssertionError("must not transmit")):
        success, _tx, message = service_cls().send_invoice(inv, triggered_by_user_id=user.id)
    assert not success
    assert "issue the invoice with Mark as issued first" in message


# --- creating drafts -----------------------------------------------------------------------------------


def _step_one(project, test_client, entity, **extra):
    data = {
        "client_id": test_client.id,
        "period": "custom",
        "period_start": "2026-11-01",
        "period_end": "2026-11-30",
        "project_ids": [str(project.id)],
        "projects_touched": "1",
        "issue_date": "2026-11-30",
        "due_date": "2026-12-30",
        "currency_code": "EUR",
        "billing_entity_id": entity.id,
        "client_name": test_client.name,
    }
    data.update(extra)
    return data


def _newest():
    return Invoice.query.order_by(Invoice.id.desc()).first()


def test_step_one_makes_a_placeholder_draft(app, authenticated_client, project, test_client, series):
    resp = authenticated_client.post("/invoices/new", data=_step_one(project, test_client, series.billing_entity))
    assert resp.status_code == 302
    inv = _newest()
    assert inv.invoice_number == f"DRAFT-{inv.id}"
    assert billing.last_seq(series, 0) == 19


def test_step_one_shows_the_number_row_for_an_entity_without_a_number_format(
    app, authenticated_client, project, test_client, series
):
    entity = series.billing_entity
    assert not entity.number_format  # a new entity: its numbers come from its series only
    data = billing.form_context([project], Settings.get_settings())["billing_entity_data"]
    assert data[entity.id]["series"] is True and data[entity.id]["number"] == ""
    page = authenticated_client.get(f"/invoices/new?client_id={test_client.id}").get_data(as_text=True)
    # the row with "Will be …" stays visible for a From with series
    assert "e && (e.number || e.series)" in page and "seriesNumberBox" in page


def test_step_one_set_number_now_and_printed_as(app, authenticated_client, project, test_client, series):
    data = _step_one(
        project,
        test_client,
        series.billing_entity,
        number_set_now="1",
        invoice_number="CLIENTA-24",
        printed_number="A/24",
        printed_number_preview="24",
    )
    authenticated_client.post("/invoices/new", data=data)
    inv = _newest()
    assert (inv.invoice_number, inv.printed_number, inv.printed_number_edited) == ("CLIENTA-24", "A/24", True)


def test_step_one_set_number_now_taken_is_refused(app, authenticated_client, project, test_client, user, series):
    _issued(project, test_client, user, series.billing_entity, "CLIENTA-24")
    before = Invoice.query.count()
    data = _step_one(project, test_client, series.billing_entity, number_set_now="1", invoice_number="CLIENTA-24")
    resp = authenticated_client.post("/invoices/new", data=data)
    assert resp.status_code == 409
    assert Invoice.query.count() == before


def test_old_create_form_makes_a_placeholder_draft(app, authenticated_client, project, test_client, series):
    authenticated_client.post(
        "/invoices/create",
        data={
            "project_id": project.id,
            "client_name": test_client.name,
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
            "currency_code": "EUR",
            "billing_entity_id": series.billing_entity_id,
            "invoice_number": "ignored-without-set-now",
        },
    )
    inv = _newest()
    assert inv.invoice_number == f"DRAFT-{inv.id}"
    with authenticated_client.session_transaction() as flask_session:
        flashes = [text for _, text in flask_session.get("_flashes", [])]
    assert f"Invoice DRAFT-{inv.id} created successfully" in flashes  # not the DRAFT-<random> before the commit


def test_client_is_notified_when_issued_not_when_the_draft_is_made(
    app, authenticated_client, project, test_client, series
):
    from app.models.client_notification import ClientNotification

    authenticated_client.post("/invoices/new", data=_step_one(project, test_client, series.billing_entity))
    inv = _newest()
    assert inv.invoice_number.startswith("DRAFT-")
    assert ClientNotification.query.count() == 0
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Work", quantity=1, unit_price=Decimal("100")))
    inv.calculate_totals()
    db.session.commit()
    resp = _mark_issued(authenticated_client, inv)
    assert resp.status_code in (200, 302)
    assert [n.title for n in ClientNotification.query.all()] == ["New Invoice: 20"]


def test_draft_step_one_assign_from_series_and_gap_warning(
    app, authenticated_client, project, test_client, user, series
):
    billing.set_last_seq(series, 0, 24)
    db.session.commit()
    inv = _draft(project, test_client, user, series.billing_entity, number="CLIENTA-25")
    later = _draft(project, test_client, user, series.billing_entity)
    _issue = authenticated_client.post(f"/invoices/{later.id}/status", data={"new_status": "issued"})
    assert _issue.get_json()["invoice_number"] == "CLIENTA-26"  # 25 is held by the first draft
    data = _step_one(project, test_client, series.billing_entity, number_from_series="1")
    resp = authenticated_client.post(f"/invoices/{inv.id}/step-1", data=data, follow_redirects=True)
    assert "CLIENTA-25 will become a gap" in resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).invoice_number == f"DRAFT-{inv.id}"


def test_delete_held_draft_warns_about_the_gap(app, authenticated_client, project, test_client, user, series):
    billing.set_last_seq(series, 0, 24)
    db.session.commit()
    inv = _draft(project, test_client, user, series.billing_entity, number="CLIENTA-25")
    later = _draft(project, test_client, user, series.billing_entity)
    authenticated_client.post(f"/invoices/{later.id}/status", data={"new_status": "issued"})
    resp = authenticated_client.post(f"/invoices/{inv.id}/delete", follow_redirects=True)
    assert "CLIENTA-25 will become a gap" in resp.get_data(as_text=True)


def test_number_preview_endpoint(app, authenticated_client, project, test_client, user, series):
    entity = series.billing_entity
    _issued(project, test_client, user, entity, "OLD-7", printed="24")
    resp = authenticated_client.get(
        f"/invoices/number-preview?entity_id={entity.id}&client_id={test_client.id}&issue_date=2026-11-30"
        "&number=CLIENTA-24"
    )
    data = resp.get_json()
    assert data["series"] and data["number"] == "CLIENTA-20" and data["printed"] == "20"
    assert data["typed"]["fits"] and data["typed"]["skips"] == "This skips 20 to 23."
    assert data["repeat"]["number"] == "OLD-7"


def test_bulk_assign_from_series(app, admin_authenticated_client, project, test_client, admin_user, series):
    inv = _draft(project, test_client, admin_user, series.billing_entity, number="IMPORTED-9")
    preview = admin_authenticated_client.get(f"/invoices/bulk-assign-series/preview?ids={inv.id}").get_json()
    assert preview["rows"] == [{"id": inv.id, "from": "IMPORTED-9", "to": "CLIENTA-20"}]
    admin_authenticated_client.post("/invoices/bulk-assign-series", data={"invoice_ids[]": [str(inv.id)]})
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).invoice_number == f"DRAFT-{inv.id}"


def test_list_and_draft_page_show_the_numbers(app, authenticated_client, project, test_client, user, series):
    draft = _draft(project, test_client, user, series.billing_entity)
    issued = _draft(project, test_client, user, series.billing_entity)
    authenticated_client.post(f"/invoices/{issued.id}/status", data={"new_status": "issued"})
    page = authenticated_client.get("/invoices").get_data(as_text=True)
    assert "CLIENTA-20" in page and "No. 20" in page
    assert "will be CLIENTA-21" in page
    page = authenticated_client.get(f"/invoices/{draft.id}").get_data(as_text=True)
    assert "will be" in page and "CLIENTA-21" in page
    assert f"Until then this draft is DRAFT-{draft.id}; its PDF says so." in page


# --- Cancel and reissue --------------------------------------------------------------------------------


def test_reissue_keeps_printed_number_with_suffix_from_the_original(app, project, test_client, user, series):
    from app.services import InvoiceService
    from app.services.invoice_correction_service import InvoiceCorrectionService

    inv = _draft(project, test_client, user, series.billing_entity)
    InvoiceService().transition_status(inv, "issued")
    db.session.commit()
    reissue = InvoiceCorrectionService().reissue(inv, "wrong address", user.id)
    db.session.commit()
    assert (reissue.invoice_number, reissue.printed_number) == ("CLIENTA-20-2", "20")
    assert billing.last_seq(series, 0) == 20
    InvoiceService().transition_status(reissue, "issued")  # the original is cancelled: no repeat
    db.session.commit()
    again = InvoiceCorrectionService().reissue(reissue, "again", user.id)
    db.session.commit()
    assert (again.invoice_number, again.printed_number) == ("CLIENTA-20-3", "20")


def test_reissue_with_other_bill_to_takes_that_series(
    app, authenticated_client, project, test_client, user, series, bill_to
):
    from app.services import InvoiceService
    from app.services.invoice_correction_service import InvoiceCorrectionService

    other = BillingEntityNumberSeries(
        billing_entity_id=series.billing_entity_id,
        client_id=test_client.id,
        client_entity_id=bill_to.id,
        number_format="HOLD-{SEQ}",
    )
    db.session.add(other)
    db.session.commit()
    inv = _draft(project, test_client, user, series.billing_entity)
    InvoiceService().transition_status(inv, "issued")
    db.session.commit()
    reissue = InvoiceCorrectionService().reissue(inv, "other entity", user.id)
    db.session.commit()
    data = _step_one(project, test_client, series.billing_entity, client_entity_id=bill_to.id)
    resp = authenticated_client.post(f"/invoices/{reissue.id}/step-1", data=data, follow_redirects=True)
    assert "Bill to changed: this invoice gets a number from the series of" in resp.get_data(as_text=True)
    db.session.expire_all()
    reissue = db.session.get(Invoice, reissue.id)
    assert reissue.invoice_number == f"DRAFT-{reissue.id}" and reissue.printed_number is None
    InvoiceService().transition_status(reissue, "issued")
    db.session.commit()
    assert reissue.invoice_number == "HOLD-1"


# --- the legal entity page --------------------------------------------------------------------------------


def test_entity_page_shows_numbering(app, admin_authenticated_client, series):
    page = admin_authenticated_client.get(f"/admin/legal-entities/{series.billing_entity_id}").get_data(as_text=True)
    assert "Invoice numbering" in page and "Series by client" in page and "CLIENTA-{SEQ}" in page
    assert "All invoices to " in page  # which invoices the series takes
    assert "Tokens in the formats" in page
    assert 'name="number_format" id="number_format"' not in page  # the old field is gone from the page
    # Archive / Delete / Save changes stay the last thing on the page, below the numbering
    assert page.index("Series by client") < page.index("Save changes")
    assert 'form="entityForm"' in page


def test_entity_page_with_client_legal_entities_off(app, admin_authenticated_client, series, test_client, bill_to):
    held = BillingEntityNumberSeries(
        billing_entity_id=series.billing_entity_id,
        client_id=test_client.id,
        client_entity_id=bill_to.id,
        number_format="HOLD-{SEQ}",
    )
    db.session.add(held)
    settings = Settings.get_settings()
    settings.disabled_module_ids = list(settings.disabled_module_ids or []) + ["client_entities"]
    db.session.commit()
    page = admin_authenticated_client.get(f"/admin/legal-entities/{series.billing_entity_id}").get_data(as_text=True)
    assert "the series of its client; if the client has none" in page
    assert "the series for its Bill to" not in page
    assert "made for invoices billed to Client A Holding Ltd" in page
    assert "Client legal entities are switched off" in page
    # The series in use comes first, the one waiting for the module after it
    assert page.index("CLIENTA-{SEQ}") < page.index("HOLD-{SEQ}")

    # Saving it while the module is off keeps the legal entity it was made for
    url = f"/admin/legal-entities/{series.billing_entity_id}/series/{held.id}"
    assert "Client A Holding Ltd" in admin_authenticated_client.get(url).get_data(as_text=True)
    resp = admin_authenticated_client.post(
        url,
        data={"client_id": test_client.id, "number_format": "HOLD-{SEQ}", "printed_format": "{SEQ}", "next_seq_0": "1"},
    )
    assert resp.status_code == 302
    db.session.expire_all()
    assert BillingEntityNumberSeries.query.get(held.id).client_entity_id == bill_to.id


def test_default_series_is_saved_and_checked(app, admin_authenticated_client, entity):
    url = f"/admin/legal-entities/{entity.id}/numbering"
    resp = admin_authenticated_client.post(
        url,
        data={"number_format": "ACME-{MM}-{SEQ}", "reset_yearly": "1", f"next_seq_{date.today().year}": "1"},
        follow_redirects=True,
    )
    assert "{MM} is not allowed" in resp.get_data(as_text=True)
    resp = admin_authenticated_client.post(
        url,
        data={"number_format": "ACME-{SEQ}", "reset_yearly": "1", f"next_seq_{date.today().year}": "1"},
        follow_redirects=True,
    )
    assert "needs {YYYY} or {YY}" in resp.get_data(as_text=True)
    admin_authenticated_client.post(
        url, data={"number_format": "ACME-{YYYY}-{SEQ}", "printed_format": "{SEQ}", "next_seq_0": "7"}
    )
    s = BillingEntityNumberSeries.query.filter_by(billing_entity_id=entity.id).one()
    assert (s.number_format, s.printed_format, billing.last_seq(s, 0)) == ("ACME-{YYYY}-{SEQ}", "{SEQ}", 6)


def test_series_form_errors(app, admin_authenticated_client, project, test_client, user, series):
    other_entity = BillingEntity(short_name="Other Ltd", legal_name="OTHER LTD")
    db.session.add(other_entity)
    db.session.commit()
    url = f"/admin/legal-entities/{other_entity.id}/series/new"
    resp = admin_authenticated_client.post(
        url, data={"client_id": test_client.id, "number_format": "CLIENTA-{SEQ}", "next_seq_0": "1"}
    )
    assert resp.status_code == 400
    assert "Numbers are unique across all invoices" in resp.get_data(as_text=True)

    _issued(project, test_client, user, series.billing_entity, "CLIENTA-20")
    _issued(project, test_client, user, series.billing_entity, "CLIENTA-21")
    resp = admin_authenticated_client.post(
        f"/admin/legal-entities/{series.billing_entity_id}/series/{series.id}",
        data={"client_id": test_client.id, "number_format": "CLIENTA-{SEQ}", "next_seq_0": "20"},
    )
    page = resp.get_data(as_text=True)
    # Next can't go back below the last number given ...
    assert "Next {SEQ} must be above 21, the last number given in this series." in page and "Use 22" in page
    # ... nor onto numbers that drafts hold
    _draft(project, test_client, user, series.billing_entity, number="CLIENTA-22")
    _draft(project, test_client, user, series.billing_entity, number="CLIENTA-23")
    resp = admin_authenticated_client.post(
        f"/admin/legal-entities/{series.billing_entity_id}/series/{series.id}",
        data={"client_id": test_client.id, "number_format": "CLIENTA-{SEQ}", "next_seq_0": "22"},
    )
    page = resp.get_data(as_text=True)
    assert "CLIENTA-22 to CLIENTA-23 are already taken." in page and "Use 24" in page

    resp = admin_authenticated_client.post(
        f"/admin/legal-entities/{series.billing_entity_id}/series/new",
        data={"client_id": test_client.id, "number_format": "CL-{SEQ}", "next_seq_0": "1"},
    )
    assert "already has a series for this Bill to" in resp.get_data(as_text=True)


def test_series_form_saves_a_bill_to_series(app, admin_authenticated_client, entity, test_client, bill_to):
    resp = admin_authenticated_client.post(
        f"/admin/legal-entities/{entity.id}/series/new",
        data={
            "client_id": test_client.id,
            "client_entity_id": bill_to.id,
            "number_format": "HOLD-{SEQ}",
            "printed_format": "{SEQ}",
            "next_seq_0": "5",
        },
    )
    assert resp.status_code == 302
    s = BillingEntityNumberSeries.query.filter_by(number_format="HOLD-{SEQ}").one()
    assert (s.client_entity_id, billing.last_seq(s, 0)) == (bill_to.id, 4)


def test_pages_render(app, admin_authenticated_client, project, test_client, admin_user, series, bill_to):
    entity = series.billing_entity
    draft = _draft(project, test_client, admin_user, entity)
    set_now = _draft(project, test_client, admin_user, entity, number="CLIENTA-30")
    pages = {
        "/invoices/new?client_id=%d" % test_client.id: "seriesNumberBox",
        f"/invoices/{draft.id}/step-1": "The draft itself is DRAFT-",
        f"/invoices/{set_now.id}/step-1": "Assign from the series when issued",
        f"/invoices/create?project_id={project.id}": "seriesNumberBox",
        f"/admin/legal-entities/{entity.id}/series/new": "Any legal entity of this client",
        f"/admin/legal-entities/{entity.id}/series/{series.id}": "Next invoice to",
        f"/invoices/{draft.id}?view=full": "ttPostIssuing",
    }
    for url, marker in pages.items():
        resp = admin_authenticated_client.get(url, follow_redirects=True)
        assert resp.status_code == 200, url
        assert marker in resp.get_data(as_text=True), url
