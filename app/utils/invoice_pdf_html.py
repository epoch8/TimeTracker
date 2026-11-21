"""Invoice PDF from an HTML template.

The template is a plain Jinja page with its own CSS, laid out in normal document
flow (no absolute coordinates). WeasyPrint turns it into a PDF, so long tables
break across pages on their own and repeat their header.

Templates live in ``app/templates/invoices/pdf_html/<name>.html``. The same HTML
is served as a browser preview, so what you see in the preview is what ends up
in the PDF.

Switch it on with ``INVOICE_PDF_RENDERER=html``; pick the template with
``INVOICE_PDF_HTML_TEMPLATE`` (default ``contract``). With the switch off,
invoices print through the stock layout designer exactly as before.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from flask import current_app, render_template
from markupsafe import Markup, escape

TEMPLATE_DIR = "invoices/pdf_html"
DEFAULT_TEMPLATE = "contract"
DEFAULT_DATE_FORMAT = "%d/%m/%Y"

_FONTS_DIR = Path(__file__).resolve().parent.parent / "resources" / "fonts"
_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates" / TEMPLATE_DIR
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


def html_renderer_enabled() -> bool:
    """True when invoices should print through the HTML renderer."""
    try:
        value = current_app.config.get("INVOICE_PDF_RENDERER", "designer")
    except RuntimeError:
        return False
    return str(value or "").strip().lower() == "html"


def available_templates() -> List[str]:
    """Names of the HTML invoice templates shipped in the repo."""
    if not _TEMPLATES_ROOT.is_dir():
        return []
    return sorted(p.stem for p in _TEMPLATES_ROOT.glob("*.html") if not p.name.startswith("_"))


def resolve_template_name(name: Optional[str] = None) -> str:
    """Return a safe, existing template name; fall back to the default."""
    if not name:
        try:
            name = current_app.config.get("INVOICE_PDF_HTML_TEMPLATE") or DEFAULT_TEMPLATE
        except RuntimeError:
            name = DEFAULT_TEMPLATE
    name = str(name).strip().lower()
    if not _NAME_RE.match(name) or name not in available_templates():
        return DEFAULT_TEMPLATE
    return name


# ---------------------------------------------------------------------------
# Formatting helpers (exposed to templates as filters / functions)
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def format_amount(value: Any, thousands: str = " ", decimals: int = 2) -> str:
    """16666.66 -> '16 666.66' (non-breaking space between thousands)."""
    number = _to_decimal(value).quantize(Decimal(1).scaleb(-decimals))
    text = f"{number:,.{decimals}f}"
    return text.replace(",", thousands)


def format_quantity(value: Any) -> str:
    """Hours without trailing zeros: 96 -> '96', 24.50 -> '24.5'."""
    number = _to_decimal(value).normalize()
    text = f"{number:f}"
    return text


def format_doc_date(value: Any, fmt: Optional[str] = None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return str(value)
    if fmt is None:
        try:
            fmt = current_app.config.get("INVOICE_PDF_HTML_DATE_FORMAT") or DEFAULT_DATE_FORMAT
        except RuntimeError:
            fmt = DEFAULT_DATE_FORMAT
    return value.strftime(fmt)


def text_lines(value: Any) -> Markup:
    """Escape text and keep its line breaks."""
    if not value:
        return Markup("")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return Markup("<br>").join(escape(line) for line in text.split("\n"))


def text_paragraphs(value: Any) -> Markup:
    """Blank line = new paragraph, single line break = <br>."""
    if not value:
        return Markup("")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    return Markup("").join(Markup("<p>") + text_lines(b) + Markup("</p>") for b in blocks)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _as_list(rel: Any) -> list:
    if rel is None:
        return []
    if hasattr(rel, "all"):
        try:
            return list(rel.all())
        except Exception:
            return []
    try:
        return list(rel)
    except TypeError:
        return []


def _line_items(invoice) -> List[SimpleNamespace]:
    """Items, extra goods and billed expenses as one list of rows."""
    rows: List[SimpleNamespace] = []

    items = _as_list(getattr(invoice, "items", None))
    items.sort(key=lambda i: (getattr(i, "position", 0) or 0, getattr(i, "id", 0) or 0))
    for item in items:
        rows.append(
            SimpleNamespace(
                description=getattr(item, "description", "") or "",
                quantity=_to_decimal(getattr(item, "quantity", 1)),
                unit_price=_to_decimal(getattr(item, "unit_price", 0)),
                total=_to_decimal(getattr(item, "total_amount", 0)),
            )
        )

    for good in _as_list(getattr(invoice, "extra_goods", None)):
        parts = [getattr(good, "name", "") or ""]
        if getattr(good, "description", None):
            parts.append(str(good.description))
        rows.append(
            SimpleNamespace(
                description="\n".join(p for p in parts if p),
                quantity=_to_decimal(getattr(good, "quantity", 1)),
                unit_price=_to_decimal(getattr(good, "unit_price", 0)),
                total=_to_decimal(getattr(good, "total_amount", 0)),
            )
        )

    for expense in _as_list(getattr(invoice, "expenses", None)):
        parts = [getattr(expense, "title", "") or ""]
        if getattr(expense, "description", None):
            parts.append(str(expense.description))
        amount = _to_decimal(getattr(expense, "total_amount", None) or getattr(expense, "amount", 0))
        rows.append(
            SimpleNamespace(
                description="\n".join(p for p in parts if p),
                quantity=Decimal("1"),
                unit_price=amount,
                total=amount,
            )
        )

    for index, row in enumerate(rows, start=1):
        row.number = index
    return rows


def _seller(settings) -> SimpleNamespace:
    """Who issues the invoice. Today: Settings. Later: the invoice's legal entity."""

    def get(attr: str) -> str:
        return (getattr(settings, attr, "") or "").strip() if settings is not None else ""

    return SimpleNamespace(
        name=get("company_name"),
        tax_id=get("company_tax_id"),
        address=get("company_address"),
        email=get("company_email"),
        phone=get("company_phone"),
        website=get("company_website"),
        bank_info=get("company_bank_info"),
    )


