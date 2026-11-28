"""Migration 224: number series, counters, printed numbers, and templates that print display_number.

Runs on SQLite by default; set MIGRATION_TEST_DATABASE_URL to run it on PostgreSQL.
"""

import importlib.util
import os
import tempfile
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [pytest.mark.unit, pytest.mark.database]

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations",
    "versions",
    "224_add_invoice_number_series.py",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_224", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _drop_new_tables(engine):
    """What the upgrade adds goes first: its foreign keys point at the tables below."""
    inspector = sa.inspect(engine)
    if "number_series_id" in _columns(engine, "invoices") if inspector.has_table("invoices") else False:
        _run(engine, _load_migration().downgrade)
    with engine.begin() as conn:
        for table in ("billing_entity_number_counters", "billing_entity_number_series"):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))


@pytest.fixture
def engine():
    url = os.environ.get("MIGRATION_TEST_DATABASE_URL")
    path = None
    if not url:
        path = os.path.join(tempfile.gettempdir(), f"migration_224_{uuid.uuid4().hex}.sqlite")
        url = f"sqlite:///{path}"
    engine = sa.create_engine(url)
    meta = sa.MetaData()
    sa.Table("clients", meta, sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String(200)))
    sa.Table(
        "billing_entities",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("short_name", sa.String(200)),
        sa.Column("number_format", sa.String(120)),
        sa.Column("number_counter", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    sa.Table(
        "invoices",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invoice_number", sa.String(50), unique=True),
        sa.Column("status", sa.String(20)),
        sa.Column("billing_entity_id", sa.Integer, sa.ForeignKey("billing_entities.id")),
    )
    sa.Table(
        "invoice_templates",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("html", sa.Text),
    )
    sa.Table(
        "invoice_html_templates",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("html", sa.Text, nullable=False),
    )
    sa.Table(
        "invoice_html_template_versions",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("html", sa.Text, nullable=False),
    )
    sa.Table(
        "invoice_pdf_templates",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("template_html", sa.Text),
        sa.Column("template_json", sa.Text),
        sa.Column("design_json", sa.Text),
    )
    sa.Table(
        "settings",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invoice_pdf_template_html", sa.Text),
        sa.Column("invoice_pdf_design_json", sa.Text),
    )
    _drop_new_tables(engine)
    meta.drop_all(engine)
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO billing_entities (id, short_name, number_format, number_counter, status) VALUES "
                "(1, 'Acme', 'ACME-{YYYY}-{SEQ}', 41, 'active'), "
                "(2, 'Old', 'OLD-{SEQ}', 3, 'archived'), "
                "(3, 'Plain', NULL, 0, 'active')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO invoices (id, invoice_number, status, billing_entity_id) VALUES "
                "(1, 'ACME-2026-41', 'sent', 1), (2, 'HAND-7', 'sent', 1), (3, 'ACME-2026-42', 'draft', 1)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO invoice_html_templates (id, html) VALUES (1, '<h1>{{ invoice.invoice_number }}</h1>')")
        )
        conn.execute(
            sa.text(
                "INSERT INTO invoice_html_template_versions (id, html) VALUES "
                "(1, 'No. {{ invoice.invoice_number }}'), (2, 'no number here')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO invoice_pdf_templates (id, template_html, template_json, design_json) VALUES "
                "(1, '{{ invoice.invoice_number }}', '{\"text\": \"{{ invoice.invoice_number }}\"}', NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO settings (id, invoice_pdf_template_html, invoice_pdf_design_json) VALUES "
                "(1, '#{{ invoice.invoice_number }}', '{\"text\": \"{{ invoice.invoice_number }}\"}')"
            )
        )
        conn.execute(
            sa.text("INSERT INTO invoice_templates (id, html) VALUES (1, 'Invoice {{ invoice.invoice_number }}')")
        )
    yield engine
    _drop_new_tables(engine)
    meta.drop_all(engine)
    engine.dispose()
    if path and os.path.exists(path):
        os.remove(path)


