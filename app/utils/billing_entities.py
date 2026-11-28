"""Helpers for the optional Legal entities module (billing entities and payment accounts).

- which entity and payment account a new invoice defaults to;
- the invoice number from an entity's number format;
- copying the seller details and payment text into an invoice;
- a Settings view for the PDF generators that prints the invoice's own seller.
"""

import html
import logging
import re
from datetime import date

from app import db

logger = logging.getLogger(__name__)

MODULE_ID = "billing_entities"
_TOKEN = re.compile(r"\{(YYYY|YY|MM|DD|SEQ)\}")


def module_on(user=None):
    """True when the Legal entities module is enabled (for the given or current user)."""
    try:
        from app.models import Settings
        from app.utils.module_registry import ModuleRegistry

        return ModuleRegistry.is_enabled(MODULE_ID, Settings.get_settings(), user)
    except Exception:
        return False


def active_entities():
    from app.models import BillingEntity

    return BillingEntity.query.filter_by(status="active").order_by(BillingEntity.short_name).all()


def default_entity_ids_by_client():
    """{client_id: billing_entity_id of that client's latest invoice that has one}."""
    from app.models import Invoice

    rows = (
        db.session.query(Invoice.client_id, Invoice.billing_entity_id)
        .filter(Invoice.client_id.isnot(None), Invoice.billing_entity_id.isnot(None))
        .order_by(Invoice.issue_date.asc(), Invoice.id.asc())
        .all()
    )
    out = {}
    for client_id, entity_id in rows:  # later rows win
        out[client_id] = entity_id
    return out


def default_entity_for_client(client_id):
    from app.models import BillingEntity

    entity_id = default_entity_ids_by_client().get(client_id)
    entity = BillingEntity.query.get(entity_id) if entity_id else None
    if entity and entity.is_active:
        return entity
    return None


def billing_period(issue_date=None):
    """The month an invoice is for: the month before the issue date (invoices go out after month end)."""
    d = issue_date or date.today()
    if d.month == 1:
        return d.year - 1, 12
    return d.year, d.month - 1


def format_invoice_number(entity, issue_date=None, seq=None, period=None):
    """Fill an entity's number format. {YYYY}/{YY}/{MM} are the billing period, {DD} the issue day.

    ``period``: a day in the month the invoice bills (e.g. the end of its period); without it the
    billing period is the month before the issue date.
    """
    fmt = (entity.number_format or "").strip()
    if not fmt:
        return None
    d = issue_date or date.today()
    year, month = (period.year, period.month) if period else billing_period(d)
    values = {
        "YYYY": f"{year:04d}",
        "YY": f"{year % 100:02d}",
        "MM": f"{month:02d}",
        "DD": f"{d.day:02d}",
        "SEQ": str(seq if seq is not None else (entity.number_counter or 0) + 1),
    }
    return _TOKEN.sub(lambda m: values[m.group(1)], fmt)


def number_owner(number, exclude_invoice_id=None):
    """The invoice that already has this number, or None."""
    from app.models import Invoice

    if not number:
        return None
    q = Invoice.query.filter(Invoice.invoice_number == number)
    if exclude_invoice_id:
        q = q.filter(Invoice.id != exclude_invoice_id)
    return q.order_by(Invoice.id.asc()).first()


def _number_taken(number, exclude_invoice_id=None):
    return number_owner(number, exclude_invoice_id) is not None


def unique_number(number, exclude_invoice_id=None):
    """number, or number-2, number-3, … whichever is free."""
    if not _number_taken(number, exclude_invoice_id):
        return number
    n = 2
    while _number_taken(f"{number}-{n}", exclude_invoice_id):
        n += 1
    return f"{number}-{n}"


class NumberTaken(Exception):
    """The invoice number is already used by another invoice.

    ``owner`` is that invoice, ``next_free`` the number the form offers instead.
    """

    def __init__(self, number, owner, next_free):
        super().__init__(number)
        self.number = number
        self.owner = owner
        self.next_free = next_free


def _uses_seq(entity):
    return "{SEQ}" in (entity.number_format or "")


def next_free_number(entity, issue_date=None, number=None, period=None):
    """The number to offer when ``number`` (default: the entity's next number) is taken.

    With {SEQ} in the format: the first free number after the entity's counter. Otherwise
    ``number-2``, ``number-3``, … whichever is free.
    """
    if _uses_seq(entity):
        seq = (entity.number_counter or 0) + 1
        while _number_taken(format_invoice_number(entity, issue_date, seq=seq, period=period)):
            seq += 1
        return format_invoice_number(entity, issue_date, seq=seq, period=period)
    base = number or format_invoice_number(entity, issue_date, period=period)
    return unique_number(base) if base else None


def _seq_of(entity, number, issue_date=None, period=None):
    """The {SEQ} value if ``number`` is the entity's format filled for this issue date, else None."""
    if not number or not _uses_seq(entity):
        return None
    marker = "\x00"
    pattern = re.escape(format_invoice_number(entity, issue_date, seq=marker, period=period)).replace(
        re.escape(marker), r"(\d+)"
    )
    match = re.fullmatch(pattern, number)
    return int(match.group(1)) if match else None


def check_number(entity, number, issue_date=None, exclude_invoice_id=None, period=None):
    """Raise NumberTaken if ``number`` belongs to another invoice."""
    owner = number_owner(number, exclude_invoice_id)
    if owner is not None:
        offer = (
            next_free_number(entity, issue_date, number, period=period)
            if entity
            else unique_number(number, exclude_invoice_id)
        )
        raise NumberTaken(number, owner, offer)


def claim_number(entity, number, issue_date=None, period=None):
    """Move the entity's {SEQ} counter up to ``number`` if it is the format's number past the counter.

    So a number typed by hand, or picked with "Use next free", is not offered again. The caller commits.
    """
    seq = _seq_of(entity, number, issue_date, period=period)
    if seq is not None and seq > (entity.number_counter or 0):
        entity.number_counter = seq


def preview_invoice_number(entity, issue_date=None, period=None):
    """The entity's next number as the form shows it (it may be taken: see ``number_preview``)."""
    return format_invoice_number(entity, issue_date, period=period)


def allocate_invoice_number(entity, issue_date=None, period=None):
    """Next number for an invoice from ``entity``, or None if the entity has no format.

    Raises NumberTaken when that number already belongs to another invoice. Bumps the entity's
    counter when the format uses {SEQ}, so it moves only together with the invoice. The caller commits.
    """
    fmt = (entity.number_format or "").strip()
    if not fmt:
        return None
    if _uses_seq(entity):
        seq = (entity.number_counter or 0) + 1
        number = format_invoice_number(entity, issue_date, seq=seq, period=period)
    else:
        seq = None
        number = format_invoice_number(entity, issue_date, period=period)
    check_number(entity, number, issue_date, period=period)
    if seq is not None:
        entity.number_counter = seq
    return number


def invoice_ref(invoice):
    """What the form shows about an invoice (or a row with its number, client name, issue date)."""
    from flask import current_app

    fmt = current_app.jinja_env.filters.get("format_date")
    issued = invoice.issue_date
    return {
        "number": invoice.invoice_number,
        "client": invoice.client_name or "",
        "date": (fmt(issued) if fmt else issued.isoformat()) if issued else "",
    }


def number_preview(entity, issue_date=None, period=None):
    """The entity's next number for the form, with the invoice holding it and the next free one."""
    number = preview_invoice_number(entity, issue_date, period=period)
    owner = number_owner(number) if number else None
    return {
        "number": number or "",
        "taken": invoice_ref(owner) if owner else None,
        "next_free": next_free_number(entity, issue_date, number, period=period) if owner else "",
    }


def _ref_columns(Invoice):
    return (Invoice.invoice_number, Invoice.client_name, Invoice.issue_date)


def latest_invoice_by_entity():
    """{billing_entity_id: number, client and issue date of its latest invoice}."""
    from app.models import Invoice

    rows = (
        db.session.query(Invoice.billing_entity_id, *_ref_columns(Invoice))
        .filter(Invoice.billing_entity_id.isnot(None))
        .order_by(Invoice.issue_date.asc(), Invoice.id.asc())
        .all()
    )
    out = {}
    for row in rows:  # later rows win
        out[row.billing_entity_id] = row
    return out


