"""Invoice number series of billing entities, and the printed invoice number.

- ``billing_entity_number_series``: a number format, a printed format and a counter per billing
  entity (default series), per client, or per legal entity of a client.
- ``billing_entity_number_counters``: the last {SEQ} of a series, per year for a series that
  restarts every year (year 0 otherwise).
- ``invoices.number_series_id``, ``invoices.printed_number``, ``invoices.printed_number_edited``.
- Every billing entity with a number format gets it as its default series, the counter carried
  over; its issued invoices whose number fits the format are linked to the series. The entity's
  ``number_format`` and ``number_counter`` stay for the downgrade and are no longer read.
- Invoice PDF templates (HTML templates and all their versions, JSON/visual templates, the Settings
  template) print ``invoice.display_number``: the printed number, else the invoice number. Without
  printed numbers this prints exactly what ``invoice.invoice_number`` printed.

Revision ID: 224_add_invoice_number_series
Revises: 223_refresh_draft_invoice_client_snapshots
"""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "224_add_invoice_number_series"
down_revision = "223_refresh_draft_invoice_client_snapshots"
branch_labels = None
depends_on = None

OLD_VARIABLE = "invoice.invoice_number"
NEW_VARIABLE = "invoice.display_number"
_TOKEN = re.compile(r"\{(YYYY|YY|MM|DD|SEQ)\}")

# (table, text columns) of the invoice templates whose text names the invoice number
TEMPLATE_COLUMNS = (
    ("invoice_html_templates", ("html",)),
    ("invoice_html_template_versions", ("html",)),
    ("invoice_pdf_templates", ("template_html", "template_json", "design_json")),
    ("settings", ("invoice_pdf_template_html",)),
)


def _has_table(inspector, name):
    return name in inspector.get_table_names()


def _columns(inspector, table_name):
    try:
        return {c["name"] for c in inspector.get_columns(table_name)}
    except Exception:
        return set()


