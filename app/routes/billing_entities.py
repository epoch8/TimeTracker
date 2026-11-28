"""Admin pages of the Legal entities module: our billing entities and their payment accounts."""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app import db, log_event
from app.models import BillingEntity, PaymentAccount
from app.utils.db import safe_commit
from app.utils.module_helpers import module_enabled
from app.utils.permissions import admin_or_permission_required

billing_entities_bp = Blueprint("billing_entities", __name__)

VAT_CATEGORIES = ("S", "Z", "E", "AE", "K", "G", "O", "L", "M")


def _clean(name, limit=None):
    value = (request.form.get(name, "") or "").strip()
    if limit:
        value = value[:limit]
    return value or None


def _entities():
    return BillingEntity.query.order_by(BillingEntity.status, BillingEntity.short_name).all()


def _fill_entity(entity):
    entity.short_name = _clean("short_name", 200) or entity.short_name
    entity.legal_name = _clean("legal_name", 255) or entity.short_name
    country = (_clean("country", 2) or "").upper()
    entity.country = country or None
    entity.registration_number = _clean("registration_number", 100)
    entity.vat_number = _clean("vat_number", 100)
    entity.address = _clean("address")
    entity.email = _clean("email", 200)
    entity.phone = _clean("phone", 50)
    entity.website = _clean("website", 200)
    vat = (_clean("default_vat_category", 5) or "").upper()
    entity.default_vat_category = vat if vat in VAT_CATEGORIES else None
    rate = _clean("default_tax_rate")
    try:
        entity.default_tax_rate = Decimal(rate) if rate else None
    except InvalidOperation:
        entity.default_tax_rate = None
    entity.default_vat_exemption_code = _clean("default_vat_exemption_code", 50)
    entity.default_vat_exemption_reason = _clean("default_vat_exemption_reason")
    entity.default_terms = _clean("default_terms")
    entity.default_notes = _clean("default_notes")
    if "pdf_template" in request.form:
        from app.utils.invoice_templates import ref_exists

        ref = _clean("pdf_template", 64)
        entity.pdf_template = ref if ref and ref_exists(ref) else None


LOGO_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")


def _apply_logo(entity):
    """Save an uploaded logo, or drop the current one. Returns an error message or None.

    Every upload gets a new file name and old files are kept: invoices that already left
    draft carry the file name in their seller snapshot and keep printing their logo.
    """
    if request.form.get("remove_logo"):
        entity.logo_filename = None
    file = request.files.get("logo")
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in LOGO_EXTENSIONS:
        return _("Logo must be a PNG, JPG, GIF or WebP image")
    try:
        from PIL import Image

        file.stream.seek(0)
        Image.open(file.stream).verify()
        file.stream.seek(0)
    except Exception:
        return _("Logo must be a PNG, JPG, GIF or WebP image")
    import os
    import uuid

    from flask import current_app

    folder = os.path.realpath(os.path.join(current_app.root_path, "static", "uploads", "logos"))
    os.makedirs(folder, exist_ok=True)
    name = f"entity_logo_{uuid.uuid4().hex[:12]}.{ext}"
    path = os.path.realpath(os.path.join(folder, name))
    if not path.startswith(folder + os.sep):
        return _("Logo must be a PNG, JPG, GIF or WebP image")
    file.save(path)
    entity.logo_filename = name
    return None


@billing_entities_bp.route("/admin/legal-entities")
@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>")
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def index(entity_id=None):
    entities = _entities()
    entity = None
    if entity_id:
        entity = BillingEntity.query.get_or_404(entity_id)
    elif entities:
        entity = entities[0]
    from app.utils.billing_entities import numbering_context

    return render_template(
        "billing_entities/index.html",
        entities=entities,
        entity=entity,
        vat_categories=VAT_CATEGORIES,
        numbering=numbering_context(entity) if entity else None,
    )