def latest_invoice_by_client(client_ids):
    """{client_id: number, client and issue date of that client's latest invoice}."""
    from app.models import Invoice

    if not client_ids:
        return {}
    rows = (
        db.session.query(Invoice.client_id, *_ref_columns(Invoice))
        .filter(Invoice.client_id.in_(client_ids))
        .order_by(Invoice.issue_date.asc(), Invoice.id.asc())
        .all()
    )
    out = {}
    for row in rows:  # later rows win
        out[row.client_id] = row
    return out


def entity_defaults(entity):
    """Form defaults for a new invoice from this entity (Notes have none: they differ on every invoice)."""
    return {
        "vat_category": entity.default_vat_category or "",
        "tax_rate": str(entity.default_tax_rate) if entity.default_tax_rate is not None else "",
        "vat_exemption_code": entity.default_vat_exemption_code or "",
        "vat_exemption_reason": entity.default_vat_exemption_reason or "",
        "terms": entity.default_terms or "",
    }


def form_context(projects, settings, issue_date=None, period=None):
    """Data the invoice create form needs for From / Pay to (empty when the module is off).

    ``issue_date`` / ``period``: the dates the number previews are for (see ``format_invoice_number``).
    """
    if not module_on():
        return {"billing_enabled": False}
    entities = active_entities()
    if not entities:
        return {"billing_enabled": False}
    by_client = default_entity_ids_by_client()
    latest_by_entity = latest_invoice_by_entity()
    data = {}
    for e in entities:
        previous = latest_by_entity.get(e.id)
        data[e.id] = {
            "name": e.short_name,
            **number_preview(e, issue_date, period=period),
            "previous": invoice_ref(previous) if previous else None,
            "defaults": entity_defaults(e),
            "accounts": [
                {"id": a.id, "currency": a.currency, "label": a.label, "text": a.printed_text}
                for a in e.active_payment_accounts()
            ],
            "seller": e.seller_snapshot(),
        }
    project_clients = {str(p.id): p.client_id for p in (projects or [])}
    latest_by_client = latest_invoice_by_client(sorted({c for c in project_clients.values() if c}))
    return {
        "billing_enabled": True,
        "billing_entities": entities,
        "billing_entity_data": data,
        "billing_entity_by_client": {str(k): v for k, v in by_client.items()},
        "billing_project_clients": project_clients,
        "billing_last_by_client": {str(k): invoice_ref(v) for k, v in latest_by_client.items()},
    }


def resolve_from_form(form, currency_code):
    """(entity, account, error) from the posted billing_entity_id / payment_account_id."""
    from app.models import BillingEntity, PaymentAccount

    entity_id = form.get("billing_entity_id", type=int) if hasattr(form, "get") else None
    if not entity_id:
        return None, None, None
    entity = BillingEntity.query.get(entity_id)
    if not entity or not entity.is_active:
        return None, None, "Selected legal entity not found"
    account_id = form.get("payment_account_id", type=int)
    account = PaymentAccount.query.get(account_id) if account_id else None
    if account and (account.billing_entity_id != entity.id or not account.is_active):
        return None, None, "Selected payment account does not belong to the legal entity"
    if account and currency_code and account.currency != currency_code:
        return None, None, "Payment account currency does not match the invoice currency"
    return entity, account, None


def apply_to_invoice(invoice, entity, account):
    """Link the invoice to its seller and payment account and copy their texts into it."""
    invoice.billing_entity_id = entity.id if entity else None
    invoice.payment_account_id = account.id if account else None
    invoice.seller_snapshot = entity.seller_snapshot() if entity else None
    invoice.payment_details_text = account.printed_text if account else None


class SellerSettings:
    """Settings as seen by the PDF generators for one invoice: the seller comes from the invoice.

    Everything not about the seller (currency, templates, PEPPOL, …) is read from the real Settings.
    """

    def __init__(self, settings, snapshot, payment_text, iban):
        self._settings = settings
        self._snapshot = snapshot or {}
        self._payment_text = payment_text or ""
        self.payment_text = self._payment_text  # as typed, for HTML templates (company_bank_info is escaped)
        self._iban = iban or ""

    def __getattr__(self, name):
        snap = self._snapshot
        if name == "company_name":
            return snap.get("legal_name") or ""
        if name == "company_address":
            return snap.get("address") or ""
        if name == "company_email":
            return snap.get("email") or ""
        if name == "company_phone":
            return snap.get("phone") or ""
        if name == "company_website":
            return snap.get("website") or ""
        if name == "company_tax_id":
            return snap.get("vat_number") or snap.get("registration_number") or ""
        if name == "company_country":
            return snap.get("country") or ""
        if name in ("company_street", "company_postcode", "company_city", "company_bic"):
            return ""
        if name == "company_iban":
            return self._iban
        if name == "company_bank_info":
            # Printed as HTML / ReportLab markup by the generators: escape, keep line breaks.
            return html.escape(self._payment_text).replace("\n", "<br/>")
        if name == "company_logo_filename":
            return self._snapshot.get("logo_filename") or ""
        return getattr(self._settings, name)

    def _logo_path(self):
        from app.models import BillingEntity

        return BillingEntity.logo_path(self._snapshot.get("logo_filename"))

    def has_logo(self):
        return self._logo_path() is not None

    def get_logo_path(self):
        return self._logo_path()

    def get_logo_url(self):
        name = self._snapshot.get("logo_filename")
        return f"/uploads/logos/{name}" if name and self._logo_path() else None


def seller_settings_for(invoice, settings):
    """Settings to render ``invoice`` with: its own seller when it has one, else ``settings``."""
    try:
        if invoice is None or not getattr(invoice, "billing_entity_id", None) or not invoice.seller_snapshot:
            return settings
        iban = invoice.payment_account.iban if getattr(invoice, "payment_account", None) else ""
        return SellerSettings(settings, invoice.seller_snapshot, invoice.payment_details_text, iban)
    except Exception:
        return settings


# --- number series ----------------------------------------------------------------------------------
#
# With the module on, an entity's invoices are numbered by its series (app.models.billing_entity_number_series):
# the series of the invoice's Bill to, else the client's series ("Any legal entity"), else the entity's
# default series. A draft holds a placeholder number, DRAFT-<id>; the series number is given when the
# invoice leaves draft, in a ``before_flush`` hook, so every path that changes the status gets it and a
# deleted draft leaves no gap. An entity with no series at all keeps the entity's own number format,
# given when the draft is created (as before series existed).

# session.info keys: drafts issued in the open transaction / in committed ones, to notify their client
_NOTIFY_PENDING = "invoice_numbering_notify"
_NOTIFY_READY = "invoice_numbering_notify_ready"

DRAFT_PREFIX = "DRAFT-"
CLIENT_ENTITIES_MODULE_ID = "client_entities"
SERIES_TOKENS = ("YYYY", "YY", "MM", "DD", "SEQ")
PRINTED_TOKENS = SERIES_TOKENS + ("NUMBER",)
_ANY_TOKEN = re.compile(r"\{([A-Z]+)\}")
_PRINTED_TOKEN = re.compile(r"\{(YYYY|YY|MM|DD|SEQ|NUMBER)\}")
_NOT_ISSUED = ("draft", "cancelled")


def default_issue_date():
    """Issue date of a new invoice when none is given: today (UTC)."""
    from app.utils.invoice_numbering import default_issue_date as _default

    return _default()


def _module_on_by_settings(module_id, settings):
    """Whether a module is on for the whole instance (Settings only, whoever is signed in)."""
    from app.utils.module_registry import ModuleCategory, ModuleRegistry

    module = ModuleRegistry.get(module_id)
    if not module:
        return False
    if module.category == ModuleCategory.CORE:
        return True
    disabled = getattr(settings, "disabled_module_ids", None) or []
    if isinstance(disabled, list) and module_id in disabled:
        return False
    return all(_module_on_by_settings(dep, settings) for dep in module.dependencies)


def _numbering_settings():
    from app.models import Settings

    with db.session.no_autoflush:
        return Settings.query.first()


def numbering_on(settings=None):
    """True when invoice numbers come from the entities' series: the module is on in Settings.

    Not per user: two people with different module settings must not number one entity two ways.
    """
    try:
        settings = settings if settings is not None else _numbering_settings()
        return _module_on_by_settings(MODULE_ID, settings)
    except Exception:
        return False


def bill_to_entity_id(invoice, settings=None):
    """The invoice's Bill to legal entity of the client, when the Client legal entities module is on.

    The hook through which this module reads the other one: it never imports its models.
    """
    entity_id = getattr(invoice, "client_entity_id", None)
    if not entity_id:
        return None
    try:
        settings = settings if settings is not None else _numbering_settings()
        return entity_id if _module_on_by_settings(CLIENT_ENTITIES_MODULE_ID, settings) else None
    except Exception:
        return None