def test_upgrade_makes_default_series_and_links_issued_invoices(engine):
    migration = _load_migration()
    _run(engine, migration.upgrade)

    assert {"number_series_id", "printed_number", "printed_number_edited"} <= _columns(engine, "invoices")
    with engine.connect() as conn:
        series = conn.execute(
            sa.text(
                "SELECT billing_entity_id, client_id, number_format, printed_format, reset_yearly, status "
                "FROM billing_entity_number_series ORDER BY billing_entity_id"
            )
        ).fetchall()
        assert [tuple(r) for r in series] == [
            (1, None, "ACME-{YYYY}-{SEQ}", "{NUMBER}", False, "active"),
            (2, None, "OLD-{SEQ}", "{NUMBER}", False, "archived"),
        ]
        counters = conn.execute(
            sa.text(
                "SELECT s.billing_entity_id, c.year, c.last_seq FROM billing_entity_number_counters c "
                "JOIN billing_entity_number_series s ON s.id = c.series_id ORDER BY s.billing_entity_id"
            )
        ).fetchall()
        assert [tuple(r) for r in counters] == [(1, 0, 41), (2, 0, 3)]
        linked = conn.execute(
            sa.text("SELECT id, number_series_id IS NOT NULL, printed_number FROM invoices ORDER BY id")
        ).fetchall()
        # an issued invoice whose number fits the format joins the series; a hand number and a draft don't
        assert [tuple(r) for r in linked] == [(1, True, None), (2, False, None), (3, False, None)]
        edited = conn.execute(sa.text("SELECT printed_number_edited FROM invoices WHERE id = 1")).scalar()
        assert not edited

        assert conn.execute(sa.text("SELECT html FROM invoice_html_templates")).scalar() == (
            "<h1>{{ invoice.display_number }}</h1>"
        )
        versions = conn.execute(sa.text("SELECT html FROM invoice_html_template_versions ORDER BY id")).fetchall()
        assert [v[0] for v in versions] == ["No. {{ invoice.display_number }}", "no number here"]
        pdf = conn.execute(
            sa.text("SELECT template_html, template_json, design_json FROM invoice_pdf_templates")
        ).first()
        assert pdf[0] == "{{ invoice.display_number }}" and "invoice.display_number" in pdf[1] and pdf[2] is None
        assert conn.execute(sa.text("SELECT invoice_pdf_template_html FROM settings")).scalar() == (
            "#{{ invoice.display_number }}"
        )
        assert (
            "invoice.display_number" in conn.execute(sa.text("SELECT invoice_pdf_design_json FROM settings")).scalar()
        )
        assert conn.execute(sa.text("SELECT html FROM invoice_templates")).scalar() == (
            "Invoice {{ invoice.display_number }}"
        )


def test_one_active_default_series_per_entity(engine):
    migration = _load_migration()
    _run(engine, migration.upgrade)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO billing_entity_number_series (billing_entity_id, number_format, printed_format, "
                    "reset_yearly, status, created_at, updated_at) VALUES (1, 'X-{SEQ}', '{NUMBER}', false, 'active', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    # an archived one next to it is fine, and a legal entity of a client needs the client
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO billing_entity_number_series (billing_entity_id, number_format, printed_format, "
                "reset_yearly, status, created_at, updated_at) VALUES (1, 'X-{SEQ}', '{NUMBER}', false, 'archived', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO billing_entity_number_series (billing_entity_id, client_entity_id, number_format, "
                    "printed_format, reset_yearly, status, created_at, updated_at) VALUES (3, 5, 'Y-{SEQ}', "
                    "'{NUMBER}', false, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )


def test_downgrade_restores_format_counter_and_templates(engine):
    migration = _load_migration()
    _run(engine, migration.upgrade)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE billing_entities SET number_format = NULL, number_counter = 0"))
        series_id = conn.execute(
            sa.text("SELECT id FROM billing_entity_number_series WHERE billing_entity_id = 1")
        ).scalar()
        conn.execute(
            sa.text("UPDATE billing_entity_number_series SET reset_yearly = true WHERE id = :s"), {"s": series_id}
        )
        conn.execute(
            sa.text(
                "INSERT INTO billing_entity_number_counters (series_id, year, last_seq) VALUES (:s, 2026, 7), "
                "(:s, 2027, 2), (:s, 2028, 0)"
            ),
            {"s": series_id},
        )
    _run(engine, migration.downgrade)

    assert not {"number_series_id", "printed_number", "printed_number_edited"} & _columns(engine, "invoices")
    assert "billing_entity_number_series" not in sa.inspect(engine).get_table_names()
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, number_format, number_counter FROM billing_entities ORDER BY id")
        ).fetchall()
        # The counter of the latest year with a number given: the next year's row (0) does not reset it
        assert [tuple(r) for r in rows] == [(1, "ACME-{YYYY}-{SEQ}", 2), (2, "OLD-{SEQ}", 3), (3, None, 0)]
        assert conn.execute(sa.text("SELECT html FROM invoice_html_templates")).scalar() == (
            "<h1>{{ invoice.invoice_number }}</h1>"
        )
        assert conn.execute(sa.text("SELECT html FROM invoice_templates")).scalar() == (
            "Invoice {{ invoice.invoice_number }}"
        )
        assert conn.execute(sa.text("SELECT invoice_pdf_design_json FROM settings")).scalar() == (
            '{"text": "{{ invoice.invoice_number }}"}'
        )


def test_upgrade_twice_is_harmless(engine):
    migration = _load_migration()
    _run(engine, migration.upgrade)
    _run(engine, migration.upgrade)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM billing_entity_number_series")).scalar() == 2
        assert conn.execute(sa.text("SELECT html FROM invoice_html_templates")).scalar() == (
            "<h1>{{ invoice.display_number }}</h1>"
        )
