"""Variation knobs for an invoice template. Stored as JSON in InvoiceTemplate.options."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, field_validator

LAYOUTS: dict[str, str] = {
    "classic": "Classic - header band, boxed parties",
    "modern": "Modern - accent sidebar, large total",
    "minimal": "Minimal - monochrome, lots of white space",
}

FONTS: dict[str, str] = {
    "Inter": "'Inter', 'Helvetica Neue', Arial, sans-serif",
    "Roboto": "'Roboto', Arial, sans-serif",
    "Source Serif": "'Source Serif 4', Georgia, serif",
    "IBM Plex Sans": "'IBM Plex Sans', Arial, sans-serif",
    "Courier Prime": "'Courier Prime', 'Courier New', monospace",
}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class TemplateOptions(BaseModel):
    accent_color: str = "#1F3A5F"
    font: str = "Inter"
    title: str = "INVOICE"
    logo_position: Literal["left", "right", "none"] = "left"
    show_bank_details: bool = True
    show_vat_breakdown: bool = True
    show_signature: bool = False
    show_due_date: bool = True
    show_line_numbers: bool = False
    show_amount_in_words: bool = True
    show_company_footer: bool = True  # legal details line at the bottom of the page
    subtitle: str = ""  # e.g. "Original"
    header_note: str = ""
    footer_note: str = ""

    @field_validator("accent_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _HEX.match(v):
            raise ValueError("accent colour must look like #1F3A5F")
        return v.upper()

    @field_validator("font")
    @classmethod
    def _font(cls, v: str) -> str:
        if v not in FONTS:
            raise ValueError(f"unknown font {v!r}")
        return v

    @property
    def font_stack(self) -> str:
        return FONTS[self.font]

    @classmethod
    def parse_lenient(cls, raw: dict[str, Any] | None) -> "TemplateOptions":
        """Options from the DB: drop invalid values instead of failing a render."""
        raw = dict(raw or {})
        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            for err in e.errors():
                if err["loc"]:
                    raw.pop(str(err["loc"][0]), None)
            return cls.model_validate(raw)
