from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import ChangeAudit, User


def record_change(
    db: Session,
    actor: User | None,
    *,
    entity_type: str,
    entity_id: str | int,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> ChangeAudit:
    row = ChangeAudit(
        actor_id=actor.id if actor else None,
        actor_username=actor.username if actor else None,
        entity_type=entity_type,
        entity_id=str(entity_id),
        action=action,
        before_json=_json_or_none(before),
        after_json=_json_or_none(after),
    )
    db.add(row)
    return row


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"))


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