def is_draft_placeholder(number):
    return bool(number) and str(number).startswith(DRAFT_PREFIX)


def entity_has_series(entity_id):
    """True when the entity numbers by series (it has any, active or archived)."""
    from app.models import BillingEntityNumberSeries

    if not entity_id:
        return False
    with db.session.no_autoflush:
        return (
            db.session.query(BillingEntityNumberSeries.id)
            .filter(BillingEntityNumberSeries.billing_entity_id == entity_id)
            .first()
            is not None
        )


def resolve_series(entity_id, client_id, client_entity_id=None):
    """The active series an invoice from ``entity_id`` to this client takes, or None.

    The series of the Bill to legal entity, else the client's series ("Any legal entity"), else the
    entity's default series. ``client_entity_id``: pass ``bill_to_entity_id(invoice)``.
    """
    from app.models import BillingEntityNumberSeries as Series

    if not entity_id:
        return None
    with db.session.no_autoflush:
        query = Series.query.filter(Series.billing_entity_id == entity_id, Series.status == "active")
        if client_id and client_entity_id:
            found = query.filter(Series.client_id == client_id, Series.client_entity_id == client_entity_id).first()
            if found:
                return found
        if client_id:
            found = query.filter(Series.client_id == client_id, Series.client_entity_id.is_(None)).first()
            if found:
                return found
        return query.filter(Series.client_id.is_(None)).first()


def series_for_invoice(invoice, settings=None):
    """The series ``invoice`` takes now, or None (module off, no entity, or no series for it)."""
    settings = settings if settings is not None else _numbering_settings()
    if not getattr(invoice, "billing_entity_id", None) or not numbering_on(settings):
        return None
    return resolve_series(invoice.billing_entity_id, invoice.client_id, bill_to_entity_id(invoice, settings))


def _series_values(series, seq, issue_date, period):
    d = issue_date or default_issue_date()
    if series.reset_yearly:
        # The counter restarts with the issue date's year, so {YYYY} is that year too
        year = d.year
        month = period.month if period else billing_period(d)[1]
    else:
        year, month = (period.year, period.month) if period else billing_period(d)
    return {
        "YYYY": f"{year:04d}",
        "YY": f"{year % 100:02d}",
        "MM": f"{month:02d}",
        "DD": f"{d.day:02d}",
        "SEQ": "" if seq is None else str(seq),
    }


def format_series_number(series, seq, issue_date, period=None):
    """The series' number for this {SEQ} and issue date.

    ``period``: a day in the month the invoice bills (``invoice.period_end``), for {YYYY} {YY} {MM} of a
    series without a yearly restart (as the entity's format); with a restart they are the issue date's.
    """
    values = _series_values(series, seq, issue_date, period)
    return _TOKEN.sub(lambda m: values[m.group(1)], series.number_format or "")


def _number_pattern(series, issue_date, period):
    marker = "\x00"
    filled = format_series_number(series, marker, issue_date, period)
    return re.escape(filled).replace(re.escape(marker), r"(\d+)")


def series_seq_of(series, number, issue_date, period=None):
    """The {SEQ} of ``number`` if it is the series' format for this issue date, else None."""
    if not number or "{SEQ}" not in (series.number_format or ""):
        return None
    match = re.fullmatch(_number_pattern(series, issue_date, period), number)
    return int(match.group(1)) if match else None


def number_fits_series(series, number, issue_date, period=None):
    """True when ``number`` is one the series makes (a format without {SEQ}: the number or number-N)."""
    if "{SEQ}" in (series.number_format or ""):
        return series_seq_of(series, number, issue_date, period) is not None
    base = format_series_number(series, None, issue_date, period)
    return bool(number) and re.fullmatch(re.escape(base) + r"(-\d+)?", number) is not None


def format_printed_number(series, number, seq, issue_date, period=None):
    """What the client sees for ``number``: the series' printed format, or the number when it doesn't fit."""
    if series is None or not number_fits_series(series, number, issue_date, period):
        return number
    values = _series_values(series, seq, issue_date, period)
    values["NUMBER"] = number
    return _PRINTED_TOKEN.sub(lambda m: values[m.group(1)], series.printed_format or "{NUMBER}") or number


# --- counters -----------------------------------------------------------------------------------------


def _counter_table():
    from app.models import BillingEntityNumberCounter

    return BillingEntityNumberCounter.__table__


def last_seq(series, year=0):
    """The last {SEQ} the series gave in ``year`` (0 without a yearly restart); 0 when none."""
    table = _counter_table()
    with db.session.no_autoflush:
        row = db.session.execute(
            db.select(table.c.last_seq).where(table.c.series_id == series.id, table.c.year == year)
        ).first()
    return row[0] if row else 0


def _locked_last_seq(conn, series_id, year):
    """The counter row's value, locked until the transaction ends (Postgres); None when there is no row."""
    table = _counter_table()
    row = conn.execute(
        db.select(table.c.last_seq).where(table.c.series_id == series_id, table.c.year == year).with_for_update()
    ).first()
    return row[0] if row else None


def _insert_counter(conn, series_id, year, value):
    """INSERT … ON CONFLICT DO NOTHING; True when this call made the row."""
    table = _counter_table()
    values = {"series_id": series_id, "year": year, "last_seq": value}
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - other databases are not supported
        from sqlalchemy.exc import IntegrityError

        try:
            with conn.begin_nested():
                conn.execute(table.insert().values(**values))
            return True
        except IntegrityError:
            return False
    stmt = insert(table).values(**values).on_conflict_do_nothing(index_elements=["series_id", "year"])
    return conn.execute(stmt).rowcount == 1


def _raise_counter(conn, series_id, year, seq):
    """Move the counter up to ``seq`` (never down)."""
    table = _counter_table()
    if _locked_last_seq(conn, series_id, year) is None and _insert_counter(conn, series_id, year, seq):
        return
    conn.execute(
        table.update()
        .where(table.c.series_id == series_id, table.c.year == year, table.c.last_seq < seq)
        .values(last_seq=seq)
    )


def claim_seq(series, year, seq):
    """Move the series' counter for ``year`` up to ``seq`` under the counter lock: max(last_seq, seq).

    For a number set by hand. The caller commits.
    """
    if seq is None:
        return
    _raise_counter(db.session.connection(), series.id, year, int(seq))


def set_last_seq(series, year, value):
    """Set the counter as typed on the series form (Next {SEQ} minus one). The caller commits."""
    table = _counter_table()
    conn = db.session.connection()
    if _locked_last_seq(conn, series.id, year) is None and _insert_counter(conn, series.id, year, value):
        return
    conn.execute(table.update().where(table.c.series_id == series.id, table.c.year == year).values(last_seq=value))


# --- errors when an invoice is issued ----------------------------------------------------------------


def N_(text):
    """Marks a text for extraction (a Babel default keyword); it is translated where it is shown."""
    return text


def _translate(text):
    try:
        from flask_babel import gettext

        return gettext(text)
    except Exception:  # no app or request context: the English text
        return text


class IssueNumberError(Exception):
    """The invoice could not get its number when it was issued. Its status change is rolled back.

    ``message`` is a fixed text for the user (``text()`` translated); the exception text is never shown.
    """

    code = "number_error"
    message = N_("The invoice number could not be assigned")

    def text(self):
        return _translate(self.message)

    def payload(self):
        return {"error": self.code, "message": self.text()}


class NumberJustTaken(IssueNumberError):
    code = "number_just_taken"
    message = N_("This number was just taken, try again")


class NumberNotAssigned(IssueNumberError):
    code = "number_not_assigned"
    message = N_("The invoice has no number yet: issue it with Mark as issued first")


class DuplicatePrintedNumber(IssueNumberError):
    """The printed number is already on another invoice of this client.

    ``printed``: the number; ``other_id``, ``other_number``, ``other_date``: the invoice that has it.
    """

    code = "duplicate_printed_number"
    message = N_("This printed number is already used for this client")

    def __init__(self, printed, number, other):
        super().__init__(printed)
        self.printed = printed
        self.number = number
        self.other_id = other.id
        self.other_number = other.invoice_number
        self.other_date = other.issue_date

    def payload(self):
        return {
            "error": self.code,
            "message": self.text(),
            "printed_number": self.printed,
            "invoice_number": self.number,
            "duplicate_of": {
                "id": self.other_id,
                "invoice_number": self.other_number,
                "issue_date": self.other_date.isoformat() if self.other_date else None,
            },
        }


