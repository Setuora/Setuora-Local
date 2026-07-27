from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.auth import require_permission
from app.database import get_db
from app.models import ScanLog, TransactionType
from app.services.inventory import InventoryError
from app.services.replacement import replace_barcode_serial
from app.templates import templates

router = APIRouter()


@router.get("/qr-replacement")
@router.get("/barcode-replacement")
def replacement_page(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "barcode_replacement")
    logs = db.scalars(
        select(ScanLog).where(ScanLog.action == TransactionType.QR_REPLACEMENT.value).order_by(desc(ScanLog.created_at)).limit(40)
    ).all()
    return templates.TemplateResponse(
        request,
        "qr_replacement.html",
        {"request": request, "user": user, "logs": logs, "error": None, "replacement": None},
    )


@router.post("/qr-replacement")
@router.post("/barcode-replacement")
def replace_qr(
    request: Request,
    old_serial_number: str = Form(...),
    new_serial_number: str = Form(""),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "barcode_replacement")
    logs = db.scalars(
        select(ScanLog).where(ScanLog.action == TransactionType.QR_REPLACEMENT.value).order_by(desc(ScanLog.created_at)).limit(40)
    ).all()
    try:
        replacement = replace_barcode_serial(db, user, old_serial_number, new_serial_number or None, reason)
    except InventoryError as exc:
        return templates.TemplateResponse(
            request,
            "qr_replacement.html",
            {"request": request, "user": user, "logs": logs, "error": str(exc), "replacement": None},
            status_code=400,
        )
    logs = db.scalars(
        select(ScanLog).where(ScanLog.action == TransactionType.QR_REPLACEMENT.value).order_by(desc(ScanLog.created_at)).limit(40)
    ).all()
    return templates.TemplateResponse(
        request,
        "qr_replacement.html",
        {"request": request, "user": user, "logs": logs, "error": None, "replacement": replacement},
    )
