from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_permission
from app.database import get_db
from app.models import Batch, BatchItem, BatchType, Product, Serial, SerialStatus, WarehouseLevel
from app.services.assignment import AssignmentLine, assign_barcodes_to_existing_stock, parse_bulk_assignment_xlsx
from app.services.exports import DEFAULT_LABEL_COLUMNS, DEFAULT_LABEL_ROWS, barcode_labels_pdf, label_layout, serials_xlsx
from app.services.expiry import parse_optional_date
from app.services.inventory import InventoryError
from app.services.product_permissions import (
    user_can_generate_product_qr,
    user_can_manage_purchase_qr_permission,
)
from app.templates import templates

router = APIRouter(prefix="/barcode-assignment")
MAX_BULK_ASSIGNMENT_UPLOAD_BYTES = 5 * 1024 * 1024


def _assignment_batch(db: Session, batch_id: int) -> Batch | None:
    return db.scalar(
        select(Batch)
        .where(Batch.id == batch_id, Batch.batch_type == BatchType.QR_ASSIGNMENT.value)
        .options(
            selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product),
            selectinload(Batch.user),
        )
    )


@router.get("")
def assignment_page(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "barcode_assignment")
    products = _assignment_products(db, user)
    batches = _recent_assignment_batches(db, user)
    return templates.TemplateResponse(
        request,
        "barcode_assignment.html",
        {
            "request": request,
            "user": user,
            "products": products,
            "batches": batches,
            "warehouse_levels": [level.value for level in WarehouseLevel],
            "error": None,
        },
    )


@router.post("/generate")
def generate_assignment(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(...),
    prefix: str = Form(""),
    initial_status: str = Form(SerialStatus.IN_STOCK.value),
    product_batch_number: str = Form(""),
    mfg_date: str = Form(""),
    expiry_date: str = Form(""),
    warehouse: str = Form(""),
    warehouse_level: str = Form(WarehouseLevel.COMPANY_WAREHOUSE.value),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "barcode_assignment")
    product = db.get(Product, product_id)
    if not product:
        return _assignment_error(request, db, user, "Product not found")
    if not user_can_generate_product_qr(user, product):
        return _assignment_error(request, db, user, "Purchase QR printing is not enabled for this product")
    try:
        parsed_status = SerialStatus(initial_status)
        if parsed_status not in {SerialStatus.GENERATED, SerialStatus.IN_STOCK}:
            raise InventoryError("Choose either purchase/future stock or existing stock")
        parsed_mfg_date = parse_optional_date(mfg_date)
        parsed_expiry_date = parse_optional_date(expiry_date)
        if parsed_mfg_date and parsed_expiry_date and parsed_expiry_date <= parsed_mfg_date:
            raise InventoryError("Expiry date must be after mfg date")
        batch = assign_barcodes_to_existing_stock(
            db,
            user,
            [
                AssignmentLine(
                    product=product,
                    quantity=quantity,
                    prefix=prefix.strip() or None,
                    product_batch_number=product_batch_number.strip() or None,
                    mfg_date=parsed_mfg_date,
                    expiry_date=parsed_expiry_date,
                    warehouse=warehouse.strip() or None,
                    warehouse_level=warehouse_level,
                )
            ],
            notes=notes,
            source="GENERATED" if parsed_status == SerialStatus.GENERATED else "MANUAL",
            initial_status=parsed_status,
        )
    except (InventoryError, ValueError) as exc:
        return _assignment_error(request, db, user, str(exc))
    return RedirectResponse(f"/barcode-assignment/{batch.id}", status_code=303)