# --- printed number repeated for the client ----------------------------------------------------------


def find_printed_duplicate(invoice, printed=None, issued_only=True, series=None):
    """Another invoice of the same entity and client with this printed number, or None.

    Compares ``coalesce(printed_number, invoice_number)``, so older invoices without a printed
    number count. Cancelled invoices don't. With Bill to on both invoices it must be the same legal
    entity; Bill to on one only is a repeat. ``issued_only``: only invoices that left draft (when
    issuing); the draft's warning checks drafts too. A series that restarts every year compares
    invoices of the same issue-date year only.
    """
    from datetime import date as _date

    from sqlalchemy import func, or_

    from app.models import Invoice

    printed = printed or invoice.printed_number or invoice.invoice_number
    if not printed or not invoice.billing_entity_id or not invoice.client_id:
        return None
    with db.session.no_autoflush:
        query = Invoice.query.filter(
            Invoice.billing_entity_id == invoice.billing_entity_id,
            Invoice.client_id == invoice.client_id,
            Invoice.status != "cancelled",
            func.coalesce(Invoice.printed_number, Invoice.invoice_number) == printed,
        )
        if invoice.id:
            query = query.filter(Invoice.id != invoice.id)
        if issued_only:
            query = query.filter(Invoice.status != "draft")
        bill_to = bill_to_entity_id(invoice)
        if bill_to:
            query = query.filter(or_(Invoice.client_entity_id.is_(None), Invoice.client_entity_id == bill_to))
        series = series if series is not None else getattr(invoice, "number_series", None)
        if series is not None and series.reset_yearly:
            year = (invoice.issue_date or default_issue_date()).year
            query = query.filter(Invoice.issue_date >= _date(year, 1, 1), Invoice.issue_date <= _date(year, 12, 31))
        return query.order_by(Invoice.issue_date.asc(), Invoice.id.asc()).first()


# --- drafts ---------------------------------------------------------------------------------------------


def assign_draft_placeholder(invoice):
    """Give a draft the placeholder number DRAFT-<id> (DRAFT-<random> until its id is known).

    Only for an invoice from an entity whose series will number it; returns False (and changes
    nothing) otherwise, and the caller numbers the invoice as before.
    """
    if series_for_invoice(invoice) is None:
        return False
    set_draft_placeholder(invoice)
    return True


def set_draft_placeholder(invoice):
    """The draft's number goes back to the series: DRAFT-<id>, given a series number when issued."""
    import uuid

    if invoice.id:
        invoice.invoice_number = f"{DRAFT_PREFIX}{invoice.id}"
    else:
        invoice.invoice_number = f"{DRAFT_PREFIX}{uuid.uuid4().hex[:8]}"
        invoice._draft_placeholder_pending = True
    if not invoice.printed_number_edited:
        invoice.printed_number = None
    invoice.number_series_id = None


def number_preview_for(entity_id, client_id, client_entity_id, issue_date=None, period=None, exclude_invoice_id=None):
    """What a draft from ``entity_id`` to this client and Bill to will be numbered, or None without a series.

    ``{"series_id", "series", "number", "printed", "previous", "held"}``: the number is the series'
    next free one now (another draft issued first takes it), ``previous`` the last invoice of the series.
    """
    series = resolve_series(entity_id, client_id, client_entity_id)
    if series is None:
        return None
    return series_preview(series, issue_date, period, exclude_invoice_id=exclude_invoice_id)


def series_preview(series, issue_date=None, period=None, exclude_invoice_id=None):
    issue_date = issue_date or default_issue_date()
    year = series.counter_year(issue_date)
    seq = last_seq(series, year) + 1
    number = format_series_number(series, seq, issue_date, period)
    if "{SEQ}" in (series.number_format or ""):
        while number_owner(number, exclude_invoice_id) is not None:
            seq += 1
            number = format_series_number(series, seq, issue_date, period)
    else:
        number = unique_number(number, exclude_invoice_id)
    previous = last_series_invoice(series)
    return {
        "series_id": series.id,
        "series": series_label(series),
        "number": number,
        "printed": format_printed_number(series, number, seq, issue_date, period),
        "seq": seq,
        "year": year if series.reset_yearly else None,
        "year_starts": bool(series.reset_yearly and seq == 1),
        "previous": printed_ref(previous) if previous else None,
    }


def will_be_number(invoice, cache):
    """For lists: the number a DRAFT-… draft will get, or "". Cheap per row.

    ``cache`` (a dict kept for one request) holds the settings, the active series and the next free
    {SEQ} per (series, year), so a page of drafts costs a few queries, not a few per row. The number is
    a preview: the hook decides when the draft is issued.
    """
    from app.models import BillingEntityNumberSeries as Series

    if invoice is None or invoice.status != "draft" or not is_draft_placeholder(invoice.invoice_number):
        return ""
    if not invoice.billing_entity_id:
        return ""
    if "on" not in cache:
        settings = _numbering_settings()
        cache["on"] = numbering_on(settings)
        cache["bill_to_on"] = _module_on_by_settings(CLIENT_ENTITIES_MODULE_ID, settings)
        cache["next"] = {}
        if cache["on"]:
            with db.session.no_autoflush:
                active = Series.query.filter(Series.status == "active").all()
            cache["series"] = {(s.billing_entity_id, s.client_id, s.client_entity_id): s for s in active}
    if not cache["on"]:
        return ""
    found = cache["series"]
    bill_to = invoice.client_entity_id if cache["bill_to_on"] else None
    series = (
        (found.get((invoice.billing_entity_id, invoice.client_id, bill_to)) if bill_to else None)
        or (found.get((invoice.billing_entity_id, invoice.client_id, None)) if invoice.client_id else None)
        or found.get((invoice.billing_entity_id, None, None))
    )
    if series is None:
        return ""
    issue_date = invoice.issue_date or default_issue_date()
    period = invoice.period_end
    if "{SEQ}" not in (series.number_format or ""):
        return format_series_number(series, None, issue_date, period)
    key = (series.id, series.counter_year(issue_date))
    if key not in cache["next"]:
        seq = last_seq(series, key[1]) + 1
        while number_owner(format_series_number(series, seq, issue_date, period)) is not None:
            seq += 1
        cache["next"][key] = seq
    return format_series_number(series, cache["next"][key], issue_date, period)


def last_series_invoice(series):
    from app.models import Invoice

    query = Invoice.query.filter(Invoice.number_series_id == series.id, Invoice.status != "draft")
    return query.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).first()


def printed_ref(invoice):
    """``invoice_ref`` plus the printed number and id, for hints that name an invoice."""
    ref = invoice_ref(invoice)
    ref["id"] = invoice.id
    ref["printed"] = invoice.printed_number or ""
    return ref


def series_label(series):
    """How a series is named on the forms: its Bill to or client, or the entity's default series."""
    from flask_babel import gettext as _

    entity = series.billing_entity
    if series.client_id is None:
        return _("Default series of %(entity)s", entity=entity.short_name if entity else "")
    client = series.client.name if series.client else ""
    if series.client_entity_id:
        return client_entity_name(series.client_entity_id) or client
    return client


def client_entity_name(client_entity_id):
    """Legal name of a client's legal entity, or "" when it is gone (read through the invoice's Bill to link)."""
    from app.models import Invoice

    try:
        target = Invoice.client_entity.property.mapper.class_
        entity = db.session.get(target, client_entity_id)
        return entity.legal_name if entity else ""
    except Exception:
        return ""


# --- the hook -------------------------------------------------------------------------------------------


def _leaving_draft(invoice):
    """Why the hook numbers this invoice: "placeholder", "given" or None (nothing to do)."""
    from sqlalchemy import inspect

    status = invoice.status or "draft"
    if status in _NOT_ISSUED:
        return None
    if is_draft_placeholder(invoice.invoice_number):
        return "placeholder"
    state = inspect(invoice)
    if state.pending or state.transient:
        return "given"
    deleted = state.attrs.status.history.deleted
    if deleted and any((old or "draft") == "draft" for old in deleted):
        return "given"
    return None


def _flush_state(session):
    info = session.info.setdefault("invoice_numbering", {})
    info.setdefault("last_seq", {})  # {(series_id, year): seq} given in this flush
    info.setdefault("numbers", set())  # numbers given in this flush
    return info


