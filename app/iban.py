import re


def normalize_iban(raw: str) -> str:
    return re.sub(r"\s+", "", raw).upper()


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check."""
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1