@router.post("/bulk")
def bulk_assignment(
    request: Request,
    upload: UploadFile = File(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "barcode_assignment")
    try:
        data = upload.file.read(MAX_BULK_ASSIGNMENT_UPLOAD_BYTES + 1)
        if len(data) > MAX_BULK_ASSIGNMENT_UPLOAD_BYTES:
            raise InventoryError("Upload an Excel file up to 5 MB")
        lines = parse_bulk_assignment_xlsx(
            db,
            data,
            user=user,
            allow_product_create=user_can_manage_purchase_qr_permission(user),
        )
        blocked_products = [line.product.product_code for line in lines if not user_can_generate_product_qr(user, line.product)]
        if blocked_products:
            joined = ", ".join(sorted(set(blocked_products))[:5])
            suffix = "..." if len(set(blocked_products)) > 5 else ""
            raise InventoryError(f"Purchase QR printing is not enabled for {joined}{suffix}")
        batch = assign_barcodes_to_existing_stock(db, user, lines, notes=notes, source="BULK_EXCEL")
    except InventoryError as exc:
        db.rollback()
        return _assignment_error(request, db, user, str(exc))
    return RedirectResponse(f"/barcode-assignment/{batch.id}", status_code=303)


@router.get("/{batch_id}")
def assignment_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = require_permission(request, db, "barcode_assignment")
    batch = _assignment_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/barcode-assignment", status_code=303)
    _require_assignment_batch_access(user, batch)
    return templates.TemplateResponse(
        request,
        "barcode_assignment_detail.html",
        {
            "request": request,
            "user": user,
            "batch": batch,
            "label_pdf_rows": DEFAULT_LABEL_ROWS,
            "label_pdf_columns": DEFAULT_LABEL_COLUMNS,
        },
    )


@router.get("/{batch_id}/labels.pdf")
def assignment_labels_pdf(
    request: Request,
    batch_id: int,
    rows_per_page: int = DEFAULT_LABEL_ROWS,
    columns_per_page: int = DEFAULT_LABEL_COLUMNS,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "barcode_assignment")
    batch = _assignment_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/barcode-assignment", status_code=303)
    _require_assignment_batch_access(user, batch)
    rows_per_page, columns_per_page = label_layout(rows_per_page, columns_per_page)
    serials = [item.serial for item in batch.items]
    return Response(
        barcode_labels_pdf(serials, rows_per_page=rows_per_page, columns_per_page=columns_per_page),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_number}-qr-labels.pdf"},
    )


@router.get("/{batch_id}/serials.xlsx")
def assignment_serials_xlsx(
    request: Request,
    batch_id: int,
    fields: str = "",
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "barcode_assignment")
    batch = _assignment_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/barcode-assignment", status_code=303)
    _require_assignment_batch_access(user, batch)
    serials = [item.serial for item in batch.items]
    return Response(
        serials_xlsx(serials, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_number}-barcodes.xlsx"},
    )


def _assignment_error(request: Request, db: Session, user, error: str):
    products = _assignment_products(db, user)
    batches = _recent_assignment_batches(db, user)
    return templates.TemplateResponse(
        request,
        "barcode_assignment.html",
        {
            "request": request,
            "user": user,
            "products": products,
            "batches": batches,
            "warehouse_levels": [level.value for level in WarehouseLevel],
            "error": error,
        },
        status_code=400,
    )


def _recent_assignment_batches(db: Session, user) -> list[Batch]:
    batches = db.scalars(
        select(Batch)
        .where(Batch.batch_type == BatchType.QR_ASSIGNMENT.value)
        .order_by(desc(Batch.created_at))
        .limit(50)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    ).all()
    return [batch for batch in batches if _assignment_batch_allowed(user, batch)][:20]


def _assignment_products(db: Session, user) -> list[Product]:
    products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.product_code)).all()
    return [product for product in products if user_can_generate_product_qr(user, product)]


def _assignment_batch_allowed(user, batch: Batch) -> bool:
    return all(user_can_generate_product_qr(user, item.serial.product) for item in batch.items if item.serial)


def _require_assignment_batch_access(user, batch: Batch) -> None:
    if not _assignment_batch_allowed(user, batch):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
