from datetime import datetime, timezone
import json

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import LabelPrintLog, Role, Serial, User, has_any_role


class LabelPrintError(ValueError):
    pass


DEFAULT_LABEL_LAYOUT = {
    "page_size": "A4",
    "orientation": "portrait",
    "page_width_mm": 210,
    "page_height_mm": 297,
    "margin_top_mm": 8,
    "margin_bottom_mm": 8,
    "margin_left_mm": 8,
    "margin_right_mm": 8,
    "horizontal_spacing_mm": 0,
    "vertical_spacing_mm": 0,
    "label_width_mm": 48.5,
    "label_height_mm": 25.4,
    "qr_size_mm": 19.5,
    "rows": 11,
    "columns": 4,
    "start_position": 1,
    "scale_percent": 100,
    "printer_name": "",
}

_FLOAT_LIMITS = {
    "page_width_mm": (50, 500),
    "page_height_mm": (50, 500),
    "margin_top_mm": (0, 100),
    "margin_bottom_mm": (0, 100),
    "margin_left_mm": (0, 100),
    "margin_right_mm": (0, 100),
    "horizontal_spacing_mm": (0, 50),
    "vertical_spacing_mm": (0, 50),
    "label_width_mm": (10, 200),
    "label_height_mm": (10, 200),
    "qr_size_mm": (5, 100),
    "scale_percent": (50, 150),
}
_INT_LIMITS = {"rows": (1, 30), "columns": (1, 12), "start_position": (1, 360)}


def validate_label_layout(value: object) -> dict:
    """Return a complete, bounded label layout safe for persistence and CSS."""
    incoming = value if isinstance(value, dict) else {}
    layout = dict(DEFAULT_LABEL_LAYOUT)
    for key, (minimum, maximum) in _FLOAT_LIMITS.items():
        raw = incoming.get(key, layout[key])
        if isinstance(raw, bool):
            raise LabelPrintError(f"Invalid {key.replace('_', ' ')}")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise LabelPrintError(f"Invalid {key.replace('_', ' ')}") from exc
        if not minimum <= number <= maximum:
            raise LabelPrintError(f"{key.replace('_', ' ').title()} must be between {minimum} and {maximum}")
        layout[key] = round(number, 3)
    for key, (minimum, maximum) in _INT_LIMITS.items():
        raw = incoming.get(key, layout[key])
        if isinstance(raw, bool):
            raise LabelPrintError(f"Invalid {key.replace('_', ' ')}")
        try:
            number = int(raw)
        except (TypeError, ValueError) as exc:
            raise LabelPrintError(f"Invalid {key.replace('_', ' ')}") from exc
        if not minimum <= number <= maximum:
            raise LabelPrintError(f"{key.replace('_', ' ').title()} must be between {minimum} and {maximum}")
        layout[key] = number

    page_size = str(incoming.get("page_size", layout["page_size"])).strip()
    orientation = str(incoming.get("orientation", layout["orientation"])).strip().lower()
    if page_size not in {"A4", "Letter", "Custom"}:
        raise LabelPrintError("Invalid page size")
    if orientation not in {"portrait", "landscape"}:
        raise LabelPrintError("Invalid page orientation")
    layout["page_size"] = page_size
    layout["orientation"] = orientation
    layout["printer_name"] = str(incoming.get("printer_name", "")).strip()[:120]

    capacity = layout["rows"] * layout["columns"]
    if layout["start_position"] > capacity:
        raise LabelPrintError(f"Starting label position cannot exceed {capacity}")
    if layout["qr_size_mm"] > min(layout["label_width_mm"], layout["label_height_mm"]):
        raise LabelPrintError("QR size must fit inside the label")

    scale = layout["scale_percent"] / 100
    used_width = (
        layout["margin_left_mm"]
        + layout["margin_right_mm"]
        + scale
        * (
            layout["columns"] * layout["label_width_mm"]
            + (layout["columns"] - 1) * layout["horizontal_spacing_mm"]
        )
    )
    used_height = (
        layout["margin_top_mm"]
        + layout["margin_bottom_mm"]
        + scale
        * (
            layout["rows"] * layout["label_height_mm"]
            + (layout["rows"] - 1) * layout["vertical_spacing_mm"]
        )
    )
    page_width = layout["page_width_mm"]
    page_height = layout["page_height_mm"]
    if orientation == "landscape":
        page_width, page_height = page_height, page_width
    if used_width > page_width + 0.01 or used_height > page_height + 0.01:
        raise LabelPrintError(
            f"Layout needs {used_width:.1f} x {used_height:.1f} mm but the page is "
            f"{page_width:.1f} x {page_height:.1f} mm"
        )
    return layout


