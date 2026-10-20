from datetime import date
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.invoices.options import LAYOUTS, TemplateOptions
from app.invoices.view import InvoiceView
from app.invoices.words import amount_in_words

LAYOUT_DIR = Path(__file__).parent / "layouts"


def _money(v: Decimal) -> str:
    return f"{v:,.2f}"


def _qty(v: Decimal) -> str:
    return f"{v.normalize():f}"


def _tint(hex_color: str, white: float) -> str:
    """Mix a #RRGGBB colour with white (0 = unchanged, 1 = white)."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    r, g, b = (round(c + (255 - c) * white) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def _date(v: date | None) -> str:
    return v.strftime("%d.%m.%Y") if v else ""


_env = Environment(
    loader=FileSystemLoader(LAYOUT_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters.update(money=_money, qty=_qty, date=_date, words=amount_in_words, tint=_tint)


def render_invoice_html(inv: InvoiceView, layout: str, options: TemplateOptions) -> str:
    if layout not in LAYOUTS:
        layout = "classic"
    tpl = _env.get_template(f"{layout}.html.j2")
    return tpl.render(inv=inv, o=options)
