from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import require_permission, require_user
from app.database import get_db
from app.models import InventoryTransaction, Product, Role, Serial, SerialStatus, User, WarehouseLevel
from app.services.assignment import AssignmentLine, assign_barcodes_to_existing_stock
from app.services.access_control import configured_role_has_access
from app.services.change_audit import record_change
from app.services.expiry import parse_optional_date
from app.services.inventory import InventoryError
from app.services.product_permissions import require_product_qr_generation, user_can_manage_purchase_qr_permission
from app.services.stock_movement import (
    product_inventory_metrics,
    product_sales_report_pdf,
    product_sales_transactions,
)
from app.templates import templates

router = APIRouter(prefix="/products")


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def product_snapshot(product: Product) -> dict[str, object]:
    return {
        "id": product.id,
        "product_code": product.product_code,
        "product_name": product.product_name,
        "nickname": product.nickname,
        "category": product.category,
        "brand": product.brand,
        "hsn": product.hsn,
        "gst_rate": product.gst_rate,
        "unit": product.unit,
        "default_rate": product.default_rate,
        "sales_discount_rate": product.sales_discount_rate,
        "shelf_verification_interval": product.shelf_verification_interval,
        "purchase_qr_print_allowed": product.purchase_qr_print_allowed,
        "tally_stock_item_name": product.tally_stock_item_name,
        "alternate_tally_stock_item_name": product.alternate_tally_stock_item_name,
        "active": product.active,
    }


def user_can_open_product_generate(user: User) -> bool:
    config = getattr(user, "_access_config", {})
    return configured_role_has_access(config, user.role, "product_create", {"edit"}) or configured_role_has_access(
        config,
        user.role,
        "barcode_assignment",
        {"edit"},
    )


def product_page_context(db: Session, user: User, rows: list[Product], q: str, error: str | None = None) -> dict:
    inventory_metrics, analysis_days = product_inventory_metrics(db, rows)
    sales = product_sales_transactions(db, {product.id for product in rows}, analysis_days)
    sales_by_product: dict[int, list[InventoryTransaction]] = {product.id: [] for product in rows}
    for sale in sales:
        if sale.product_id in sales_by_product:
            sales_by_product[sale.product_id].append(sale)
    return {
        "user": user,
        "products": rows,
        "inventory_metrics": inventory_metrics,
        "sales_by_product": sales_by_product,
        "analysis_days": analysis_days,
        "warehouse_levels": [level.value for level in WarehouseLevel],
        "q": q,
        "error": error,
    }


@router.get("")
def products(request: Request, q: str = "", error: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "product_master")
    error_message = {
        "serial_generation_failed": "Barcode generation failed",
        "default_rate_invalid": "Default rate cannot be negative",
        "sales_discount_invalid": "Sales discount must be between 0 and 100%",
        "shelf_interval_invalid": "Shelf verification interval must be between 1 and 1000 scans",
        "hsn_invalid": "HSN cannot be blank",
        "gst_rate_invalid": "GST rate must be between 0 and 100%",
        "product_delete_blocked": "Product has serials or transaction history and cannot be deleted",
    }.get(error, error)
    query = select(Product).order_by(Product.product_code)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Product.product_code.ilike(like),
                Product.product_name.ilike(like),
                Product.nickname.ilike(like),
                Product.tally_stock_item_name.ilike(like),
                Product.alternate_tally_stock_item_name.ilike(like),
                Product.hsn.ilike(like),
                Product.brand.ilike(like),
            )
        )
    rows = db.scalars(query).all()
    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "request": request,
            **product_page_context(db, user, rows, q, error_message or None),
        },
    )


@router.get("/{product_id}/sales-report.pdf")
def product_sales_pdf(request: Request, product_id: int, db: Session = Depends(get_db)):
    require_permission(request, db, "product_master")
    product = db.get(Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=303)
    metrics, analysis_days = product_inventory_metrics(db, [product])
    sales = product_sales_transactions(db, {product.id}, analysis_days)
    safe_code = "".join(character for character in product.product_code if character.isalnum() or character in "-_")
    return Response(
        product_sales_report_pdf(product, metrics[product.id], sales, analysis_days),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"attachment; filename={safe_code or f'product-{product.id}'}-sales-report.pdf"
            )
        },
    )