def numbering_before_flush(session):
    """``before_flush``: number the invoices that leave draft (see ``allocate_for_invoice``)."""
    from app.models import Invoice

    # Bookkeeping of one flush only: a flush that failed must not leave numbers behind
    session.info.pop("invoice_numbering", None)
    session.info.pop("invoice_drafts_to_name", None)
    leaving = []
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Invoice):
            continue
        if obj in session.new and getattr(obj, "_draft_placeholder_pending", False):
            session.info.setdefault("invoice_drafts_to_name", []).append(obj)
        if (obj.billing_entity_id or is_draft_placeholder(obj.invoice_number)) and _leaving_draft(obj):
            leaving.append(obj)
    if not leaving:
        return
    with session.no_autoflush:
        settings = _numbering_settings()
        if not numbering_on(settings):
            # No series to number from: a DRAFT-… that leaves draft would reach the client as it is
            if any(_leaving_draft(invoice) == "placeholder" for invoice in leaving):
                raise NumberNotAssigned()
            return
        for invoice in leaving:
            placeholder = is_draft_placeholder(invoice.invoice_number)
            allocate_for_invoice(invoice, settings=settings)
            if placeholder and invoice.id and invoice.client_id:
                # The client was not told about the draft (DRAFT-…): it is told now, with the number
                session.info.setdefault(_NOTIFY_PENDING, []).append(invoice.id)


def numbering_after_commit(session):
    """``after_commit``: the issued drafts of this transaction wait for ``send_issue_notifications``."""
    pending = session.info.pop(_NOTIFY_PENDING, None)
    if pending:
        session.info.setdefault(_NOTIFY_READY, []).extend(pending)


def numbering_after_rollback(session):
    """``after_rollback``: nothing was issued."""
    session.info.pop(_NOTIFY_PENDING, None)


def send_issue_notifications():
    """Notify clients about invoices issued from a DRAFT-… draft, after the commit that issued them.

    Called when a request or an app context ends: the commit itself can't send (no SQL in
    ``after_commit``). Each invoice is checked again, so one rolled back to a savepoint is skipped.
    """
    if not db.session.registry.has():
        return
    session = db.session()
    ready = session.info.pop(_NOTIFY_READY, None)
    if not ready:
        return
    from app.models import Invoice
    from app.services.client_notification_service import ClientNotificationService

    if session.new or session.dirty or session.deleted:
        # Changes nobody committed are dropped when the request ends anyway; the notice must not commit them
        session.rollback()
    for invoice_id in dict.fromkeys(ready):
        try:
            invoice = db.session.get(Invoice, invoice_id)
            if invoice is None or not invoice.client_id or invoice.status in _NOT_ISSUED:
                continue
            if is_draft_placeholder(invoice.invoice_number):
                continue
            ClientNotificationService().notify_invoice_created(invoice.id, invoice.client_id)
        except Exception:
            logger.exception("Could not notify the client about issued invoice %s", invoice_id)
            db.session.rollback()


def numbering_after_flush(session):
    """``after_flush_postexec``: new drafts become DRAFT-<id>; this flush's bookkeeping is cleared."""
    session.info.pop("invoice_numbering", None)
    drafts = session.info.pop("invoice_drafts_to_name", None) or []
    for invoice in drafts:
        invoice._draft_placeholder_pending = False
        if invoice.id and is_draft_placeholder(invoice.invoice_number):
            invoice.invoice_number = f"{DRAFT_PREFIX}{invoice.id}"


def _number_holder(number, invoice_id, state):
    """(id, number, status) of the invoice holding ``number``, or None; numbers given in this flush count."""
    from app.models import Invoice

    if number in state["numbers"]:
        return (None, number, None)
    row = (
        db.session.query(Invoice.id, Invoice.status)
        .filter(Invoice.invoice_number == number, Invoice.id != (invoice_id or 0))
        .first()
    )
    return (row.id, number, row.status) if row else None


def _replace_number_copies(old, new):
    """Copies of the draft's number (time entries) follow the number it gets."""
    from app.models import TimeEntry

    if not old or old == new:
        return
    for obj in list(db.session.new) + list(db.session.dirty):
        if isinstance(obj, TimeEntry) and obj.invoice_number == old:
            obj.invoice_number = new
    for entry in TimeEntry.query.filter(TimeEntry.invoice_number == old).all():
        entry.invoice_number = new


def _check_repeat(invoice, printed, series):
    """Raise DuplicatePrintedNumber unless the person confirmed exactly this printed number."""
    other = find_printed_duplicate(invoice, printed, issued_only=True, series=series)
    if other is None:
        return None
    confirmed = getattr(invoice, "_confirm_duplicate_printed", None)
    if confirmed is not None and str(confirmed).strip() == printed:
        return other
    raise DuplicatePrintedNumber(printed, invoice.invoice_number, other)


def allocate_for_invoice(invoice, settings=None):
    """Number an invoice that leaves draft. Called by the ``before_flush`` hook; checks first, writes last.

    - DRAFT-… and not a draft: the series' next free {SEQ} under the counter lock; a number another
      invoice holds (a draft set by hand) is skipped and reported.
    - Leaving draft with a number already set (Set number now, an older draft, a reissue): the number
      stays; if it is the series' format its {SEQ} is claimed (the counter only moves forward). The
      printed number is filled only when empty and not edited.

    Either way a printed number already used for this client raises DuplicatePrintedNumber unless
    ``invoice._confirm_duplicate_printed`` equals it. Does nothing with the module off or for an entity
    without series. Raises IssueNumberError; the caller rolls back.
    """
    from sqlalchemy.exc import OperationalError

    from app.models import BillingEntityNumberSeries

    kind = _leaving_draft(invoice)
    if kind is None:
        return None
    if not invoice.billing_entity_id:
        if kind == "placeholder":
            raise NumberNotAssigned()
        return None
    settings = settings if settings is not None else _numbering_settings()
    if not numbering_on(settings):
        if kind == "placeholder":
            raise NumberNotAssigned()
        return None
    if not entity_has_series(invoice.billing_entity_id):
        if kind == "placeholder":
            raise NumberNotAssigned()
        return None
    series = resolve_series(invoice.billing_entity_id, invoice.client_id, bill_to_entity_id(invoice, settings))
    if series is None and invoice.number_series_id:
        series = db.session.get(BillingEntityNumberSeries, invoice.number_series_id)
    if series is None:
        if kind == "placeholder":
            raise NumberNotAssigned()
        return None
    issue_date = invoice.issue_date or default_issue_date()
    period = invoice.period_end
    year = series.counter_year(issue_date)
    state = _flush_state(db.session)
    try:
        conn = db.session.connection()
        # The counter row exists (INSERT … ON CONFLICT DO NOTHING, value 0: no number is taken), then it is
        # locked (SELECT … FOR UPDATE) until the transaction ends: issuing in one series is serialized
        _insert_counter(conn, series.id, year, 0)
        locked = _locked_last_seq(conn, series.id, year)
        if kind == "placeholder":
            result = _allocate_next(invoice, series, issue_date, period, year, locked, state)
        else:
            result = _keep_given(invoice, series, issue_date, period, year, locked, state)
    except OperationalError:
        # SQLite: another writer holds the database ("database is locked")
        raise NumberJustTaken()
    invoice._numbering_result = result
    return result


def _allocate_next(invoice, series, issue_date, period, year, locked, state):
    base = max(locked or 0, state["last_seq"].get((series.id, year), 0))
    held_by = None
    if "{SEQ}" in (series.number_format or ""):
        seq = base + 1
        number = format_series_number(series, seq, issue_date, period)
        while True:
            holder = _number_holder(number, invoice.id, state)
            if holder is None:
                break
            if holder[0] and held_by is None:
                held_by = holder
            seq += 1
            number = format_series_number(series, seq, issue_date, period)
    else:
        seq = None
        base_number = format_series_number(series, None, issue_date, period)
        number = base_number
        n = 1
        while _number_holder(number, invoice.id, state) is not None:
            n += 1
            number = f"{base_number}-{n}"
    if invoice.printed_number_edited and invoice.printed_number:
        printed = invoice.printed_number
    else:
        printed = format_printed_number(series, number, seq, issue_date, period)
    old_number = invoice.invoice_number
    invoice.invoice_number = number  # the repeat check names this number
    try:
        repeated = _check_repeat(invoice, printed, series)
    except IssueNumberError:
        invoice.invoice_number = old_number
        raise
    # Checks passed: write
    if seq is not None:
        _raise_counter(db.session.connection(), series.id, year, seq)
        state["last_seq"][(series.id, year)] = seq
    state["numbers"].add(number)
    invoice.printed_number = printed
    invoice.number_series_id = series.id
    _replace_number_copies(old_number, number)
    said = getattr(invoice, "_numbering_expected", None)
    return {
        "number": number,
        "printed": printed,
        "held_by": held_by,
        "draft_said": said if said and said != number else None,
        "repeated": printed_ref(repeated) if repeated is not None else None,
    }


