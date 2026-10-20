import hmac
import secrets

from fastapi import Request
from starlette.datastructures import FormData


class LoginRequired(Exception):
    """Raised by require_admin; turned into a redirect to the login page."""


class CsrfFailed(Exception):
    pass


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise LoginRequired()


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, form: FormData) -> None:
    expected = request.session.get("csrf")
    sent = form.get("csrf")
    if not expected or not isinstance(sent, str) or not hmac.compare_digest(expected, sent):
        raise CsrfFailed()


def password_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())