@billing_entities_bp.route("/admin/legal-entities/new", methods=["GET", "POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def create():
    if request.method == "GET":
        return _render_new(None)
    short_name = _clean("short_name", 200)
    entity = BillingEntity(short_name=short_name, legal_name=short_name)
    _fill_entity(entity)
    if not short_name:
        flash(_("Short name is required"), "error")
        return _render_new(entity), 400
    logo_error = _apply_logo(entity)
    if logo_error:
        flash(logo_error, "error")
        return _render_new(entity), 400
    db.session.add(entity)
    number_format = _clean("number_format", 120)
    if number_format:
        # The format typed on the Add page becomes the entity's default series
        from app.models import BillingEntityNumberSeries

        db.session.flush()
        db.session.add(BillingEntityNumberSeries(billing_entity_id=entity.id, number_format=number_format))
    if not safe_commit("billing_entity_create", {"short_name": short_name}):
        flash(_("Could not save the legal entity"), "error")
        return _render_new(entity), 400
    log_event("billing_entity.created", user_id=current_user.id, billing_entity_id=entity.id)
    # Step 2: the entity's first payment account, with a "created" banner and a Skip link
    return redirect(url_for("billing_entities.new_account", entity_id=entity.id, created=1))


def _render_new(entity):
    """The Add entity page; ``entity`` is an unsaved one holding the typed values after an error."""
    return render_template("billing_entities/new.html", entity=entity, vat_categories=VAT_CATEGORIES)


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>", methods=["POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def update(entity_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    _fill_entity(entity)
    logo_error = _apply_logo(entity)
    if logo_error:
        flash(logo_error, "error")
    if not safe_commit("billing_entity_update", {"billing_entity_id": entity.id}):
        flash(_("Could not save the legal entity"), "error")
    else:
        flash(_("Legal entity saved"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id))


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/archive", methods=["POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def toggle_archive(entity_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    entity.status = "active" if entity.status == "archived" else "archived"
    safe_commit("billing_entity_archive", {"billing_entity_id": entity.id})
    flash(_("Legal entity archived") if entity.status == "archived" else _("Legal entity restored"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id))


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/delete", methods=["POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def delete(entity_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    if entity.is_used():
        flash(_("Used on invoices, archive it instead"), "error")
        return redirect(url_for("billing_entities.index", entity_id=entity.id))
    db.session.delete(entity)
    safe_commit("billing_entity_delete", {"billing_entity_id": entity_id})
    flash(_("Legal entity deleted"), "success")
    return redirect(url_for("billing_entities.index"))


# --- Payment accounts ---------------------------------------------------------------------------


def _account_or_404(entity, account_id):
    account = PaymentAccount.query.get_or_404(account_id)
    if account.billing_entity_id != entity.id:
        abort(404)
    return account


def _render_account(entity, account, form=None):
    return render_template(
        "billing_entities/account.html",
        entity=entity,
        account=account,
        form=form or {},
        created=request.args.get("created") == "1",
    )


def _save_account(entity, account):
    currency = (_clean("currency", 3) or "").upper()
    iban = (_clean("iban", 64) or "").replace(" ", "").upper()
    if len(currency) != 3 or not iban:
        flash(_("Currency and IBAN are required"), "error")
        return None
    duplicate = PaymentAccount.query.filter_by(iban=iban, currency=currency).first()
    if duplicate and (account is None or duplicate.id != account.id):
        flash(_("An account with this IBAN and currency already exists"), "error")
        return None
    if account is None:
        account = PaymentAccount(billing_entity_id=entity.id, currency=currency, iban=iban)
        db.session.add(account)
    account.currency = currency
    account.iban = iban
    account.bank_name = _clean("bank_name", 120)
    status = request.form.get("status", "active")
    account.status = status if status in ("active", "archived") else "active"

    account.details_text = (request.form.get("details_text") or "").strip() or None
    return account


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/accounts/new", methods=["GET", "POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def new_account(entity_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    if request.method == "GET":
        return _render_account(entity, None)
    account = _save_account(entity, None)
    if account is None or not safe_commit("payment_account_create", {"billing_entity_id": entity.id}):
        db.session.rollback()
        return _render_account(entity, None, form=request.form), 400
    flash(_("Payment account created"), "success")
    if request.form.get("next") == "another":
        return redirect(url_for("billing_entities.new_account", entity_id=entity.id))
    return redirect(url_for("billing_entities.index", entity_id=entity.id))


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/accounts/<int:account_id>", methods=["GET", "POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def account(entity_id, account_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    account = _account_or_404(entity, account_id)
    if request.method == "GET":
        return _render_account(entity, account)
    if _save_account(entity, account) is None or not safe_commit(
        "payment_account_update", {"payment_account_id": account.id}
    ):
        db.session.rollback()
        return redirect(url_for("billing_entities.account", entity_id=entity.id, account_id=account.id))
    flash(_("Payment account saved"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id))


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/accounts/<int:account_id>/archive", methods=["POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def archive_account(entity_id, account_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    account = _account_or_404(entity, account_id)
    account.status = "active" if account.status == "archived" else "archived"
    safe_commit("payment_account_archive", {"payment_account_id": account.id})
    flash(_("Payment account archived") if account.status == "archived" else _("Payment account restored"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id))


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/accounts/<int:account_id>/delete", methods=["POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def delete_account(entity_id, account_id):
    entity = BillingEntity.query.get_or_404(entity_id)
    account = _account_or_404(entity, account_id)
    if account.is_used():
        flash(_("Used on an invoice, archive it instead"), "error")
        return redirect(url_for("billing_entities.account", entity_id=entity.id, account_id=account.id))
    db.session.delete(account)
    safe_commit("payment_account_delete", {"payment_account_id": account_id})
    flash(_("Payment account deleted"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id))


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/preview-pdf")
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def preview_pdf(entity_id):
    """A trial invoice from this entity, printed with the template chosen in the form.

    ``?template=`` is the value of the template select, saved or not: ``default`` for the
    default from Settings, else a template reference. Without it, the entity's saved choice. The invoice is the entity's
    latest one; without one, the latest invoice with no legal entity printed with this
    entity's details; for HTML templates without any invoice, a sample.
    """
    import io

    from flask import send_file

    from app.models import Invoice, Settings
    from app.utils.billing_entities import SellerSettings
    from app.utils.invoice_pdf_html import TemplateError, render_invoice_pdf
    from app.utils.invoice_templates import default_template_ref, load_ref, ref_exists
    from app.utils.pdf_generator import InvoicePDFGenerator

    entity = BillingEntity.query.get_or_404(entity_id)
    settings = Settings.get_settings()
    # no ?template (a bookmarked link): what the entity has saved
    wanted = (request.args.get("template") or entity.pdf_template or "default").strip()
    if wanted == "default" or not ref_exists(wanted):
        wanted = default_template_ref(settings)
    resolved = load_ref(wanted)

    account = next((a for a in entity.payment_accounts if a.is_active), None)
    entity_seller = SellerSettings(
        settings, entity.seller_snapshot(), account.printed_text if account else "", account.iban if account else ""
    )
    invoice = Invoice.query.filter_by(billing_entity_id=entity.id).order_by(Invoice.id.desc()).first()
    if invoice is None:
        invoice = Invoice.query.filter(Invoice.billing_entity_id.is_(None)).order_by(Invoice.id.desc()).first()

    try:
        if invoice is not None:
            generator = InvoicePDFGenerator(invoice, settings=entity_seller)
            generator.template_ref = resolved.ref
            pdf = generator.generate_pdf()
        elif resolved.is_visual:
            flash(_("Create an invoice first: the Visual layout is previewed on a real invoice."), "info")
            return redirect(url_for("billing_entities.index", entity_id=entity.id))
        else:
            from app.routes.invoice_templates import sample_invoice

            sample = sample_invoice()
            sample.payment_details_text = account.printed_text if account else ""
            pdf = render_invoice_pdf(resolved.html, sample, entity_seller)
    except TemplateError as exc:
        flash(_("The template could not be printed: %(err)s", err=exc.message), "error")
        return redirect(url_for("billing_entities.index", entity_id=entity.id))
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=False, download_name="preview.pdf")


# --- Numbering: the default series and the series by client ---------------------------------------------


def _int_field(name):
    value = (request.form.get(name, "") or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return 0


def _posted_next_seqs(years):
    return {year: _int_field(f"next_seq_{year}") for year in years}


def _flash_checks(errors, warnings):
    for item in errors:
        flash(item["message"], "error")
    for item in warnings:
        flash(item["message"], "warning")


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/numbering", methods=["POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def save_default_series(entity_id):
    """Save the entity's default series: format, printed format, Next {SEQ}, Restart every year."""
    from app.models import BillingEntityNumberSeries
    from app.utils import billing_entities as billing

    entity = BillingEntity.query.get_or_404(entity_id)
    series = entity.number_series.filter(
        BillingEntityNumberSeries.client_id.is_(None), BillingEntityNumberSeries.status == "active"
    ).first()
    created = series is None
    if created:
        series = BillingEntityNumberSeries(billing_entity_id=entity.id, status="active")
    number_format = _clean("number_format", 120) or ""
    printed_format = _clean("printed_format", 120) or "{NUMBER}"
    reset_yearly = bool(request.form.get("reset_yearly"))
    probe = BillingEntityNumberSeries(reset_yearly=reset_yearly)
    next_seqs = _posted_next_seqs(billing.next_seq_years(probe))
    errors, warnings = billing.check_series(series, number_format, printed_format, reset_yearly, next_seqs)
    if errors:
        db.session.rollback()
        _flash_checks(errors, [])
        return redirect(url_for("billing_entities.index", entity_id=entity.id) + "#numbering")
    series.number_format = number_format
    series.printed_format = printed_format
    series.reset_yearly = reset_yearly
    if created:
        db.session.add(series)
        db.session.flush()
        if not any(next_seqs.values()) and entity.number_counter:
            # Carry on from the counter the entity had before it had series
            billing.set_last_seq(series, 0, entity.number_counter)
    billing.save_next_seqs(series, next_seqs)
    if not safe_commit("billing_entity_default_series", {"billing_entity_id": entity.id}):
        flash(_("Could not save the numbering"), "error")
    else:
        _flash_checks([], warnings)
        flash(_("Numbering saved"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id) + "#numbering")


def _series_or_404(entity, series_id):
    from app.models import BillingEntityNumberSeries

    series = BillingEntityNumberSeries.query.get_or_404(series_id)
    if series.billing_entity_id != entity.id:
        abort(404)
    return series


def _render_series(entity, series, form=None, errors=None, warnings=None, status=200):
    from app.models import Client
    from app.utils import billing_entities as billing

    clients = Client.query.filter_by(status="active").order_by(Client.name).all()
    if series is not None and series.client_id and all(c.id != series.client_id for c in clients):
        clients.append(series.client)
    choices = billing.client_entity_choices([c.id for c in clients])
    preview = None
    if series is not None and series.id and series.number_format:
        preview = billing.series_preview(series)
    return (
        render_template(
            "billing_entities/series.html",
            entity=entity,
            series=series,
            form=form or {},
            clients=clients,
            client_entities={str(k): v for k, v in choices.items()},
            client_entities_on=billing.client_entities_on(),
            kept_bill_to=(
                billing.client_entity_name(series.client_entity_id)
                if series is not None and series.client_entity_id and not billing.client_entities_on()
                else ""
            ),
            next_seq=(billing.last_seq(series, 0) + 1) if series is not None and series.id else 1,
            preview=preview,
            errors=errors or [],
            warnings=warnings or [],
        ),
        status,
    )


@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/series/new", methods=["GET", "POST"])
@billing_entities_bp.route("/admin/legal-entities/<int:entity_id>/series/<int:series_id>", methods=["GET", "POST"])
@login_required
@module_enabled("billing_entities")
@admin_or_permission_required("manage_settings")
def series_form(entity_id, series_id=None):
    """Add or edit a series for a client (Bill to: one of its legal entities, or any)."""
    from sqlalchemy.exc import IntegrityError

    from app.models import BillingEntityNumberSeries, Client
    from app.utils import billing_entities as billing

    entity = BillingEntity.query.get_or_404(entity_id)
    series = _series_or_404(entity, series_id) if series_id else None
    if series is not None and series.client_id is None:
        # The default series is edited in the Numbering block of the entity page
        return redirect(url_for("billing_entities.index", entity_id=entity.id) + "#numbering")
    if request.method == "GET":
        return _render_series(entity, series)

    form = request.form
    client = Client.query.get(form.get("client_id", type=int) or 0)
    if client is None:
        flash(_("Choose a client"), "error")
        return _render_series(entity, series, form=form, status=400)
    bill_to_id = form.get("client_entity_id", type=int) or None
    if not billing.client_entities_on() and series is not None and series.client_id == client.id:
        # The form has no Bill to while the module is off: the series keeps the one it was made for
        bill_to_id = series.client_entity_id
    elif bill_to_id and billing.client_entity_owner(bill_to_id) != client.id:
        flash(_("That legal entity is not one of this client's"), "error")
        return _render_series(entity, series, form=form, status=400)
    status = form.get("status", "active")
    status = status if status in ("active", "archived") else "active"
    target = series or BillingEntityNumberSeries(billing_entity_id=entity.id)
    with db.session.no_autoflush:
        target.client_id = client.id
        target.client_entity_id = bill_to_id
        target.status = status
        number_format = _clean("number_format", 120) or ""
        printed_format = _clean("printed_format", 120) or "{NUMBER}"
        next_seqs = {0: _int_field("next_seq_0")}
        errors, warnings = billing.check_series(target, number_format, printed_format, False, next_seqs)
        clash = _active_clash(target)
    if clash is not None:
        errors.append(
            {
                "field": "client_entity_id",
                "message": (
                    _("Archive the active series first")
                    if series is not None and series.status == "archived"
                    else _("This client already has a series for this Bill to: edit that one")
                ),
                "use": None,
            }
        )
    if errors:
        db.session.rollback()
        return _render_series(entity, series, form=form, errors=errors, warnings=warnings, status=400)
    target.number_format = number_format
    target.printed_format = printed_format
    target.reset_yearly = False
    if series is None:
        db.session.add(target)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash(_("This client already has a series for this Bill to: edit that one"), "error")
        return _render_series(entity, None, form=form, status=400)
    billing.save_next_seqs(target, next_seqs)
    if not safe_commit("billing_entity_series", {"billing_entity_id": entity.id}):
        flash(_("Could not save the series"), "error")
        return _render_series(entity, series, form=form, status=400)
    _flash_checks([], warnings)
    flash(_("Series saved"), "success")
    return redirect(url_for("billing_entities.index", entity_id=entity.id) + "#numbering")


def _active_clash(series):
    """Another active series for the same client and Bill to (only one may be active)."""
    from app.models import BillingEntityNumberSeries as Series

    if series.status != "active":
        return None
    query = Series.query.filter(
        Series.billing_entity_id == series.billing_entity_id,
        Series.client_id == series.client_id,
        Series.status == "active",
    )
    if series.client_entity_id:
        query = query.filter(Series.client_entity_id == series.client_entity_id)
    else:
        query = query.filter(Series.client_entity_id.is_(None))
    if series.id:
        query = query.filter(Series.id != series.id)
    return query.first()
