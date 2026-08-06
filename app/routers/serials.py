import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import desc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import require_permission
from app.database import get_db
from app.models import (
    Batch,
    InventoryTransaction,
    LabelPrintLog,
    LabelTemplate,
    Product,
    RelocationSerial,
    Role,
    ScanLog,
    Serial,
    StockRelocation,
    has_any_role,
)
from app.services.exports import DEFAULT_LABEL_COLUMNS, DEFAULT_LABEL_ROWS, barcode_labels_pdf, barcode_png, label_layout, serials_xlsx
from app.services.label_printing import (
    DEFAULT_LABEL_LAYOUT,
    LabelPrintError,
    record_serial_label_prints,
    user_is_label_admin,
    validate_label_layout,
)
from app.services.log_fields import barcode_sold_by, invoice_created_by, product_audited_by
from app.templates import templates

router = APIRouter(prefix="/serials")


def _parse_ids(ids: str) -> list[int]:
    return list(dict.fromkeys(int(value) for value in ids.split(",") if value.strip().isdigit()))


@router.get("")
def serials(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "serial_data")
    query = select(Serial).join(Product).order_by(Serial.created_at.desc()).limit(250)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(Serial.serial_number.ilike(like), Product.product_code.ilike(like), Product.product_name.ilike(like)))
    if status:
        query = query.where(Serial.status == status)
    rows = db.scalars(query).all()
    return templates.TemplateResponse(
        request,
        "serials.html",
        {"request": request, "user": user, "serials": rows, "q": q, "status": status},
    )


@router.get("/{serial_id}/barcode.png")
def serial_barcode(serial_id: int, request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "serial_data")
    serial = db.get(Serial, serial_id)
    if not serial:
        raise HTTPException(status_code=404)
    return Response(barcode_png(serial.serial_number), media_type="image/png")


@router.get("/labels")
def labels(request: Request, ids: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "label_files")
    parsed = _parse_ids(ids)
    rows = db.scalars(
        select(Serial).where(Serial.id.in_(parsed)).order_by(Serial.serial_number).options(selectinload(Serial.product))
    ).all() if parsed else []
    printed_serials = [serial for serial in rows if serial.label_printed_at]
    is_label_admin = user_is_label_admin(user)
    is_purchase_user = has_any_role(user.role, (Role.PURCHASE,))
    can_manage_labels = is_label_admin or is_purchase_user
    print_logs = db.scalars(
        select(LabelPrintLog)
        .where(LabelPrintLog.serial_id.in_(parsed))
        .order_by(LabelPrintLog.printed_at.desc())
        .options(selectinload(LabelPrintLog.serial), selectinload(LabelPrintLog.printed_by))
    ).all() if parsed else []
    saved_templates = db.scalars(
        select(LabelTemplate)
        .where(LabelTemplate.created_by_id == user.id)
        .order_by(LabelTemplate.name)
    ).all()
    return templates.TemplateResponse(
        request,
        "labels.html",
        {
            "request": request,
            "user": user,
            "serials": rows,
            "printed_serials": printed_serials,
            "print_logs": print_logs,
            "can_manage_labels": can_manage_labels,
            "is_label_admin": is_label_admin,
            "can_print": bool(rows) and can_manage_labels and (is_label_admin or not printed_serials),
            "label_ids": ",".join(str(serial.id) for serial in rows),
            "default_label_layout": DEFAULT_LABEL_LAYOUT,
            "saved_label_templates": [
                {"id": template.id, "name": template.name, "settings": template.settings}
                for template in saved_templates
            ],
            "label_pdf_rows": DEFAULT_LABEL_ROWS,
            "label_pdf_columns": DEFAULT_LABEL_COLUMNS,
        },
    )


@router.post("/labels/print")
async def mark_labels_printed(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "label_files")
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    raw_ids = payload.get("ids", [])
    if not isinstance(raw_ids, list):
        raw_ids = []
    serial_ids = []
    for value in raw_ids:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            serial_ids.append(value)
        elif isinstance(value, str) and value.isdigit():
            serial_ids.append(int(value))
    if not serial_ids:
        return JSONResponse({"ok": False, "error": "No labels selected"}, status_code=403)
    raw_copies = payload.get("copies", 1)
    try:
        copies = int(raw_copies) if not isinstance(raw_copies, bool) else 0
    except (TypeError, ValueError):
        copies = 0
    try:
        logs = record_serial_label_prints(
            db,
            user,
            serial_ids,
            copies=copies,
            reason=str(payload.get("reason") or ""),
            template_name=str(payload.get("template_name") or ""),
            layout=payload.get("layout"),
        )
    except LabelPrintError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "labels": len(logs), "copies": copies})


