from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_permission
from app.database import get_db
from app.models import Product, Role, StockRelocation, StorageLocation, WarehouseLevel, has_any_role
from app.services.exports import barcode_png, location_labels_pdf
from app.services.relocation import (
    MoveItem,
    RelocationError,
    create_location,
    find_location_by_code,
    relocate_stock,
    search_stock,
    warehouse_map_rows,
)
from app.templates import templates

router = APIRouter(prefix="/warehouse")

RELOCATION_ROLES = {
    Role.SUPER_ADMIN.value,
    Role.ADMIN.value,
    Role.WAREHOUSE_MANAGER.value,
    Role.AUDITOR.value,
}
LOCATION_MANAGER_ROLES = {
    Role.SUPER_ADMIN.value,
    Role.ADMIN.value,
    Role.WAREHOUSE_MANAGER.value,
}


def _require_relocator(request: Request, db: Session):
    user = require_permission(request, db, "stock_relocation", {"edit", "yes"})
    if not has_any_role(user.role, RELOCATION_ROLES):
        raise HTTPException(status_code=403, detail="Not allowed")
    return user


def _require_location_manager(request: Request, db: Session):
    user = require_permission(request, db, "location_manage", {"edit", "yes"})
    if not has_any_role(user.role, LOCATION_MANAGER_ROLES):
        raise HTTPException(status_code=403, detail="Not allowed")
    return user


def _device_used(request: Request) -> str:
    agent = request.headers.get("user-agent", "").strip()
    lowered = agent.lower()
    if "android" in lowered:
        kind = "Android device"
    elif "iphone" in lowered or "ipad" in lowered:
        kind = "iOS device"
    elif "windows" in lowered:
        kind = "Windows device"
    elif "macintosh" in lowered:
        kind = "Mac device"
    elif "linux" in lowered:
        kind = "Linux device"
    else:
        kind = "Browser device"
    return f"{kind} · {agent}"[:240] if agent else kind


def _active_locations(db: Session) -> list[StorageLocation]:
    return db.scalars(
        select(StorageLocation)
        .where(StorageLocation.active.is_(True))
        .order_by(
            StorageLocation.warehouse,
            StorageLocation.zone,
            StorageLocation.section,
            StorageLocation.rack,
            StorageLocation.shelf,
            StorageLocation.bin,
        )
    ).all()


@router.get("/move")
def move_stock_page(request: Request, db: Session = Depends(get_db)):
    user = _require_relocator(request, db)
    locations = _active_locations(db)
    return templates.TemplateResponse(
        request,
        "warehouse_move.html",
        {
            "request": request,
            "user": user,
            "locations": locations,
            "locations_data": [
                [
                    location.id,
                    location.code,
                    location.warehouse,
                    location.zone,
                    location.section,
                    location.rack,
                    location.shelf,
                    location.bin,
                    location.full_path,
                ]
                for location in locations
            ],
        },
    )


@router.get("/api/search")
def move_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    _require_relocator(request, db)
    return JSONResponse({"ok": True, "results": search_stock(db, q)})


@router.get("/api/location")
def location_lookup(request: Request, q: str, db: Session = Depends(get_db)):
    _require_relocator(request, db)
    location = find_location_by_code(db, q)
    if not location:
        return JSONResponse({"ok": False, "error": "Location code is invalid or inactive"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "location": {
                "id": location.id,
                "code": location.code,
                "warehouse": location.warehouse,
                "zone": location.zone,
                "section": location.section,
                "rack": location.rack,
                "shelf": location.shelf,
                "bin": location.bin,
                "path": location.full_path,
            },
        }
    )


