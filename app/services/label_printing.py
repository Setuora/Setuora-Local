from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Serial, User


class LabelPrintError(ValueError):
    pass


def mark_serial_labels_printed_once(db: Session, user: User, serial_ids: list[int]) -> list[Serial]:
    ids = list(dict.fromkeys(serial_ids))
    if not ids:
        raise LabelPrintError("No labels selected")

    serials = db.scalars(select(Serial).where(Serial.id.in_(ids))).all()
    if len(serials) != len(ids):
        raise LabelPrintError("Some labels were not found")

    already_printed = [serial.serial_number for serial in serials if serial.label_printed_at]
    if already_printed:
        joined = ", ".join(sorted(already_printed)[:5])
        suffix = "..." if len(already_printed) > 5 else ""
        raise LabelPrintError(f"Print option already used for {joined}{suffix}")

    printed_at = datetime.now(timezone.utc)
    result = db.execute(
        update(Serial)
        .where(Serial.id.in_(ids), Serial.label_printed_at.is_(None))
        .values(label_printed_at=printed_at, label_printed_by_id=user.id)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != len(ids):
        db.rollback()
        serials = db.scalars(select(Serial).where(Serial.id.in_(ids))).all()
        already_printed = [serial.serial_number for serial in serials if serial.label_printed_at]
        joined = ", ".join(sorted(already_printed)[:5])
        suffix = "..." if len(already_printed) > 5 else ""
        raise LabelPrintError(f"Print option already used for {joined}{suffix}".strip())
    db.commit()
    serials = db.scalars(select(Serial).where(Serial.id.in_(ids))).all()
    return serials