def _keep_given(invoice, series, issue_date, period, year, locked, state):
    number = invoice.invoice_number
    seq = series_seq_of(series, number, issue_date, period)
    printed = invoice.printed_number
    if not printed and not invoice.printed_number_edited:
        printed = format_printed_number(series, number, seq, issue_date, period)
    repeated = _check_repeat(invoice, printed or number, series)
    # Checks passed: write
    if seq is not None and seq > max(locked or 0, state["last_seq"].get((series.id, year), 0)):
        _raise_counter(db.session.connection(), series.id, year, seq)
        state["last_seq"][(series.id, year)] = seq
    if printed and printed != invoice.printed_number:
        invoice.printed_number = printed
    if not invoice.number_series_id:
        invoice.number_series_id = series.id
    return {
        "number": number,
        "printed": invoice.printed_number or number,
        "held_by": None,
        "draft_said": None,
        "repeated": printed_ref(repeated) if repeated is not None else None,
    }


# --- what every path that changes an invoice's status does ------------------------------------------------


def give_settings_number(invoice, new_status):
    """A DRAFT-… draft that no series will number (the module was switched off): a Settings number now.

    Call before the status changes, outside the flush: the Settings numbering locks and may flush,
    and it only sees numbers already in the database, so each invoice is numbered and flushed in turn.
    Returns True when the number changed.
    """
    from app.models import Invoice

    if new_status in _NOT_ISSUED or not is_draft_placeholder(invoice.invoice_number):
        return False
    if series_for_invoice(invoice) is not None:
        return False  # the hook gives the series number
    old = invoice.invoice_number
    invoice.invoice_number = Invoice.generate_invoice_number()
    if not invoice.printed_number_edited:
        invoice.printed_number = None
    _replace_number_copies(old, invoice.invoice_number)
    db.session.flush()
    return True


def flush_status_change(rollback=True):
    """Flush right after an invoice's status changed, so a numbering error surfaces here.

    Returns None, or the IssueNumberError after rolling back (``rollback=False``: the caller rolls
    back its savepoint). A unique-number clash with another series' number is NumberJustTaken.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    try:
        db.session.flush()
    except IssueNumberError as exc:
        if rollback:
            db.session.rollback()
        return exc
    except IntegrityError as exc:
        if "invoice_number" not in str(getattr(exc, "orig", "") or ""):
            raise
        if rollback:
            db.session.rollback()
        return NumberJustTaken()
    except OperationalError as exc:
        # SQLite: another writer holds the database while the invoice row is written ("database is locked")
        if "database is locked" not in str(getattr(exc, "orig", "") or ""):
            raise
        if rollback:
            db.session.rollback()
        return NumberJustTaken()
    return None


def issue_error_text(error):
    """The fixed text of a numbering error on a path with no dialog (a payment on a draft, Peppol, SDI)."""
    if isinstance(error, DuplicatePrintedNumber):
        try:
            from flask_babel import gettext

            return gettext(
                "No. %(printed)s is already used for this client (invoice %(invoice)s): "
                "issue the invoice with Mark as issued first",
                printed=error.printed,
                invoice=error.other_number,
            )
        except Exception:
            return (
                f"No. {error.printed} is already used for this client (invoice {error.other_number}): "
                "issue the invoice with Mark as issued first"
            )
    return error.text()


def number_before_sending(invoice):
    """True when a draft must be issued, its number committed, before its PDF or e-invoice is built."""
    if invoice is None or (invoice.status or "draft") != "draft":
        return False
    if is_draft_placeholder(invoice.invoice_number):
        return True
    return bool(invoice.billing_entity_id) and numbering_on() and entity_has_series(invoice.billing_entity_id)


def set_confirmed_repeat(invoice, printed):
    """The person agreed to issue with this printed number although the client already has it."""
    printed = (printed or "").strip()
    invoice._confirm_duplicate_printed = printed or None


def numbering_message(invoice):
    """What issuing told about the number, for a flash: "Issued as …", with a held or skipped number."""
    from flask_babel import gettext as _

    result = getattr(invoice, "_numbering_result", None)
    if not result:
        return None
    text = _("Issued as %(number)s (No. %(printed)s).", number=result["number"], printed=result["printed"])
    held = result.get("held_by")
    if held and held[2] == "draft":
        text += " " + _(
            "%(number)s is held by draft %(draft)s, which has that number set by hand.",
            number=held[1],
            draft=held[0],
        )
    elif held:
        text += " " + _("%(number)s is already used by invoice %(invoice)s.", number=held[1], invoice=held[0])
    elif result.get("draft_said"):
        text += " " + _(
            "The draft said %(expected)s, but %(expected)s was issued first from another draft.",
            expected=result["draft_said"],
        )
    return text


# --- the create and draft forms -----------------------------------------------------------------------


def numbers_by_series(entity):
    """True when the entity's invoices are numbered by its series (module on, the entity has series)."""
    return entity is not None and numbering_on() and entity_has_series(entity.id)


def set_now_number(form):
    """The number typed with "Set number now" on the create forms, or "" (the series assigns it)."""
    if not hasattr(form, "get") or not form.get("number_set_now"):
        return ""
    return (form.get("invoice_number", "") or "").strip()


def apply_printed_from_form(invoice, form):
    """Printed as, typed for this invoice only: kept (``printed_number_edited``) when changed from what was shown.

    Emptied: the series fills it when the invoice is issued. Left as shown: nothing changes.
    """
    if not hasattr(form, "get") or "printed_number" not in form:
        return
    typed = (form.get("printed_number", "") or "").strip()
    shown = (form.get("printed_number_preview", "") or "").strip()
    if typed == shown:
        return
    if typed:
        invoice.printed_number = typed[:50]
        invoice.printed_number_edited = True
    else:
        invoice.printed_number = None
        invoice.printed_number_edited = False


def check_set_number(number, exclude_invoice_id=None):
    """Raise NumberTaken if a number set by hand belongs to another invoice (offer: the series, not "-2")."""
    owner = number_owner(number, exclude_invoice_id)
    if owner is not None:
        raise NumberTaken(number, owner, "")


def number_new_draft(invoice, typed_number=""):
    """Number a new invoice from an entity that numbers by series. Call once From and Bill to are set.

    "Set number now": the typed number (the caller checked it is free); its {SEQ} is claimed when issued.
    Otherwise DRAFT-<id>: the series number comes when the invoice is issued. No series for this client
    and Bill to (the entity has no default series): the Settings number, as without the module.
    """
    from app.models import Invoice

    if typed_number:
        invoice.invoice_number = typed_number
        return "set"
    if assign_draft_placeholder(invoice):
        return "series"
    invoice.invoice_number = Invoice.generate_invoice_number()
    return "settings"


def gap_warning(invoice):
    """The number this draft holds would become a gap if the draft lost it: "ACME-25", else None.

    A draft set by hand to a series number that the series has already passed (later invoices skipped it).
    """
    if invoice is None or invoice.status != "draft" or is_draft_placeholder(invoice.invoice_number):
        return None
    series = series_for_invoice(invoice)
    if series is None:
        return None
    issue_date = invoice.issue_date or default_issue_date()
    seq = series_seq_of(series, invoice.invoice_number, issue_date, invoice.period_end)
    if seq is None or seq > last_seq(series, series.counter_year(issue_date)):
        return None
    return invoice.invoice_number


def draft_number_context(invoice):
    """What a draft's header and step 1 say about its number (module on, entity numbers by series)."""
    if invoice is None or invoice.status != "draft" or not numbers_by_series(invoice.billing_entity):
        return None
    series = series_for_invoice(invoice)
    placeholder = is_draft_placeholder(invoice.invoice_number)
    preview = (
        series_preview(series, invoice.issue_date, invoice.period_end, exclude_invoice_id=invoice.id)
        if series is not None
        else None
    )
    if not placeholder and series is not None:
        seq = series_seq_of(series, invoice.invoice_number, invoice.issue_date, invoice.period_end)
        printed = invoice.printed_number or format_printed_number(
            series, invoice.invoice_number, seq, invoice.issue_date, invoice.period_end
        )
    else:
        printed = (
            invoice.printed_number
            if invoice.printed_number_edited and invoice.printed_number
            else (preview["printed"] if preview else "")
        )
    repeat = find_printed_duplicate(invoice, printed, issued_only=False, series=series) if printed else None
    return {
        "placeholder": placeholder,
        "series": preview,
        "series_label": series_label(series) if series is not None else "",
        "printed": printed,
        "printed_edited": bool(invoice.printed_number_edited),
        "repeat": printed_ref(repeat) if repeat is not None else None,
        "gap": gap_warning(invoice),
    }