@router.post("/relocate")
async def confirm_relocation(request: Request, db: Session = Depends(get_db)):
    user = _require_relocator(request, db)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    items: list[MoveItem] = []
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                items.append(
                    MoveItem(
                        product_id=int(raw.get("product_id")),
                        quantity=int(raw.get("quantity")),
                        product_batch_number=str(raw["batch_number"]).strip()
                        if raw.get("batch_number")
                        else None,
                        source_location_id=int(raw["source_location_id"])
                        if raw.get("source_location_id") is not None
                        else None,
                        legacy_warehouse=str(raw["legacy_warehouse"]).strip()
                        if raw.get("legacy_warehouse")
                        else None,
                        serial_id=int(raw["serial_id"]) if raw.get("serial_id") is not None else None,
                    )
                )
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "Invalid move item"}, status_code=400)
    try:
        destination_id = int(payload.get("destination_id"))
    except (AttributeError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Select a destination location"}, status_code=400)
    try:
        rows = relocate_stock(
            db,
            user=user,
            destination_id=destination_id,
            items=items,
            reason=str(payload.get("reason", "")),
            device_used=_device_used(request),
        )
    except RelocationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse(
        {
            "ok": True,
            "message": f"Moved {sum(row.quantity for row in rows)} unit(s) successfully",
            "references": [row.reference_number for row in rows],
        }
    )


@router.get("/history")
def relocation_history(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "warehouse_data")
    query = (
        select(StockRelocation)
        .join(Product)
        .order_by(desc(StockRelocation.created_at))
        .limit(300)
        .options(
            selectinload(StockRelocation.product),
            selectinload(StockRelocation.user),
            selectinload(StockRelocation.serial_links),
        )
    )
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                StockRelocation.reference_number.ilike(like),
                Product.product_code.ilike(like),
                Product.product_name.ilike(like),
                StockRelocation.product_batch_number.ilike(like),
            )
        )
    rows = db.scalars(query).all()
    return templates.TemplateResponse(
        request,
        "warehouse_history.html",
        {"request": request, "user": user, "relocations": rows, "q": q},
    )


@router.get("/map")
def warehouse_map(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "warehouse_data")
    rows = warehouse_map_rows(db)
    return templates.TemplateResponse(
        request,
        "warehouse_map.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "total_locations": len(rows),
            "active_locations": sum(1 for row in rows if row["location"].active),
            "total_units": sum(int(row["quantity"]) for row in rows),
        },
    )


@router.get("/locations")
def locations_page(
    request: Request,
    error: str = "",
    created: int | None = None,
    db: Session = Depends(get_db),
):
    user = _require_location_manager(request, db)
    rows = db.scalars(
        select(StorageLocation).order_by(
            StorageLocation.warehouse,
            StorageLocation.zone,
            StorageLocation.section,
            StorageLocation.rack,
            StorageLocation.shelf,
            StorageLocation.bin,
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "warehouse_locations.html",
        {
            "request": request,
            "user": user,
            "locations": rows,
            "created_location": next((row for row in rows if row.id == created), None),
            "warehouse_levels": [level.value for level in WarehouseLevel],
            "error": error or None,
        },
    )


@router.post("/locations")
def add_location(
    request: Request,
    code: str = Form(...),
    warehouse: str = Form(...),
    warehouse_level: str = Form(WarehouseLevel.COMPANY_WAREHOUSE.value),
    zone: str = Form(...),
    section: str = Form(...),
    rack: str = Form(...),
    shelf: str = Form(...),
    bin_name: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_location_manager(request, db)
    try:
        location = create_location(
            db,
            code=code,
            warehouse=warehouse,
            warehouse_level=warehouse_level,
            zone=zone,
            section=section,
            rack=rack,
            shelf=shelf,
            bin_name=bin_name,
        )
    except RelocationError as exc:
        return RedirectResponse(f"/warehouse/locations?{urlencode({'error': str(exc)})}", status_code=303)
    return RedirectResponse(f"/warehouse/locations?created={location.id}", status_code=303)


@router.post("/locations/{location_id}/toggle")
def toggle_location(location_id: int, request: Request, db: Session = Depends(get_db)):
    _require_location_manager(request, db)
    location = db.get(StorageLocation, location_id)
    if location:
        location.active = not location.active
        db.commit()
    return RedirectResponse("/warehouse/locations", status_code=303)


@router.get("/locations/{location_id}/qr.png")
def location_qr(location_id: int, request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "warehouse_data")
    location = db.get(StorageLocation, location_id)
    if not location:
        raise HTTPException(status_code=404)
    return Response(
        barcode_png(location.code),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{location.code}-qr.png"'},
    )


@router.get("/locations/labels.pdf")
def location_labels(
    request: Request,
    ids: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "warehouse_data")
    parsed_ids = list(dict.fromkeys(int(value) for value in ids.split(",") if value.strip().isdigit()))
    query = select(StorageLocation)
    if parsed_ids:
        query = query.where(StorageLocation.id.in_(parsed_ids))
    else:
        query = query.where(StorageLocation.active.is_(True))
    locations = db.scalars(
        query.order_by(
            StorageLocation.warehouse,
            StorageLocation.zone,
            StorageLocation.section,
            StorageLocation.rack,
            StorageLocation.shelf,
            StorageLocation.bin,
        )
    ).all()
    return Response(
        location_labels_pdf(locations),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=setuora-location-qr-labels.pdf"},
    )