@router.post("")
def create_product(
    request: Request,
    product_code: str = Form(...),
    product_name: str = Form(...),
    nickname: str = Form(""),
    category: str = Form(""),
    brand: str = Form(""),
    hsn: str = Form(...),
    gst_rate: float = Form(...),
    unit: str = Form("Pcs"),
    default_rate: float = Form(0),
    sales_discount_rate: float = Form(0),
    shelf_verification_interval: int = Form(1),
    purchase_qr_print_allowed: bool = Form(False),
    tally_stock_item_name: str = Form(""),
    alternate_tally_stock_item_name: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "product_create")
    if sales_discount_rate < 0 or sales_discount_rate > 100:
        rows = db.scalars(select(Product).order_by(Product.product_code)).all()
        return templates.TemplateResponse(
            request,
            "products.html",
            {
                "request": request,
                **product_page_context(db, user, rows, "", "Sales discount must be between 0 and 100%"),
            },
            status_code=400,
        )
    if shelf_verification_interval < 1 or shelf_verification_interval > 1000:
        rows = db.scalars(select(Product).order_by(Product.product_code)).all()
        return templates.TemplateResponse(
            request,
            "products.html",
            {
                "request": request,
                **product_page_context(db, user, rows, "", "Shelf verification interval must be between 1 and 1000 scans"),
            },
            status_code=400,
        )
    product = Product(
        product_code=product_code.strip().upper(),
        product_name=product_name.strip(),
        nickname=nickname.strip() or None,
        category=category.strip() or None,
        brand=brand.strip() or None,
        hsn=hsn.strip(),
        gst_rate=gst_rate,
        unit=unit.strip() or "Pcs",
        default_rate=default_rate,
        sales_discount_rate=sales_discount_rate,
        shelf_verification_interval=shelf_verification_interval,
        purchase_qr_print_allowed=purchase_qr_print_allowed if user_can_manage_purchase_qr_permission(user) else False,
        tally_stock_item_name=tally_stock_item_name.strip() or product_name.strip(),
        alternate_tally_stock_item_name=alternate_tally_stock_item_name.strip() or None,
    )
    db.add(product)
    try:
        db.flush()
        record_change(
            db,
            user,
            entity_type="product",
            entity_id=product.id,
            action="create",
            before=None,
            after=product_snapshot(product),
        )
        db.commit()
    except Exception:
        db.rollback()
        rows = db.scalars(select(Product).order_by(Product.product_code)).all()
        return templates.TemplateResponse(
            request,
            "products.html",
            {
                "request": request,
                **product_page_context(db, user, rows, "", "Product code already exists"),
            },
            status_code=400,
        )
    return RedirectResponse("/products", status_code=303)


@router.get("/{product_id}/name", operation_id="product_name_legacy_redirect_get")
@router.post("/{product_id}/name", operation_id="product_name_legacy_redirect_post")
def product_name_legacy_redirect(request: Request, product_id: int, db: Session = Depends(get_db)):
    require_permission(request, db, "product_master")
    return RedirectResponse("/products", status_code=303)


@router.post("/{product_id}/delete")
def delete_product(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
):
    user = require_user(request, db, {Role.SUPER_ADMIN})
    product = db.get(Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=303)

    serial_count = db.scalar(select(func.count(Serial.id)).where(Serial.product_id == product.id)) or 0
    transaction_count = db.scalar(select(func.count(InventoryTransaction.id)).where(InventoryTransaction.product_id == product.id)) or 0
    if serial_count or transaction_count:
        return RedirectResponse("/products?error=product_delete_blocked", status_code=303)

    before = product_snapshot(product)
    record_change(
        db,
        user,
        entity_type="product",
        entity_id=product.id,
        action="delete",
        before=before,
        after=None,
    )
    db.delete(product)
    db.commit()
    return RedirectResponse("/products", status_code=303)


