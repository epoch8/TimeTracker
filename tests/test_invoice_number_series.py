"""Invoice number series of our legal entities and the printed number (Legal entities module).

A series numbers the invoices of one legal entity: its default series, a series per client, or per
legal entity of a client (Bill to). A draft is DRAFT-<id>; its series number is given when it leaves
draft, on every path that changes the status, so a deleted draft leaves no gap. The printed number
is what the client sees; a printed number already used for the client needs a confirmation.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import db
from app.models import (
    BillingEntity,
    BillingEntityNumberCounter,
    BillingEntityNumberSeries,
    ClientEntity,
    Invoice,
    Settings,
    TimeEntry,
)
from app.services import InvoiceService
from app.utils import billing_entities as billing

pytestmark = [pytest.mark.integration]


# --- fixtures -------------------------------------------------------------------------------------


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
def bill_to_b(app, test_client):
    e = ClientEntity(client_id=test_client.id, legal_name="Client A Trading Ltd", country="CY")
    db.session.add(e)
    db.session.commit()
    return e


def _series(entity, number_format, client=None, client_entity=None, printed="{NUMBER}", next_seq=1, **kw):
    s = BillingEntityNumberSeries(
        billing_entity_id=entity.id,
        client_id=client.id if client else None,
        client_entity_id=client_entity.id if client_entity else None,
        number_format=number_format,
        printed_format=printed,
        **kw,
    )
    db.session.add(s)
    db.session.flush()
    if next_seq > 1:
        billing.set_last_seq(s, s.counter_year(date.today()) if s.reset_yearly else 0, next_seq - 1)
    db.session.commit()
    return s


def _draft(project, test_client, user, entity=None, client_entity=None, number=None, issue_date=None, **kw):
    inv = Invoice(
        invoice_number=number or "TMP",
        project_id=project.id,
        client_name=test_client.name,
        due_date=date.today() + timedelta(days=30),
        created_by=user.id,
        client_id=test_client.id,
        issue_date=issue_date or date(2026, 11, 30),
        **kw,
    )
    inv.billing_entity_id = entity.id if entity else None
    inv.client_entity_id = client_entity.id if client_entity else None
    if number is None:
        assert billing.number_new_draft(inv) in ("series", "settings")
    db.session.add(inv)
    db.session.commit()
    return inv


def _issue(invoice, **kw):
    result = InvoiceService().transition_status(invoice, "issued", **kw)
    if result.get("success"):
        db.session.commit()
    return result


def _disable(module_id):
    settings = Settings.get_settings()
    settings.disabled_module_ids = list(settings.disabled_module_ids or []) + [module_id]
    db.session.commit()


def _counter(series, year=0):
    return billing.last_seq(series, year)


# --- drafts and issuing ---------------------------------------------------------------------------


def test_draft_is_placeholder_and_issue_takes_next_number(app, project, test_client, user, entity):
    series = _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    draft = _draft(project, test_client, user, entity)
    assert draft.invoice_number == f"DRAFT-{draft.id}"
    assert draft.printed_number is None
    assert _counter(series) == 19  # a draft takes nothing

    assert _issue(draft)["success"]
    assert draft.invoice_number == "CLIENTA-20"
    assert draft.printed_number == "20"
    assert draft.display_number == "20"
    assert draft.number_series_id == series.id
    assert _counter(series) == 20


def test_two_drafts_issued_in_turn_have_no_gap(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, next_seq=20)
    first = _draft(project, test_client, user, entity)
    second = _draft(project, test_client, user, entity)
    deleted = _draft(project, test_client, user, entity)
    db.session.delete(deleted)
    db.session.commit()
    _issue(second)
    _issue(first)
    assert (second.invoice_number, first.invoice_number) == ("CLIENTA-20", "CLIENTA-21")


def test_series_by_bill_to_then_client_then_default(app, project, test_client, user, entity, bill_to, bill_to_b):
    _series(entity, "ACME-{YYYY}-{SEQ}")
    _series(entity, "CLIENT-{SEQ}", client=test_client)
    _series(entity, "CLIENTA-{SEQ}", client=test_client, client_entity=bill_to, printed="{SEQ}", next_seq=20)
    a = _draft(project, test_client, user, entity, client_entity=bill_to)
    b = _draft(project, test_client, user, entity, client_entity=bill_to_b)
    _issue(a)
    _issue(b)
    assert a.invoice_number == "CLIENTA-20"
    assert b.invoice_number == "CLIENT-1"


def test_client_entities_off_takes_any_legal_entity_series(app, project, test_client, user, entity, bill_to):
    _series(entity, "CLIENT-{SEQ}", client=test_client)
    _series(entity, "CLIENTA-{SEQ}", client=test_client, client_entity=bill_to)
    _disable("client_entities")
    inv = _draft(project, test_client, user, entity, client_entity=bill_to)
    _issue(inv)
    assert inv.invoice_number == "CLIENT-1"


def test_archived_series_is_not_picked(app, project, test_client, user, entity):
    _series(entity, "ACME-{SEQ}")
    _series(entity, "OLD-{SEQ}", client=test_client, status="archived")
    inv = _draft(project, test_client, user, entity)
    _issue(inv)
    assert inv.invoice_number == "ACME-1"


def test_number_held_by_draft_set_by_hand_is_skipped(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, next_seq=25)
    held = _draft(project, test_client, user, entity, number="CLIENTA-25")
    inv = _draft(project, test_client, user, entity)
    _issue(inv)
    assert inv.invoice_number == "CLIENTA-26"
    assert inv._numbering_result["held_by"] == (held.id, "CLIENTA-25")
    message = billing.numbering_message(inv)
    assert f"CLIENTA-25 is held by draft {held.id}" in message
    # the held number is issued by its own draft; the counter only moves forward
    _issue(held)
    assert held.invoice_number == "CLIENTA-25"
    series = BillingEntityNumberSeries.query.filter_by(number_format="CLIENTA-{SEQ}").one()
    assert _counter(series) == 26


def test_set_number_now_claims_its_seq_when_issued(app, project, test_client, user, entity):
    series = _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    inv = _draft(project, test_client, user, entity, number="CLIENTA-24")
    assert _counter(series) == 19
    _issue(inv)
    assert inv.invoice_number == "CLIENTA-24"
    assert inv.printed_number == "24"
    assert inv.number_series_id == series.id
    assert _counter(series) == 24


def test_number_outside_format_is_kept_and_printed_as_is(app, project, test_client, user, entity):
    series = _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    inv = _draft(project, test_client, user, entity, number="INV-OLD-7")
    _issue(inv)
    assert inv.invoice_number == "INV-OLD-7"
    assert inv.printed_number == "INV-OLD-7"
    assert inv.number_series_id == series.id
    assert _counter(series) == 19


def test_edited_printed_number_is_kept(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}")
    inv = _draft(project, test_client, user, entity)
    inv.printed_number = "A/1"
    inv.printed_number_edited = True
    db.session.commit()
    _issue(inv)
    assert (inv.invoice_number, inv.printed_number) == ("CLIENTA-1", "A/1")


def test_cancelling_a_draft_gives_no_number(app, project, test_client, user, entity):
    series = _series(entity, "CLIENTA-{SEQ}", client=test_client)
    inv = _draft(project, test_client, user, entity)
    result = InvoiceService().transition_status(inv, "cancelled")
    db.session.commit()
    assert result["success"]
    assert inv.invoice_number == f"DRAFT-{inv.id}"
    assert _counter(series) == 0


def test_time_entries_get_the_issued_number(app, project, test_client, user, entity):
    from datetime import datetime

    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    inv = _draft(project, test_client, user, entity)
    entry = TimeEntry(
        user_id=user.id,
        project_id=project.id,
        start_time=datetime(2026, 11, 2, 9),
        end_time=datetime(2026, 11, 2, 11),
        billable=True,
    )
    entry.invoice_number = inv.invoice_number  # a copy of the draft's number
    db.session.add(entry)
    db.session.commit()
    _issue(inv)
    db.session.refresh(entry)
    assert entry.invoice_number == "CLIENTA-1"


# --- module off, entity without series ------------------------------------------------------------


def test_entity_without_series_numbers_at_creation_as_before(app, project, test_client, user, entity):
    entity.number_format = "INV-{YYYY}-{SEQ}"
    entity.number_counter = 3
    db.session.commit()
    inv = _draft(project, test_client, user, entity, number=billing.allocate_invoice_number(entity))
    _issue(inv)
    assert inv.invoice_number.endswith("-4")
    assert inv.printed_number is None


def test_hook_does_nothing_with_module_off(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    _disable("billing_entities")
    sent = Invoice(
        invoice_number="IMPORTED-1",
        project_id=project.id,
        client_name=test_client.name,
        due_date=date.today(),
        created_by=user.id,
        client_id=test_client.id,
    )
    sent.status = "sent"
    sent.billing_entity_id = entity.id
    db.session.add(sent)
    db.session.commit()
    assert sent.printed_number is None and sent.number_series_id is None


def test_module_off_drafts_get_settings_numbers_in_one_request(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    a = _draft(project, test_client, user, entity)
    b = _draft(project, test_client, user, entity)
    _disable("billing_entities")
    service = InvoiceService()
    assert service.transition_status(a, "issued")["success"]
    assert service.transition_status(b, "issued")["success"]
    db.session.commit()
    assert not a.invoice_number.startswith("DRAFT-") and not b.invoice_number.startswith("DRAFT-")
    assert a.invoice_number != b.invoice_number
    assert a.printed_number is None


def test_created_not_as_draft_gets_a_number(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    inv = Invoice(
        invoice_number="TMP",
        project_id=project.id,
        client_name=test_client.name,
        due_date=date.today(),
        created_by=user.id,
        client_id=test_client.id,
    )
    inv.billing_entity_id = entity.id
    billing.set_draft_placeholder(inv)
    inv.status = "sent"
    db.session.add(inv)
    db.session.commit()
    assert inv.invoice_number == "CLIENTA-1"


# --- printed number repeated for the client ---------------------------------------------------------


def _issued(project, test_client, user, entity, number, printed=None, client_entity=None, **kw):
    inv = _draft(project, test_client, user, entity, number=number, client_entity=client_entity, **kw)
    inv.printed_number = printed
    inv.status = "issued"
    billing.set_confirmed_repeat(inv, printed or number)  # set up on purpose
    db.session.commit()
    return inv


def test_repeat_of_printed_number_needs_confirmation(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    old = _issued(project, test_client, user, entity, "13")  # an older invoice without printed number
    inv = _draft(project, test_client, user, entity)
    inv.printed_number, inv.printed_number_edited = "13", True
    db.session.commit()

    result = _issue(inv)
    assert not result["success"]
    assert result["error"] == "duplicate_printed_number"
    err = result["number_error"]
    assert (err.printed, err.other_id) == ("13", old.id)
    db.session.refresh(inv)
    assert inv.status == "draft" and inv.invoice_number.startswith("DRAFT-")
    assert BillingEntityNumberCounter.query.count() == 1  # the counter did not move
    # the session works after the rollback
    assert db.session.query(Invoice).count() == 2

    result = _issue(inv, confirm_duplicate_printed="13")
    assert result["success"]
    assert inv._numbering_result["repeated"]["id"] == old.id


def test_confirmation_is_for_that_number_only(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    _issued(project, test_client, user, entity, "X-20", printed="20")
    _issued(project, test_client, user, entity, "X-21", printed="21")
    first = _draft(project, test_client, user, entity)
    other = _draft(project, test_client, user, entity)
    assert _issue(first)["error"] == "duplicate_printed_number"
    db.session.rollback()
    # meanwhile another draft takes CLIENTA-20 (confirmed), so this one gets 21: asked again
    _issue(other, confirm_duplicate_printed="20")
    first = db.session.get(Invoice, first.id)
    result = _issue(first, confirm_duplicate_printed="20")
    assert result["error"] == "duplicate_printed_number"
    assert result["number_error"].printed == "21"


def test_repeat_rules(app, project, test_client, user, entity, bill_to, bill_to_b, multiple_clients):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=7)
    probe = _draft(project, test_client, user, entity, client_entity=bill_to)
    probe.printed_number = "7"
    # another client: not a repeat
    other_client = multiple_clients[0]
    _issued(project, other_client, user, entity, "O-7", printed="7")
    assert billing.find_printed_duplicate(probe, "7") is None
    # cancelled: not a repeat
    cancelled = _issued(project, test_client, user, entity, "C-7", printed="7")
    cancelled.status = "cancelled"
    db.session.commit()
    assert billing.find_printed_duplicate(probe, "7") is None
    # another Bill to of the same client: not a repeat; Bill to on one only: a repeat
    _issued(project, test_client, user, entity, "B-7", printed="7", client_entity=bill_to_b)
    assert billing.find_printed_duplicate(probe, "7") is None
    no_bill_to = _issued(project, test_client, user, entity, "N-7", printed="7")
    assert billing.find_printed_duplicate(probe, "7").id == no_bill_to.id
    # a draft counts for the draft's warning only
    no_bill_to.status = "draft"
    db.session.commit()
    assert billing.find_printed_duplicate(probe, "7") is None
    assert billing.find_printed_duplicate(probe, "7", issued_only=False).id == no_bill_to.id


def test_yearly_series_compares_same_year_only(app, project, test_client, user, entity):
    series = _series(entity, "RU-{YYYY}-{SEQ}", printed="{SEQ}", reset_yearly=True)
    _issued(project, test_client, user, entity, "RU-2025-1", printed="1", issue_date=date(2025, 3, 1))
    inv = _draft(project, test_client, user, entity, issue_date=date(2026, 1, 5))
    assert _issue(inv)["success"]
    assert (inv.invoice_number, inv.printed_number) == ("RU-2026-1", "1")
    assert _counter(series, 2026) == 1


# --- year -----------------------------------------------------------------------------------------------


def test_yearly_series_uses_issue_date_year(app, project, test_client, user, entity):
    series = _series(entity, "RU-{YYYY}-{SEQ}", printed="{SEQ}", reset_yearly=True)
    billing.set_last_seq(series, 2026, 41)
    db.session.commit()
    december = _draft(project, test_client, user, entity, issue_date=date(2027, 1, 5))
    december.period_start, december.period_end = date(2026, 12, 1), date(2026, 12, 31)
    db.session.commit()
    _issue(december)
    assert december.invoice_number == "RU-2027-1"
    late = _draft(project, test_client, user, entity, issue_date=date(2026, 12, 31))
    _issue(late)
    assert late.invoice_number == "RU-2026-42"
    assert (_counter(series, 2026), _counter(series, 2027)) == (42, 1)


def test_series_without_restart_uses_period_year(app, project, test_client, user, entity):
    _series(entity, "ACME-{YYYY}-{SEQ}")
    inv = _draft(project, test_client, user, entity, issue_date=date(2027, 1, 5))
    inv.period_start, inv.period_end = date(2026, 12, 1), date(2026, 12, 31)
    db.session.commit()
    _issue(inv)
    assert inv.invoice_number == "ACME-2026-1"


def test_issue_date_none_is_today(app, project, test_client, user):
    inv = Invoice(
        invoice_number="T-1",
        project_id=project.id,
        client_name=test_client.name,
        due_date=date.today(),
        created_by=user.id,
        client_id=test_client.id,
        issue_date=None,
    )
    db.session.add(inv)
    db.session.commit()
    assert inv.issue_date == billing.default_issue_date()


# --- guard: status only through the object --------------------------------------------------------------


def test_bulk_status_update_is_refused_in_tests(app, invoice):
    """conftest.py refuses a bulk UPDATE of invoices.status: it would skip the numbering hook."""
    with pytest.raises(AssertionError):
        Invoice.query.filter(Invoice.id == invoice.id).update({"status": "sent"})
    db.session.rollback()
    with pytest.raises(AssertionError):
        db.session.execute(db.update(Invoice).where(Invoice.id == invoice.id).values(status="sent"))
    db.session.rollback()
    # other columns are fine
    Invoice.query.filter(Invoice.id == invoice.id).update({"notes": "bulk"})
    db.session.commit()


# --- what the client sees -----------------------------------------------------------------------------


def test_issued_printed_number_stays_when_series_changes_or_module_is_off(app, project, test_client, user, entity):
    series = _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}")
    inv = _draft(project, test_client, user, entity)
    _issue(inv)
    series.printed_format = "A/{SEQ}"
    db.session.commit()
    _disable("billing_entities")
    InvoiceService().transition_status(inv, "sent")
    db.session.commit()
    db.session.refresh(inv)
    assert (inv.invoice_number, inv.printed_number, inv.display_number) == ("CLIENTA-1", "1", "1")


# --- four module combinations ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disabled,expected",
    [
        ([], "CLIENTA-1"),  # both on: the series of the Bill to
        (["client_entities"], "CLIENT-1"),  # only our legal entities: "Any legal entity"
        (["billing_entities"], None),  # only client legal entities: Settings, at creation
        (["billing_entities", "client_entities"], None),  # both off: Settings, at creation
    ],
)
def test_module_combinations(app, project, test_client, user, entity, bill_to, disabled, expected):
    _series(entity, "CLIENT-{SEQ}", client=test_client, printed="{SEQ}")
    _series(entity, "CLIENTA-{SEQ}", client=test_client, client_entity=bill_to, printed="{SEQ}")
    for module_id in disabled:
        _disable(module_id)
    inv = _draft(project, test_client, user, entity, client_entity=bill_to)
    if expected is None:
        assert not inv.invoice_number.startswith("DRAFT-")  # numbered from Settings when created
        number = inv.invoice_number
        _issue(inv)
        assert inv.invoice_number == number and inv.printed_number is None
    else:
        assert inv.invoice_number.startswith("DRAFT-")
        _issue(inv)
        assert (inv.invoice_number, inv.printed_number) == (expected, "1")


# --- every way the status changes ----------------------------------------------------------------------


def test_record_payment_on_a_draft_gives_the_number(app, project, test_client, user, entity):
    from app.models import InvoiceItem

    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    inv = _draft(project, test_client, user, entity)
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Work", quantity=1, unit_price=Decimal("50")))
    inv.calculate_totals()
    inv.record_payment(inv.total_amount)
    db.session.commit()
    assert (inv.status, inv.invoice_number) == ("paid", "CLIENTA-1")


def test_repository_mark_as_sent_and_paid_give_the_number(app, project, test_client, user, entity):
    from app.repositories import InvoiceRepository

    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    a = _draft(project, test_client, user, entity)
    b = _draft(project, test_client, user, entity)
    InvoiceRepository().mark_as_sent(a.id)
    InvoiceRepository().mark_as_paid(b.id)
    db.session.commit()
    assert {a.invoice_number, b.invoice_number} == {"CLIENTA-1", "CLIENTA-2"}


def test_service_mark_as_paid_with_a_repeat_fails_cleanly(app, project, test_client, user, entity):
    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    _issued(project, test_client, user, entity, "OLD-20", printed="20")
    inv = _draft(project, test_client, user, entity)
    result = InvoiceService().mark_as_paid(inv.id)
    assert not result["success"] and result["error"] == "duplicate_printed_number"
    assert "issue the invoice with Mark as issued first" in result["message"]
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).status == "draft"


def test_number_just_taken_when_the_database_is_locked(app, project, test_client, user, entity, monkeypatch):
    from sqlalchemy.exc import OperationalError

    _series(entity, "CLIENTA-{SEQ}", client=test_client)
    inv = _draft(project, test_client, user, entity)

    def _locked(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(billing, "_locked_last_seq", _locked)
    result = _issue(inv)
    assert not result["success"] and result["error"] == "number_just_taken"
    assert result["message"] == "This number was just taken, try again"
    monkeypatch.undo()
    db.session.expire_all()
    inv = db.session.get(Invoice, inv.id)
    assert inv.status == "draft"
    assert _issue(inv)["success"] and inv.invoice_number == "CLIENTA-1"


def test_restart_series_without_year_and_with_month_token_is_refused(app, entity):
    s = BillingEntityNumberSeries(billing_entity_id=entity.id, status="active")
    errors, _warnings = billing.check_series(s, "RU-{MM}-{SEQ}", "{SEQ}", True, {date.today().year: 1})
    messages = " ".join(e["message"] for e in errors)
    assert "needs {YYYY} or {YY}" in messages and "{MM} is not allowed" in messages


def test_switching_restart_on_needs_next_for_this_year(app, project, test_client, user, entity):
    series = _series(entity, "RU-{YYYY}-{SEQ}")
    inv = _draft(project, test_client, user, entity)
    _issue(inv)
    errors, _ = billing.check_series(series, "RU-{YYYY}-{SEQ}", "{NUMBER}", True, {date.today().year: None})
    assert any("set Next {SEQ} for this year" in e["message"] for e in errors)
    errors, _ = billing.check_series(series, "RU-{YYYY}-{SEQ}", "{NUMBER}", True, {date.today().year: 5})
    assert not errors


def test_overlapping_formats_warn(app, entity, test_client):
    _series(entity, "AB{SEQ}")
    s = BillingEntityNumberSeries(billing_entity_id=entity.id, client_id=test_client.id, status="active")
    errors, warnings = billing.check_series(s, "AB{YY}{SEQ}", "{NUMBER}", False, {0: 1})
    assert not errors
    assert "AB261 is both" in warnings[0]["message"]


def test_failed_issue_leaves_the_session_usable(app, project, test_client, user, entity):
    """After the hook raised and the path rolled back, the next request and safe_commit work."""
    from app.utils.db import safe_commit

    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    _issued(project, test_client, user, entity, "OLD-20", printed="20")
    inv = _draft(project, test_client, user, entity)
    assert _issue(inv)["error"] == "duplicate_printed_number"
    inv = db.session.get(Invoice, inv.id)
    inv.notes = "still a draft"
    assert safe_commit("after_failed_issue", {"invoice_id": inv.id})
    db.session.expire_all()
    assert db.session.get(Invoice, inv.id).notes == "still a draft"


def test_a_failed_flush_leaves_no_number_behind(app, project, test_client, user, entity):
    """Two drafts in one flush: the second repeats a printed number; after the rollback nothing is skipped."""
    _series(entity, "CLIENTA-{SEQ}", client=test_client, printed="{SEQ}", next_seq=20)
    _issued(project, test_client, user, entity, "OLD-21", printed="21")
    first = _draft(project, test_client, user, entity)
    second = _draft(project, test_client, user, entity)
    first.status = "issued"
    second.status = "issued"
    with pytest.raises(billing.DuplicatePrintedNumber):
        db.session.flush()
    db.session.rollback()
    first = db.session.get(Invoice, first.id)
    assert _issue(first)["success"]
    assert first.invoice_number == "CLIENTA-20"


# --- notices, list previews, Next {SEQ} and texts ---------------------------------------------------


def test_list_preview_costs_the_same_for_ten_drafts(app, project, test_client, user, entity):
    from sqlalchemy import event

    _series(entity, "CLIENTA-{SEQ}", client=test_client, next_seq=20)
    ids = [_draft(project, test_client, user, entity).id for _ in range(10)]
    drafts = Invoice.query.filter(Invoice.id.in_(ids)).order_by(Invoice.id).all()  # as a list page loads them

    def count(invoices):
        statements = []

        def seen(*args):
            statements.append(1)

        engine = db.engine
        event.listen(engine, "before_cursor_execute", seen)
        try:
            cache = {}
            numbers = [billing.will_be_number(inv, cache) for inv in invoices]
        finally:
            event.remove(engine, "before_cursor_execute", seen)
        return numbers, len(statements)

    one, queries_one = count(drafts[:1])
    ten, queries_ten = count(drafts)
    assert one == ["CLIENTA-20"] and set(ten) == {"CLIENTA-20"}
    assert queries_ten == queries_one


def test_next_seq_can_not_go_below_the_last_number_given(app, project, test_client, user, entity):
    series = _series(entity, "CLIENTA-{SEQ}", client=test_client)
    first = _draft(project, test_client, user, entity)
    _issue(first)
    third = _draft(project, test_client, user, entity, number="CLIENTA-3")
    _issue(third)
    assert (first.invoice_number, third.invoice_number) == ("CLIENTA-1", "CLIENTA-3")
    # 2 is a gap below the last number given: Next can't go back into it
    errors, _ = billing.check_series(series, "CLIENTA-{SEQ}", "{NUMBER}", False, {0: 2})
    assert [(e["field"], e["use"]) for e in errors] == [("next_seq_0", 4)]
    assert "above 3" in errors[0]["message"]
    errors, _ = billing.check_series(series, "CLIENTA-{SEQ}", "{NUMBER}", False, {0: 4})
    assert errors == []
