from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def actor_tg(telegram_id: int) -> str:
    return f"tg:{telegram_id}"


def audit(
    session: AsyncSession,
    actor: str,
    action: str,
    entity: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Queue an audit row in the caller's transaction (committed with the state change)."""
    session.add(AuditLog(actor=actor, action=action, entity=entity, payload=payload or {}))
