"""Invoice number series of a billing entity (Legal entities module).

A series is a number format, a printed format and a counter. Every billing entity can have a default
series (all clients) and series for one client, or for one legal entity of a client ("Bill to").
An invoice from the entity takes the series of its Bill to, else the client's series ("Any legal
entity"), else the default series. The number is fixed when the invoice leaves draft
(``app.utils.billing_entities.allocate_for_invoice``).

``client_entity_id`` has no foreign key: the Client legal entities module is optional, and this
module does not know its tables. The form checks that the legal entity belongs to the client.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, event, text
from sqlalchemy.orm import Session

from app import db

SERIES_STATUSES = ("active", "archived")
DEFAULT_PRINTED_FORMAT = "{NUMBER}"

_ACTIVE = "status = 'active'"


class BillingEntityNumberSeries(db.Model):
    """One numbering series of a billing entity."""

    __tablename__ = "billing_entity_number_series"
    __table_args__ = (
        CheckConstraint(
            "client_entity_id IS NULL OR client_id IS NOT NULL", name="ck_number_series_client_entity_needs_client"
        ),
        # One active series per (entity), (entity, client) and (entity, client, client legal entity)
        Index(
            "uq_number_series_default",
            "billing_entity_id",
            unique=True,
            postgresql_where=text("client_id IS NULL AND " + _ACTIVE),
            sqlite_where=text("client_id IS NULL AND " + _ACTIVE),
        ),
        Index(
            "uq_number_series_client",
            "billing_entity_id",
            "client_id",
            unique=True,
            postgresql_where=text("client_entity_id IS NULL AND client_id IS NOT NULL AND " + _ACTIVE),
            sqlite_where=text("client_entity_id IS NULL AND client_id IS NOT NULL AND " + _ACTIVE),
        ),
        Index(
            "uq_number_series_client_entity",
            "billing_entity_id",
            "client_id",
            "client_entity_id",
            unique=True,
            postgresql_where=text("client_entity_id IS NOT NULL AND " + _ACTIVE),
            sqlite_where=text("client_entity_id IS NOT NULL AND " + _ACTIVE),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    billing_entity_id = db.Column(
        db.Integer, db.ForeignKey("billing_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Empty: the default series of the entity
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=True, index=True)
    # Empty: "Any legal entity" of the client. No foreign key (see the module docstring)
    client_entity_id = db.Column(db.Integer, nullable=True)
    number_format = db.Column(db.String(120), nullable=False)  # e.g. "ACME-{SEQ}", unique across invoices
    printed_format = db.Column(db.String(120), nullable=False, default=DEFAULT_PRINTED_FORMAT)  # what clients see
    # The counter starts again at 1 every year (default series only); the year is the issue date's
    reset_yearly = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    billing_entity = db.relationship(
        "BillingEntity",
        backref=db.backref("number_series", lazy="dynamic", cascade="all, delete-orphan", passive_deletes=True),
    )
    client = db.relationship("Client")
    counters = db.relationship(
        "BillingEntityNumberCounter",
        backref="series",
        lazy="dynamic",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self):
        return f"<BillingEntityNumberSeries {self.number_format}>"

    @property
    def is_active(self):
        return self.status == "active"

    @property
    def is_default(self):
        return self.client_id is None

    def counter_year(self, issue_date):
        """The counter row a number issued on ``issue_date`` uses: its year, or 0 without a yearly restart."""
        if self.reset_yearly and issue_date is not None:
            return issue_date.year
        return 0

    def last_seq(self, year=0):
        """The last {SEQ} given in ``year`` (0 for a series that does not restart), 0 when none."""
        row = self.counters.filter_by(year=year).first()
        return row.last_seq if row else 0


class BillingEntityNumberCounter(db.Model):
    """The last {SEQ} of a series: one row per year for a yearly series, one row (year 0) otherwise."""

    __tablename__ = "billing_entity_number_counters"
    __table_args__ = (UniqueConstraint("series_id", "year", name="uq_number_counters_series_year"),)

    id = db.Column(db.Integer, primary_key=True)
    series_id = db.Column(
        db.Integer, db.ForeignKey("billing_entity_number_series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    year = db.Column(db.Integer, nullable=False, default=0)
    last_seq = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<BillingEntityNumberCounter {self.series_id}/{self.year}: {self.last_seq}>"


def _keep_old_status(target, value, oldvalue, initiator):
    """Registered with active_history: the old status is loaded, so the hook sees a draft being issued."""
    return value


@event.listens_for(Session, "before_flush")
def _assign_numbers_when_issued(session, flush_context, instances):
    """An invoice leaving draft gets its series number here, on every path that changes its status."""
    from app.utils.billing_entities import numbering_before_flush

    numbering_before_flush(session)


@event.listens_for(Session, "after_flush_postexec")
def _name_new_drafts(session, flush_context):
    """A new draft is DRAFT-<id> once its id is known (DRAFT-<random> before the first flush)."""
    from app.utils.billing_entities import numbering_after_flush

    numbering_after_flush(session)


@event.listens_for(Session, "after_commit")
def _issued_drafts_committed(session):
    """Drafts that got their number in this transaction: their client is notified once it is committed."""
    from app.utils.billing_entities import numbering_after_commit

    numbering_after_commit(session)


@event.listens_for(Session, "after_rollback")
def _issued_drafts_rolled_back(session):
    from app.utils.billing_entities import numbering_after_rollback

    numbering_after_rollback(session)


def _listen_to_invoice_status():
    from app.models.invoice import Invoice

    if not event.contains(Invoice.status, "set", _keep_old_status):
        event.listen(Invoice.status, "set", _keep_old_status, active_history=True, retval=True)


_listen_to_invoice_status()