def _active_where(extra):
    return sa.text(f"{extra} AND status = 'active'")


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _has_table(inspector, "billing_entity_number_series"):
        op.create_table(
            "billing_entity_number_series",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "billing_entity_id",
                sa.Integer(),
                sa.ForeignKey("billing_entities.id", name="fk_number_series_billing_entity_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "client_id",
                sa.Integer(),
                sa.ForeignKey("clients.id", name="fk_number_series_client_id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("client_entity_id", sa.Integer(), nullable=True),
            sa.Column("number_format", sa.String(length=120), nullable=False),
            sa.Column("printed_format", sa.String(length=120), nullable=False, server_default="{NUMBER}"),
            sa.Column("reset_yearly", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "client_entity_id IS NULL OR client_id IS NOT NULL",
                name="ck_number_series_client_entity_needs_client",
            ),
        )
        op.create_index(
            "ix_billing_entity_number_series_billing_entity_id", "billing_entity_number_series", ["billing_entity_id"]
        )
        op.create_index("ix_billing_entity_number_series_client_id", "billing_entity_number_series", ["client_id"])
        op.create_index("ix_billing_entity_number_series_status", "billing_entity_number_series", ["status"])
        where = "client_id IS NULL"
        op.create_index(
            "uq_number_series_default",
            "billing_entity_number_series",
            ["billing_entity_id"],
            unique=True,
            postgresql_where=_active_where(where),
            sqlite_where=_active_where(where),
        )
        where = "client_entity_id IS NULL AND client_id IS NOT NULL"
        op.create_index(
            "uq_number_series_client",
            "billing_entity_number_series",
            ["billing_entity_id", "client_id"],
            unique=True,
            postgresql_where=_active_where(where),
            sqlite_where=_active_where(where),
        )
        where = "client_entity_id IS NOT NULL"
        op.create_index(
            "uq_number_series_client_entity",
            "billing_entity_number_series",
            ["billing_entity_id", "client_id", "client_entity_id"],
            unique=True,
            postgresql_where=_active_where(where),
            sqlite_where=_active_where(where),
        )

    if not _has_table(inspector, "billing_entity_number_counters"):
        op.create_table(
            "billing_entity_number_counters",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "series_id",
                sa.Integer(),
                sa.ForeignKey("billing_entity_number_series.id", name="fk_number_counters_series_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("year", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("series_id", "year", name="uq_number_counters_series_year"),
        )
        op.create_index(
            "ix_billing_entity_number_counters_series_id", "billing_entity_number_counters", ["series_id"]
        )

    inspector = inspect(bind)
    existing = _columns(inspector, "invoices")
    with op.batch_alter_table("invoices") as batch:
        if "number_series_id" not in existing:
            batch.add_column(sa.Column("number_series_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_invoices_number_series_id",
                "billing_entity_number_series",
                ["number_series_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index("ix_invoices_number_series_id", ["number_series_id"])
        if "printed_number" not in existing:
            batch.add_column(sa.Column("printed_number", sa.String(length=50), nullable=True))
        if "printed_number_edited" not in existing:
            batch.add_column(
                sa.Column("printed_number_edited", sa.Boolean(), nullable=False, server_default=sa.false())
            )

    _default_series_from_entities(bind)
    _replace_in_templates(bind, OLD_VARIABLE, NEW_VARIABLE)


def _number_regex(number_format):
    """A regex for the numbers a format makes (tokens: digits). Formats are not checked here."""
    parts = []
    last = 0
    for match in _TOKEN.finditer(number_format):
        parts.append(re.escape(number_format[last : match.start()]))
        parts.append(r"\d+")
        last = match.end()
    parts.append(re.escape(number_format[last:]))
    return re.compile("".join(parts))


def _default_series_from_entities(bind):
    """Each entity's number format becomes its default series; its matching issued invoices join it."""
    entities = bind.execute(
        sa.text(
            "SELECT id, number_format, number_counter, status FROM billing_entities "
            "WHERE number_format IS NOT NULL AND number_format <> ''"
        )
    ).fetchall()
    for entity_id, number_format, counter, status in entities:
        number_format = number_format.strip()
        if not number_format:
            continue
        already = bind.execute(
            sa.text("SELECT id FROM billing_entity_number_series WHERE billing_entity_id = :e AND client_id IS NULL"),
            {"e": entity_id},
        ).first()
        if already:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO billing_entity_number_series "
                "(billing_entity_id, client_id, client_entity_id, number_format, printed_format, reset_yearly, "
                "status, created_at, updated_at) "
                "VALUES (:e, NULL, NULL, :f, '{NUMBER}', :r, :s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"e": entity_id, "f": number_format, "r": False, "s": status or "active"},
        )
        series_id = bind.execute(
            sa.text(
                "SELECT id FROM billing_entity_number_series WHERE billing_entity_id = :e AND client_id IS NULL "
                "ORDER BY id DESC"
            ),
            {"e": entity_id},
        ).scalar()
        bind.execute(
            sa.text("INSERT INTO billing_entity_number_counters (series_id, year, last_seq) VALUES (:s, 0, :n)"),
            {"s": series_id, "n": int(counter or 0)},
        )
        pattern = _number_regex(number_format)
        rows = bind.execute(
            sa.text("SELECT id, invoice_number FROM invoices WHERE billing_entity_id = :e AND status <> 'draft'"),
            {"e": entity_id},
        ).fetchall()
        ids = [row[0] for row in rows if row[1] and pattern.fullmatch(row[1])]
        for invoice_id in ids:
            bind.execute(
                sa.text("UPDATE invoices SET number_series_id = :s WHERE id = :i"), {"s": series_id, "i": invoice_id}
            )


def _replace_in_templates(bind, old, new):
    inspector = inspect(bind)
    for table, columns in TEMPLATE_COLUMNS:
        if not _has_table(inspector, table):
            continue
        present = _columns(inspector, table)
        for column in columns:
            if column not in present:
                continue
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = REPLACE({column}, :old, :new) WHERE {column} LIKE :like"),
                {"old": old, "new": new, "like": f"%{old}%"},
            )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    _replace_in_templates(bind, NEW_VARIABLE, OLD_VARIABLE)
    if _has_table(inspector, "billing_entity_number_series"):
        _restore_entity_formats(bind)

    existing = _columns(inspector, "invoices")
    with op.batch_alter_table("invoices") as batch:
        if "printed_number_edited" in existing:
            batch.drop_column("printed_number_edited")
        if "printed_number" in existing:
            batch.drop_column("printed_number")
        if "number_series_id" in existing:
            batch.drop_index("ix_invoices_number_series_id")
            batch.drop_constraint("fk_invoices_number_series_id", type_="foreignkey")
            batch.drop_column("number_series_id")

    inspector = inspect(bind)
    if _has_table(inspector, "billing_entity_number_counters"):
        op.drop_table("billing_entity_number_counters")
    if _has_table(inspector, "billing_entity_number_series"):
        op.drop_table("billing_entity_number_series")


def _restore_entity_formats(bind):
    """The entity's format and counter again from its default series (the counter of its latest year)."""
    rows = bind.execute(
        sa.text(
            "SELECT s.billing_entity_id, s.number_format, s.id FROM billing_entity_number_series s "
            "WHERE s.client_id IS NULL ORDER BY CASE WHEN s.status = 'active' THEN 0 ELSE 1 END, s.id DESC"
        )
    ).fetchall()
    seen = set()
    for entity_id, number_format, series_id in rows:
        if entity_id in seen:
            continue
        seen.add(entity_id)
        last = bind.execute(
            sa.text(
                "SELECT last_seq FROM billing_entity_number_counters WHERE series_id = :s ORDER BY year DESC LIMIT 1"
            ),
            {"s": series_id},
        ).scalar()
        bind.execute(
            sa.text("UPDATE billing_entities SET number_format = :f, number_counter = :n WHERE id = :e"),
            {"f": number_format, "n": int(last or 0), "e": entity_id},
        )