# --- the Numbering block of the entity page ---------------------------------------------------------------


def _marker_format(number_format):
    """The format with every token replaced by one marker: two series that compare equal make the same numbers."""
    return _ANY_TOKEN.sub("\x00", (number_format or "").strip())


def _sample_number(number_format):
    values = {"YYYY": "2026", "YY": "26", "MM": "11", "DD": "30", "SEQ": "1"}
    return _ANY_TOKEN.sub(lambda m: values.get(m.group(1), ""), number_format or "")


def _format_regex(number_format):
    parts, last = [], 0
    for match in _ANY_TOKEN.finditer(number_format or ""):
        parts.append(re.escape(number_format[last : match.start()]))
        parts.append(r"\d+")
        last = match.end()
    parts.append(re.escape((number_format or "")[last:]))
    return re.compile("".join(parts))


def overlapping_example(format_a, format_b):
    """A number both formats can make (``AB261`` for ``AB{YY}{SEQ}`` and ``AB{SEQ}``), else None."""
    for one, other in ((format_a, format_b), (format_b, format_a)):
        sample = _sample_number(one)
        if sample and _format_regex(other).fullmatch(sample):
            return sample
    return None


def _taken_run(series, start, year, issue_date):
    """(first, last) numbers already taken from {SEQ} ``start`` on, or None when ``start`` is free."""
    seq = start
    first = last = None
    while True:
        number = format_series_number(series, seq, issue_date)
        if number_owner(number) is None:
            break
        first = first or number
        last = number
        seq += 1
    return (first, last, seq) if first else None


def max_given_seq(series, year, formats):
    """The highest {SEQ} the series gave on an issued invoice (of ``year`` for a yearly counter), or 0.

    ``formats``: series objects whose format may have made the numbers (the one typed and the one saved).
    """
    from app.models import Invoice

    if series.id is None:
        return 0
    query = db.session.query(Invoice.invoice_number, Invoice.issue_date, Invoice.period_end).filter(
        Invoice.number_series_id == series.id, Invoice.status != "draft"
    )
    best = 0
    for number, issue_date, period in query.all():
        issue_date = issue_date or default_issue_date()
        if year and issue_date.year != year:
            continue
        for probe in formats:
            seq = series_seq_of(probe, number, issue_date, period)
            if seq is not None:
                best = max(best, seq)
                break
    return best


def has_issued_invoices(series):
    from app.models import Invoice

    return (
        series.id is not None
        and db.session.query(Invoice.id)
        .filter(Invoice.number_series_id == series.id, Invoice.status != "draft")
        .first()
        is not None
    )


def next_seq_years(series, today=None):
    """The years the series form asks Next {SEQ} for: this year and the next; in January also the last one."""
    today = today or date.today()
    if not series.reset_yearly:
        return [0]
    years = [today.year, today.year + 1]
    if today.month == 1:
        years.insert(0, today.year - 1)
    return years


def check_series(series, number_format, printed_format, reset_yearly, next_seqs, today=None):
    """Errors and warnings for a series as typed. ``series`` holds the typed client, Bill to and status.

    ``next_seqs``: {year (0 without a restart): Next {SEQ} typed}. Returns ``(errors, warnings)``,
    each a list of ``{"field", "message", "use"}`` (``use``: a Next {SEQ} to offer).
    """
    from flask_babel import gettext as _

    from app.models import BillingEntityNumberSeries as Series

    errors, warnings = [], []

    def error(field, message, use=None):
        errors.append({"field": field, "message": message, "use": use})

    number_format = (number_format or "").strip()
    printed_format = (printed_format or "").strip() or "{NUMBER}"
    if not number_format:
        error("number_format", _("Number format is required"))
        return errors, warnings
    unknown = sorted({t for t in _ANY_TOKEN.findall(number_format) if t not in SERIES_TOKENS})
    if unknown:
        error("number_format", _("Unknown token(s): %(tokens)s", tokens=", ".join("{%s}" % t for t in unknown)))
    unknown = sorted({t for t in _ANY_TOKEN.findall(printed_format) if t not in PRINTED_TOKENS})
    if unknown:
        error("printed_format", _("Unknown token(s): %(tokens)s", tokens=", ".join("{%s}" % t for t in unknown)))
    if series.client_id is not None and "{SEQ}" not in number_format:
        error("number_format", _("A series for a client needs {SEQ}"))
    if reset_yearly:
        if series.client_id is not None:
            error("reset_yearly", _("Restart every year is only for the default series of a legal entity."))
        if "{YYYY}" not in number_format and "{YY}" not in number_format:
            error("reset_yearly", _("Restart every year needs {YYYY} or {YY} in the number format."))
        if "{MM}" in number_format:
            error(
                "number_format",
                _("With Restart every year, {YYYY} is the year of the issue date, and {MM} is not allowed."),
            )
    if series.id is not None and bool(series.reset_yearly) != bool(reset_yearly) and has_issued_invoices(series):
        this_year = (today or date.today()).year
        if not next_seqs.get(this_year if reset_yearly else 0):
            error(
                "reset_yearly",
                _("To switch Restart every year on a series with issued invoices, set Next {SEQ} for this year."),
            )

    # Two active series that make the same numbers
    if series.status == "active":
        marker = _marker_format(number_format)
        others = Series.query.filter(Series.status == "active")
        if series.id is not None:
            others = others.filter(Series.id != series.id)
        for other in others.all():
            other_name = other.billing_entity.short_name if other.billing_entity else ""
            if _marker_format(other.number_format) == marker:
                if other.billing_entity_id == series.billing_entity_id:
                    error(
                        "number_format",
                        _(
                            "%(series)s already uses %(format)s. Two series can't make the same number: add "
                            "something that tells them apart, e.g. %(example)s.",
                            series=series_label(other),
                            format=other.number_format,
                            example=number_format.replace("{SEQ}", "A-{SEQ}", 1),
                        ),
                    )
                else:
                    error(
                        "number_format",
                        _(
                            "%(entity)s already uses %(format)s. Numbers are unique across all invoices, so two "
                            "legal entities can't share a format: add a prefix, e.g. %(example)s.",
                            entity=other_name,
                            format=other.number_format,
                            example="X-" + number_format,
                        ),
                    )
                continue
            example = overlapping_example(number_format, other.number_format)
            if example:
                warnings.append(
                    {
                        "field": "number_format",
                        "message": _(
                            "%(format)s can make the same numbers as %(series)s %(other)s (%(example)s is both). "
                            "When that happens, the next free number is taken. You can still save.",
                            format=number_format,
                            series=series_label(other),
                            other=other.number_format,
                            example=example,
                        ),
                        "use": None,
                    }
                )

    # Next {SEQ}: a positive number; numbers from there on that other invoices hold are offered past
    probe = Series(number_format=number_format, reset_yearly=bool(reset_yearly))
    for year, value in next_seqs.items():
        if value is None:
            continue
        if value < 1:
            error(f"next_seq_{year}", _("Next {SEQ} must be 1 or more"))
            continue
        if "{SEQ}" not in number_format or errors:
            continue
        issue_day = date(year, 6, 30) if year else (today or date.today())
        # Next goes on after the last number given: it can't go back into a gap below it
        given = max_given_seq(series, year, [probe] + ([series] if series.number_format else []))
        if value <= given:
            use = given + 1
            run = _taken_run(probe, use, year, issue_day)
            error(
                f"next_seq_{year}",
                _("Next {SEQ} must be above %(seq)s, the last number given in this series.", seq=given),
                use=run[2] if run else use,
            )
            continue
        run = _taken_run(probe, value, year, issue_day)
        if run:
            first, last, free = run
            message = (
                _("%(number)s is already taken.", number=first)
                if first == last
                else _("%(first)s to %(last)s are already taken.", first=first, last=last)
            )
            error(f"next_seq_{year}", message, use=free)
    return errors, warnings


