from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_permission
from app.database import get_db
from app.models import Batch, InventoryTransaction, Product, RelocationSerial, ScanLog, Serial, StockRelocation
from app.services.access_control import role_has_access
from app.services.exports import DEFAULT_LABEL_COLUMNS, DEFAULT_LABEL_ROWS, barcode_labels_pdf, barcode_png, label_layout, serials_xlsx
from app.services.label_printing import LabelPrintError, mark_serial_labels_printed_once
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
    can_manage_labels = role_has_access(db, user.role, "label_files", {"edit"})
    return templates.TemplateResponse(
        request,
        "labels.html",
        {
            "request": request,
            "user": user,
            "serials": rows,
            "printed_serials": printed_serials,
            "can_manage_labels": can_manage_labels,
            "can_print": bool(rows) and can_manage_labels and not printed_serials,
            "label_ids": ",".join(str(serial.id) for serial in rows),
            "label_pdf_rows": DEFAULT_LABEL_ROWS,
            "label_pdf_columns": DEFAULT_LABEL_COLUMNS,
        },
    )


@router.post("/labels/print")
async def mark_labels_printed(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "label_files", {"edit"})
    try:
        payload = await request.json()
    except ValueError:
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
    try:
        mark_serial_labels_printed_once(db, user, serial_ids)
    except LabelPrintError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True})


@router.get("/labels.pdf")
def labels_pdf(
    request: Request,
    ids: str = "",
    rows_per_page: int = DEFAULT_LABEL_ROWS,
    columns_per_page: int = DEFAULT_LABEL_COLUMNS,
    db: Session = Depends(get_db),
):
    require_permission(request, db, "label_files", {"edit"})
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
