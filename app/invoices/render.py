from datetime import date
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.invoices.options import LAYOUTS, TemplateOptions
from app.invoices.view import InvoiceView

LAYOUT_DIR = Path(__file__).parent / "layouts"


def _money(v: Decimal) -> str:
    return f"{v:,.2f}"


def _qty(v: Decimal) -> str:
    return f"{v.normalize():f}"


def _date(v: date | None) -> str:
    return v.strftime("%d.%m.%Y") if v else ""


_env = Environment(
    loader=FileSystemLoader(LAYOUT_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters.update(money=_money, qty=_qty, date=_date)


def render_invoice_html(inv: InvoiceView, layout: str, options: TemplateOptions) -> str:
    if layout not in LAYOUTS:
        layout = "classic"
    tpl = _env.get_template(f"{layout}.html.j2")
    return tpl.render(inv=inv, o=options)