def save_next_seqs(series, next_seqs):
    """Store Next {SEQ} as typed: the counter of each year becomes Next minus one."""
    for year, value in next_seqs.items():
        if value is not None and value >= 1:
            set_last_seq(series, year, value - 1)


def client_entities_on():
    """Whether the Client legal entities module is on (Settings only): series may be for a Bill to."""
    return _module_on_by_settings(CLIENT_ENTITIES_MODULE_ID, _numbering_settings())


def client_entity_choices(client_ids):
    """{client_id: [(id, legal name)]} of the clients' active legal entities, when that module is on."""
    from app.models import Invoice

    if not client_ids or not client_entities_on():
        return {}
    target = Invoice.client_entity.property.mapper.class_
    rows = (
        target.query.filter(target.client_id.in_(client_ids), target.status == "active")
        .order_by(target.legal_name)
        .all()
    )
    out = {}
    for row in rows:
        out.setdefault(row.client_id, []).append((row.id, row.legal_name))
    return out


def client_entity_owner(client_entity_id):
    """The client id a legal entity of a client belongs to, or None when there is no such entity."""
    from app.models import Invoice

    target = Invoice.client_entity.property.mapper.class_
    entity = db.session.get(target, client_entity_id)
    return entity.client_id if entity else None


def numbering_context(entity, today=None):
    """What the Numbering block of the entity page shows: the default series and the series by client."""
    from app.models import BillingEntityNumberSeries as Series

    today = today or date.today()
    all_series = entity.number_series.order_by(Series.status.asc(), Series.id.asc()).all() if entity.id else []
    default = next((s for s in all_series if s.client_id is None and s.is_active), None)
    default_view = None
    if default is not None:
        default_view = {
            "series": default,
            "next": series_preview(default, today),
            "issued": has_issued_invoices(default),
        }
    # Switching Restart every year on or off carries the counter over to this year (or back)
    if default is None:
        plain_next = (entity.number_counter or 0) + 1
    elif default.reset_yearly:
        plain_next = last_seq(default, today.year) + 1
    else:
        plain_next = last_seq(default, 0) + 1
    reset_years = []
    for year in next_seq_years(Series(reset_yearly=True), today):
        if default is not None and default.reset_yearly:
            reset_years.append((year, last_seq(default, year) + 1))
        else:
            reset_years.append((year, plain_next if year == today.year else 1))
    from flask_babel import gettext as _

    bill_to_on = client_entities_on()
    rows = []
    for series in all_series:
        if series.client_id is None:
            continue
        client = series.client.name if series.client else ""
        bill_to = client_entity_name(series.client_entity_id) if series.client_entity_id else ""
        removed = bool(series.client_entity_id) and not bill_to
        # Which invoices take this series: the reader should not have to know the lookup order
        if not series.is_active:
            used_for = _("Archived: not picked; its numbers stay on their invoices")
        elif removed:
            used_for = _("Not picked: the legal entity was removed")
        elif series.client_entity_id and not bill_to_on:
            used_for = _(
                "None for now: made for invoices billed to %(bill_to)s, it waits while client legal entities are off",
                bill_to=bill_to,
            )
        elif series.client_entity_id:
            used_for = _("Invoices to %(client)s billed to %(bill_to)s", client=client, bill_to=bill_to)
        elif (
            any(
                o.client_id == series.client_id and o.client_entity_id and o.is_active and o.id != series.id
                for o in all_series
            )
            and bill_to_on
        ):
            used_for = _("Other invoices to %(client)s (any other Bill to, or none)", client=client)
        else:
            used_for = _("All invoices to %(client)s", client=client)
        last = last_series_invoice(series)
        rows.append(
            {
                "series": series,
                "client": client,
                "bill_to": bill_to,
                "removed": removed,
                "used_for": used_for,
                # Not picked for any invoice now: shown muted, after the series in use
                "idle": not series.is_active or removed or (bool(series.client_entity_id) and not bill_to_on),
                "next": last_seq(series, 0) + 1,
                "last": printed_ref(last) if last else None,
            }
        )
    # In use first; per client the series for one legal entity before "Any legal entity"
    rows.sort(key=lambda r: (r["idle"], r["client"].lower(), not r["series"].client_entity_id, r["bill_to"]))
    return {
        "default": default_view,
        "legacy_format": entity.number_format or "",
        "reset_years": reset_years,
        "plain_next": plain_next,
        "rows": rows,
        "client_entities_on": bill_to_on,
        "today": today,
    }


def number_preview_payload(
    entity, client_id, client_entity_id=None, issue_date=None, period=None, invoice=None, typed_number="", printed=""
):
    """What the Number row of step 1 shows for this From, client and Bill to (``{"series": False}`` without one).

    ``typed_number``: a number being set by hand (taken? fits the series? skips numbers?); ``printed``: the
    Printed as being typed (already used for the client?).
    """
    from flask_babel import gettext as _

    if entity is None or not numbers_by_series(entity):
        return {"series": False}
    settings = _numbering_settings()
    bill_to = client_entity_id if _module_on_by_settings(CLIENT_ENTITIES_MODULE_ID, settings) else None
    series = resolve_series(entity.id, client_id, bill_to)
    if series is None:
        return {"series": False, "settings_number": True}
    exclude = invoice.id if invoice is not None and invoice.id else None
    preview = series_preview(series, issue_date, period, exclude_invoice_id=exclude)
    out = {"series": True, "label": series_label(series), "is_default": series.client_id is None, **preview}
    out["entity_url_id"] = entity.id
    typed_number = (typed_number or "").strip()
    if typed_number:
        owner = number_owner(typed_number, exclude)
        out["typed"] = {"number": typed_number, "taken": printed_ref(owner) if owner else None}
        seq = series_seq_of(series, typed_number, issue_date or default_issue_date(), period)
        out["typed"]["fits"] = seq is not None
        out["typed"]["printed"] = format_printed_number(
            series, typed_number, seq, issue_date or default_issue_date(), period
        )
        if seq is not None and seq > preview["seq"]:
            out["typed"]["skips"] = (
                _("This skips %(first)s to %(last)s.", first=preview["seq"], last=seq - 1)
                if seq - 1 > preview["seq"]
                else _("This skips %(first)s.", first=preview["seq"])
            )
    probe_printed = (printed or "").strip() or (out.get("typed") or {}).get("printed") or preview["printed"]
    if probe_printed and client_id:
        probe_state = {
            "id": exclude,
            "billing_entity_id": entity.id,
            "client_id": client_id,
            "client_entity_id": bill_to,
            "invoice_number": typed_number or preview["number"],
            "printed_number": probe_printed,
            "issue_date": issue_date or default_issue_date(),
        }
        repeat = _repeat_for(probe_state, series)
        out["repeat"] = printed_ref(repeat) if repeat is not None else None
        out["repeat_printed"] = probe_printed
    return out


class _InvoiceProbe:
    """The fields ``find_printed_duplicate`` reads, for a draft not saved yet."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def _repeat_for(fields, series):
    probe = _InvoiceProbe(**fields)
    return find_printed_duplicate(probe, fields["printed_number"], issued_only=False, series=series)


def assign_from_series_preview(invoices):
    """What "Assign from the series when issued" would do to these drafts, in order.

    ``{"rows": [{"id", "from", "to"}], "notes": [...], "skipped": n}``: only drafts whose From numbers by
    series; numbers of one series follow each other as they would if issued in this order.
    """
    from flask_babel import gettext as _

    rows, notes, skipped = [], [], 0
    taken = {}
    for invoice in invoices:
        series = series_for_invoice(invoice) if invoice.status == "draft" else None
        if series is None:
            skipped += 1
            continue
        issue_date = invoice.issue_date or default_issue_date()
        year = series.counter_year(issue_date)
        key = (series.id, year)
        seq = taken.get(key, last_seq(series, year)) + 1
        number = format_series_number(series, seq, issue_date, invoice.period_end)
        while number_owner(number, invoice.id) is not None:
            seq += 1
            number = format_series_number(series, seq, issue_date, invoice.period_end)
        taken[key] = seq
        rows.append({"id": invoice.id, "from": invoice.invoice_number, "to": number})
        if series.reset_yearly and seq == 1 and year != default_issue_date().year:
            notes.append(
                _(
                    "%(draft)s is for %(year)s: the %(series)s series has no %(year)s numbers yet and starts at 1.",
                    draft=invoice.id,
                    year=year,
                    series=series_label(series),
                )
            )
    return {"rows": rows, "notes": notes, "skipped": skipped}