@router.post("/{product_id}/pricing")
def update_product_pricing(
    request: Request,
    product_id: int,
    default_rate: float = Form(0),
    sales_discount_rate: float = Form(0),
    shelf_verification_interval: int | None = Form(None),
    purchase_qr_print_allowed: bool = Form(False),
    nickname: str | None = Form(None),
    category: str | None = Form(None),
    brand: str | None = Form(None),
    hsn: str | None = Form(None),
    gst_rate: float | None = Form(None),
    tally_stock_item_name: str | None = Form(None),
    alternate_tally_stock_item_name: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "product_create")
    product = db.get(Product, product_id)
    if not product:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "Product not found"}, status_code=404)
        return RedirectResponse("/products", status_code=303)
    if default_rate < 0:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "Default rate cannot be negative"}, status_code=400)
        return RedirectResponse("/products?error=default_rate_invalid", status_code=303)
    if sales_discount_rate < 0 or sales_discount_rate > 100:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "Sales discount must be between 0 and 100%"}, status_code=400)
        return RedirectResponse("/products?error=sales_discount_invalid", status_code=303)
    if shelf_verification_interval is not None and (
        shelf_verification_interval < 1 or shelf_verification_interval > 1000
    ):
        if wants_json(request):
            return JSONResponse(
                {"ok": False, "error": "Shelf verification interval must be between 1 and 1000 scans"},
                status_code=400,
            )
        return RedirectResponse("/products?error=shelf_interval_invalid", status_code=303)
    if hsn is not None and not hsn.strip():
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "HSN cannot be blank"}, status_code=400)
        return RedirectResponse("/products?error=hsn_invalid", status_code=303)
    if gst_rate is not None and (gst_rate < 0 or gst_rate > 100):
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "GST rate must be between 0 and 100%"}, status_code=400)
        return RedirectResponse("/products?error=gst_rate_invalid", status_code=303)
    before = product_snapshot(product)
    product.default_rate = default_rate
    product.sales_discount_rate = sales_discount_rate
    if shelf_verification_interval is not None:
        product.shelf_verification_interval = shelf_verification_interval
    elif not product.shelf_verification_interval or product.shelf_verification_interval < 1:
        product.shelf_verification_interval = 1
    if user_can_manage_purchase_qr_permission(user):
        product.purchase_qr_print_allowed = purchase_qr_print_allowed
    if nickname is not None:
        product.nickname = nickname.strip() or None
    if category is not None:
        product.category = category.strip() or None
    if brand is not None:
        product.brand = brand.strip() or None
    if hsn is not None:
        product.hsn = hsn.strip()
    if gst_rate is not None:
        product.gst_rate = gst_rate
    if tally_stock_item_name is not None:
        product.tally_stock_item_name = tally_stock_item_name.strip() or product.product_name
    if alternate_tally_stock_item_name is not None:
        product.alternate_tally_stock_item_name = alternate_tally_stock_item_name.strip() or None
    record_change(
        db,
        user,
        entity_type="product",
        entity_id=product.id,
        action="update",
        before=before,
        after=product_snapshot(product),
    )
    db.commit()
    db.refresh(product)
    if wants_json(request):
        return JSONResponse(
            {
                "ok": True,
                "product": {
                    "id": product.id,
                    "nickname": product.nickname or "",
                    "category": product.category or "",
                    "brand": product.brand or "",
                    "hsn": product.hsn,
                    "gst_rate": float(product.gst_rate or 0),
                    "default_rate": float(product.default_rate or 0),
                    "sales_discount_rate": float(product.sales_discount_rate or 0),
                    "shelf_verification_interval": int(product.shelf_verification_interval),
                    "purchase_qr_print_allowed": bool(product.purchase_qr_print_allowed),
                    "tally_stock_item_name": product.tally_stock_item_name,
                    "alternate_tally_stock_item_name": product.alternate_tally_stock_item_name or "",
                },
            }
        )
    return RedirectResponse("/products", status_code=303)


@router.post("/{product_id}/generate")
def generate_product_serials(
    request: Request,
    product_id: int,
    quantity: int = Form(...),
    prefix: str = Form(""),
    initial_status: str = Form(SerialStatus.GENERATED.value),
    product_batch_number: str = Form(""),
    mfg_date: str = Form(""),
    expiry_date: str = Form(""),
    warehouse: str = Form(""),
    warehouse_level: str = Form(WarehouseLevel.COMPANY_WAREHOUSE.value),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if not user_can_open_product_generate(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    product = db.get(Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=303)
    require_product_qr_generation(user, product)
    try:
        parsed_status = SerialStatus(initial_status)
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
                    prefix=prefix or None,
                    product_batch_number=product_batch_number.strip() or None,
                    mfg_date=parsed_mfg_date,
                    expiry_date=parsed_expiry_date,
                    warehouse=warehouse.strip() or None,
                    warehouse_level=warehouse_level,
                )
            ],
            source="MANUAL" if parsed_status == SerialStatus.IN_STOCK else "GENERATED",
            initial_status=parsed_status,
        )
    except (InventoryError, ValueError):
        return RedirectResponse("/products?error=serial_generation_failed", status_code=303)
    return RedirectResponse(f"/barcode-assignment/{batch.id}", status_code=303)
