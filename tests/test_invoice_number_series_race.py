"""Two invoices of one series issued at the same moment get different numbers (PostgreSQL).

The counter row is locked (``SELECT … FOR UPDATE``) while a number is given, so the second waits for
the first to commit. Needs a PostgreSQL database with the schema (``flask db upgrade``): set
POSTGRES_TEST_DATABASE_URL. The test adds its own rows and deletes them.
SQLite has no row locks; there a concurrent issue fails with "try again" (see test_invoice_number_series).
"""

import os
import threading
import uuid
from datetime import date, timedelta

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.database]

URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")


@pytest.mark.skipif(not URL, reason="POSTGRES_TEST_DATABASE_URL is not set")
def test_concurrent_issue_gets_different_numbers(app_config):
    from app import create_app, db
    from app.models import BillingEntity, BillingEntityNumberSeries, Client, Invoice, Project, User
    from app.services import InvoiceService
    from app.utils import billing_entities as billing

    config = dict(app_config)
    config["SQLALCHEMY_DATABASE_URI"] = URL
    config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
    app = create_app(config)
    tag = uuid.uuid4().hex[:8]
    with app.app_context():
        user = User(username=f"race-{tag}", role="admin")
        client = Client(name=f"Race Client {tag}")
        entity = BillingEntity(short_name=f"Acme {tag}", legal_name="ACME LTD")
        db.session.add_all([user, client, entity])
        db.session.flush()
        project = Project(name="Race", client_id=client.id)
        db.session.add(project)
        db.session.add(BillingEntityNumberSeries(billing_entity_id=entity.id, number_format=f"RACE{tag}-{{SEQ}}"))
        db.session.flush()
        ids = []
        for _ in range(2):
            inv = Invoice(
                invoice_number="TMP",
                project_id=project.id,
                client_name=client.name,
                due_date=date.today() + timedelta(days=30),
                created_by=user.id,
                client_id=client.id,
            )
            inv.billing_entity_id = entity.id
            billing.number_new_draft(inv)
            db.session.add(inv)
            db.session.flush()
            ids.append(inv.id)
        db.session.commit()
        user_id, client_id, entity_id, project_id = user.id, client.id, entity.id, project.id

    barrier = threading.Barrier(2)
    results = {}

    def issue(invoice_id):
        with app.app_context():
            invoice = db.session.get(Invoice, invoice_id)
            barrier.wait()
            result = InvoiceService().transition_status(invoice, "issued")
            if result.get("success"):
                db.session.commit()
                results[invoice_id] = invoice.invoice_number
            else:
                results[invoice_id] = result.get("error")
            db.session.remove()

    threads = [threading.Thread(target=issue, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    try:
        assert sorted(results.values()) == [f"RACE{tag}-1", f"RACE{tag}-2"]
    finally:
        with app.app_context():
            for invoice_id in ids:
                db.session.delete(db.session.get(Invoice, invoice_id))
            db.session.flush()
            for obj in (db.session.get(Project, project_id), db.session.get(BillingEntity, entity_id)):
                db.session.delete(obj)
            db.session.flush()
            db.session.delete(db.session.get(Client, client_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