@router.post("/labels/templates")
async def save_label_template(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "label_files")
    if not (user_is_label_admin(user) or has_any_role(user.role, (Role.PURCHASE,))):
        return JSONResponse({"ok": False, "error": "Label printing is unavailable for this role"}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    name = str(payload.get("name") or "").strip()[:120]
    if not name:
        return JSONResponse({"ok": False, "error": "Template name is required"}, status_code=422)
    try:
        layout = validate_label_layout(payload.get("layout"))
    except LabelPrintError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    template = db.scalar(
        select(LabelTemplate).where(
            LabelTemplate.created_by_id == user.id,
            LabelTemplate.name == name,
        )
    )
    if template is None:
        template = LabelTemplate(name=name, created_by_id=user.id, settings_json="{}")
        db.add(template)
    template.settings_json = json.dumps(layout, sort_keys=True, separators=(",", ":"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse({"ok": False, "error": "A template with this name already exists"}, status_code=409)
    db.refresh(template)
    return JSONResponse(
        {
            "ok": True,
            "template": {"id": template.id, "name": template.name, "settings": layout},
        }
    )


@router.delete("/labels/templates/{template_id}")
def delete_label_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "label_files")
    template = db.scalar(
        select(LabelTemplate).where(
            LabelTemplate.id == template_id,
            LabelTemplate.created_by_id == user.id,
        )
    )
    if template is None:
        raise HTTPException(status_code=404)
    db.delete(template)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/labels.pdf")
def labels_pdf(
    request: Request,
    ids: str = "",
    rows_per_page: int = DEFAULT_LABEL_ROWS,
    columns_per_page: int = DEFAULT_LABEL_COLUMNS,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "label_files")
    if not user_is_label_admin(user):
        raise HTTPException(status_code=403, detail="Only admins can download label PDFs")
    parsed = _parse_ids(ids)
    rows = db.scalars(
        select(Serial).where(Serial.id.in_(parsed)).order_by(Serial.serial_number).options(selectinload(Serial.product))
    ).all() if parsed else []
    rows_per_page, columns_per_page = label_layout(rows_per_page, columns_per_page)
    return Response(
        barcode_labels_pdf(rows, rows_per_page=rows_per_page, columns_per_page=columns_per_page),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=setuora-qr-labels.pdf"},
    )


@router.get("/labels.xlsx")
def labels_xlsx(
    request: Request,
    ids: str = "",
    fields: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "label_files")
    parsed = _parse_ids(ids)
    rows = db.scalars(
        select(Serial).where(Serial.id.in_(parsed)).order_by(Serial.serial_number).options(selectinload(Serial.product))
    ).all() if parsed else []
    return Response(
        serials_xlsx(rows, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=setuora-barcodes.xlsx"},
    )


@router.get("/{serial_id}")
def serial_detail(serial_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "serial_data")
    serial = db.scalar(select(Serial).where(Serial.id == serial_id).options(selectinload(Serial.product)))
    if not serial:
        raise HTTPException(status_code=404)
    transactions = db.scalars(
        select(InventoryTransaction)
        .where(InventoryTransaction.serial_id == serial.id)
        .order_by(InventoryTransaction.created_at)
        .options(
            selectinload(InventoryTransaction.user),
            selectinload(InventoryTransaction.batch).selectinload(Batch.user),
            selectinload(InventoryTransaction.product),
        )
    ).all()
    logs = db.scalars(
        select(ScanLog)
        .where(ScanLog.serial_id == serial.id)
        .order_by(desc(ScanLog.created_at))
        .limit(80)
        .options(selectinload(ScanLog.user), selectinload(ScanLog.batch))
    ).all()
    replacement = db.get(Serial, serial.replaced_by_id) if serial.replaced_by_id else None
    relocations = db.scalars(
        select(StockRelocation)
        .join(RelocationSerial, RelocationSerial.relocation_id == StockRelocation.id)
        .where(RelocationSerial.serial_id == serial.id)
        .order_by(desc(StockRelocation.created_at))
        .options(selectinload(StockRelocation.user))
    ).all()
    return templates.TemplateResponse(
        request,
        "serial_detail.html",
        {
            "request": request,
            "user": user,
            "serial": serial,
            "transactions": transactions,
            "logs": logs,
            "replacement": replacement,
            "relocations": relocations,
            "invoice_created_by": invoice_created_by,
            "barcode_sold_by": barcode_sold_by,
            "product_audited_by": product_audited_by,
        },
    )
