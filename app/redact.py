def redact_iban(value: str | None) -> str:
    """Keep only the country code and last 4 characters: BG80BNBG96611020345678 -> BG**...5678."""
    if not value:
        return ""
    v = value.replace(" ", "")
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:2]}**...{v[-4:]}"
