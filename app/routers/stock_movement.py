from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.auth import require_permission
from app.database import get_db
from app.models import Product, Serial
from app.services.change_audit import record_change
from app.services.settings import get_all_settings, update_settings
from app.services.stock_movement import (
    FRANCHISE_LEVELS,
    MovementConfig,
    MovementFilters,
    movement_config,
    stock_movement_pdf,
    stock_movement_rows,
    stock_movement_xlsx,
    validate_movement_config,
)
from app.templates import templates

router = APIRouter(prefix="/stock-movement")


def _parse_date(value: str, field_name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} date.") from exc


def _filters(
    *,
    product_id: int | None,
    warehouse: str,
    category: str,
    brand: str,
    batch: str,
    franchise_level: str,
    expiry_period: str,
    movement: str,
    start: str,
    end: str,
) -> MovementFilters:
    return MovementFilters(
        product_id=product_id,
        warehouse=warehouse.strip(),
        category=category.strip(),
        brand=brand.strip(),
        batch=batch.strip(),
        franchise_level=franchise_level.strip(),
        expiry_period=expiry_period.strip(),
        movement=movement.strip(),
        start=_parse_date(start, "start"),
        end=_parse_date(end, "end"),
    )


def _filter_options(db: Session) -> dict[str, object]:
    products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.product_code)).all()
    warehouse_values = db.scalars(select(distinct(Serial.warehouse)).order_by(Serial.warehouse)).all()
    warehouses = sorted({value or "Main Warehouse" for value in warehouse_values})
    batches = [
        value
        for value in db.scalars(
            select(distinct(Serial.product_batch_number))
            .where(Serial.product_batch_number.is_not(None))
            .order_by(Serial.product_batch_number)
        ).all()
        if value
    ]
    return {
        "products": products,
        "categories": sorted({product.category for product in products if product.category}),
        "brands": sorted({product.brand for product in products if product.brand}),
        "warehouses": warehouses,
        "batches": batches,
        "franchise_levels": FRANCHISE_LEVELS,
    }


def _query_string(
    product_id: int | None,
    warehouse: str,
    category: str,
    brand: str,
    batch: str,
    franchise_level: str,
    expiry_period: str,
    movement: str,
    start: str,
    end: str,
) -> str:
    values = {
        "product_id": product_id or "",
        "warehouse": warehouse,
        "category": category,
        "brand": brand,
        "batch": batch,
        "franchise_level": franchise_level,
        "expiry_period": expiry_period,
        "movement": movement,
        "start": start,
        "end": end,
    }
    return urlencode({key: value for key, value in values.items() if value not in {"", None}})


@router.get("")
def stock_movement_page(
    request: Request,
    product_id: int | None = None,
    warehouse: str = "",
    category: str = "",
    brand: str = "",
    batch: str = "",
    franchise_level: str = "",
    expiry_period: str = "",
    movement: str = "",
    start: str = "",
    end: str = "",
    saved: str = "",
    config_error: str = "",
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "stock_movement_data")
    config = movement_config(db)
    error = config_error or None
    invalid_filters = False
    try:
        filters = _filters(
            product_id=product_id,
            warehouse=warehouse,
            category=category,
            brand=brand,
            batch=batch,
            franchise_level=franchise_level,
            expiry_period=expiry_period,
            movement=movement,
            start=start,
            end=end,
        )
        rows, summary = stock_movement_rows(db, config=config, filters=filters)
    except ValueError as exc:
        error = str(exc)
        invalid_filters = True
        filters = MovementFilters()
        rows, summary = stock_movement_rows(db, config=config, filters=filters)
    return templates.TemplateResponse(
        request,
        "stock_movement.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "summary": summary,
            "config": config,
            "filters": filters,
            "filter_options": _filter_options(db),
            "export_query": _query_string(
                product_id,
                warehouse,
                category,
                brand,
                batch,
                franchise_level,
                expiry_period,
                movement,
                start,
                end,
            ),
            "error": error,
            "saved": saved == "1",
        },
        status_code=400 if invalid_filters else 200,
    )


@router.post("/settings")
def save_stock_movement_settings(
    request: Request,
    analysis_days: int = Form(...),
    dead_below_pct: float = Form(...),
    slow_below_pct: float = Form(...),
    medium_up_to_pct: float = Form(...),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "settings_edit")
    before = get_all_settings(db)
    config = MovementConfig(
        analysis_days=analysis_days,
        dead_below_pct=dead_below_pct,
        slow_below_pct=slow_below_pct,
        medium_up_to_pct=medium_up_to_pct,
    )
    try:
        validate_movement_config(config)
    except ValueError as exc:
        return RedirectResponse(
            f"/stock-movement?{urlencode({'config_error': str(exc)})}",
            status_code=303,
        )
    values = {
        "movement_analysis_days": str(config.analysis_days),
        "movement_dead_below_pct": str(config.dead_below_pct),
        "movement_slow_below_pct": str(config.slow_below_pct),
        "movement_medium_up_to_pct": str(config.medium_up_to_pct),
    }
    update_settings(db, values, commit=False)
    record_change(
        db,
        user,
        entity_type="settings",
        entity_id="stock_movement",
        action="update",
        before={key: before.get(key, "") for key in values},
        after=values,
    )
    db.commit()
    return RedirectResponse("/stock-movement?saved=1", status_code=303)


def _export_rows(
    db: Session,
    *,
    product_id: int | None,
    warehouse: str,
    category: str,
    brand: str,
    batch: str,
    franchise_level: str,
    expiry_period: str,
    movement: str,
    start: str,
    end: str,
):
    try:
        filters = _filters(
            product_id=product_id,
            warehouse=warehouse,
            category=category,
            brand=brand,
            batch=batch,
            franchise_level=franchise_level,
            expiry_period=expiry_period,
            movement=movement,
            start=start,
            end=end,
        )
        return stock_movement_rows(db, config=movement_config(db), filters=filters)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/export.xlsx")
def stock_movement_excel(
    request: Request,
    product_id: int | None = None,
    warehouse: str = "",
    category: str = "",
    brand: str = "",
    batch: str = "",
    franchise_level: str = "",
    expiry_period: str = "",
    movement: str = "",
    start: str = "",
    end: str = "",
    fields: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "stock_movement_export")
    rows, summary = _export_rows(
        db,
        product_id=product_id,
        warehouse=warehouse,
        category=category,
        brand=brand,
        batch=batch,
        franchise_level=franchise_level,
        expiry_period=expiry_period,
        movement=movement,
        start=start,
        end=end,
    )
    return Response(
        stock_movement_xlsx(rows, summary, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=setuora-stock-movement.xlsx"},
    )


@router.get("/export.pdf")
def stock_movement_pdf_export(
    request: Request,
    product_id: int | None = None,
    warehouse: str = "",
    category: str = "",
    brand: str = "",
    batch: str = "",
    franchise_level: str = "",
    expiry_period: str = "",
    movement: str = "",
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "stock_movement_export")
    rows, summary = _export_rows(
        db,
        product_id=product_id,
        warehouse=warehouse,
        category=category,
        brand=brand,
        batch=batch,
        franchise_level=franchise_level,
        expiry_period=expiry_period,
        movement=movement,
        start=start,
        end=end,
    )
    return Response(
        stock_movement_pdf(rows, summary),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=setuora-stock-movement.pdf"},
    )