def user_is_label_admin(user: User) -> bool:
    return has_any_role(user.role, (Role.ADMIN, Role.SUPER_ADMIN))


def record_serial_label_prints(
    db: Session,
    user: User,
    serial_ids: list[int],
    *,
    copies: int = 1,
    reason: str = "",
    template_name: str = "",
    layout: object = None,
) -> list[LabelPrintLog]:
    """Apply role policy and persist one auditable event per selected QR."""
    ids = list(dict.fromkeys(serial_ids))
    if not ids:
        raise LabelPrintError("No labels selected")
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 100:
        raise LabelPrintError("Copies must be between 1 and 100")

    is_admin = user_is_label_admin(user)
    is_purchase = has_any_role(user.role, (Role.PURCHASE,))
    if not is_admin and not is_purchase:
        raise LabelPrintError("Only admin and purchase users can print QR labels")
    if not is_admin and copies != 1:
        raise LabelPrintError("Purchase users can print only one copy of each QR label")

    serials = db.scalars(select(Serial).where(Serial.id.in_(ids))).all()
    if len(serials) != len(ids):
        raise LabelPrintError("Some labels were not found")

    prior_log_counts = dict(
        db.execute(
            select(LabelPrintLog.serial_id, func.sum(LabelPrintLog.copies))
            .where(LabelPrintLog.serial_id.in_(ids))
            .group_by(LabelPrintLog.serial_id)
        ).all()
    )
    previously_printed = {
        serial.id for serial in serials if serial.label_printed_at or prior_log_counts.get(serial.id, 0)
    }
    if not is_admin and previously_printed:
        blocked = [serial.serial_number for serial in serials if serial.id in previously_printed]
        joined = ", ".join(sorted(blocked)[:5])
        suffix = "..." if len(blocked) > 5 else ""
        raise LabelPrintError(f"Print option already used for {joined}{suffix}")

    clean_reason = reason.strip()[:500]
    is_reprint_request = bool(previously_printed or copies > 1)
    if is_admin and is_reprint_request and not clean_reason:
        raise LabelPrintError("A reprint reason is required")
    clean_layout = validate_label_layout(layout)
    printed_at = datetime.now(timezone.utc)

    if not is_admin:
        result = db.execute(
            update(Serial)
            .where(Serial.id.in_(ids), Serial.label_printed_at.is_(None))
            .values(label_printed_at=printed_at, label_printed_by_id=user.id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != len(ids):
            db.rollback()
            raise LabelPrintError("One or more labels were printed by another user. Refresh and try again")
    else:
        db.execute(
            update(Serial)
            .where(Serial.id.in_(ids), Serial.label_printed_at.is_(None))
            .values(label_printed_at=printed_at, label_printed_by_id=user.id)
            .execution_options(synchronize_session=False)
        )

    logs = [
        LabelPrintLog(
            serial_id=serial.id,
            printed_by_id=user.id,
            printed_at=printed_at,
            copies=copies,
            is_reprint=serial.id in previously_printed or copies > 1,
            reason=clean_reason or None,
            template_name=template_name.strip()[:120] or None,
            layout_json=json.dumps(clean_layout, sort_keys=True, separators=(",", ":")),
        )
        for serial in serials
    ]
    db.add_all(logs)
    db.commit()
    return logs


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