def _buyer(invoice) -> SimpleNamespace:
    client = getattr(invoice, "client", None)
    custom = getattr(client, "custom_fields", None) or {}
    if not isinstance(custom, dict):
        custom = {}
    return SimpleNamespace(
        name=(getattr(invoice, "client_name", None) or getattr(client, "name", "") or "").strip(),
        tax_id=(getattr(client, "vat_id", None) or "").strip(),
        address=(getattr(invoice, "client_address", None) or getattr(client, "address", None) or "").strip(),
        email=(getattr(invoice, "client_email", None) or getattr(client, "email", None) or "").strip(),
        bank_info=str(custom.get("bank_details") or "").strip(),
    )


def build_context(invoice, settings=None) -> Dict[str, Any]:
    if settings is None:
        from app.models import Settings

        settings = Settings.get_settings()

    rows = _line_items(invoice)
    subtotal = _to_decimal(getattr(invoice, "subtotal", None))
    tax_amount = _to_decimal(getattr(invoice, "tax_amount", None))
    discount = _to_decimal(getattr(invoice, "discount_amount", None))
    total = _to_decimal(getattr(invoice, "total_amount", None))
    if not total and rows:
        total = sum((r.total for r in rows), Decimal("0"))

    terms = getattr(invoice, "terms", None) or getattr(settings, "invoice_terms", None) or ""

    return {
        "invoice": invoice,
        "seller": _seller(settings),
        "buyer": _buyer(invoice),
        "rows": rows,
        "show_hours": any(r.quantity != 1 for r in rows),
        "currency": (getattr(invoice, "currency_code", None) or getattr(settings, "currency", None) or "EUR"),
        "subtotal": subtotal,
        "tax_rate": _to_decimal(getattr(invoice, "tax_rate", None)),
        "tax_amount": tax_amount,
        "discount": discount,
        "show_breakdown": bool(tax_amount or discount),
        "total": total,
        "payment_purpose": getattr(invoice, "notes", None) or "",
        "terms": terms,
        "fonts_url": _FONTS_DIR.as_uri() + "/",
        "money": format_amount,
        "qty": format_quantity,
        "doc_date": format_doc_date,
        "lines": text_lines,
        "paragraphs": text_paragraphs,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_invoice_html(invoice, settings=None, template_name: Optional[str] = None) -> str:
    name = resolve_template_name(template_name)
    return render_template(f"{TEMPLATE_DIR}/{name}.html", **build_context(invoice, settings))


def render_invoice_pdf(invoice, settings=None, template_name: Optional[str] = None) -> bytes:
    from weasyprint import HTML  # imported lazily: native deps (pango) are only needed here

    html = render_invoice_html(invoice, settings=settings, template_name=template_name)
    return HTML(string=html, base_url=str(_FONTS_DIR)).write_pdf()
