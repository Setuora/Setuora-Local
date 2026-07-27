from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import ADMIN_ROLES, require_permission, require_user
from app.database import get_db
from app.models import (
    AuditFinding,
    AuditAssignment,
    Batch,
    BatchItem,
    BatchStatus,
    BatchType,
    GstRegistrationType,
    GstTreatment,
    Product,
    Role,
    Serial,
    SyncAttempt,
    TallyLedgerCache,
    has_any_role,
)
from app.services.audit import reconcile_audit_batch, summarize_audit_findings
from app.services.exports import audit_report_pdf
from app.services.expiry import add_fefo_serials_to_batch, fefo_available_statuses
from app.services.inventory import (
    DEFAULT_UNREGISTERED_SALE_STATE,
    InventoryError,
    add_serial_to_batch,
    apply_batch_statuses,
    create_batch,
    gst_registration_requires_gstin,
    normalize_gst_registration_type,
    normalize_gstin,
    remove_batch_item,
    update_batch_item_rate,
    update_product_rate_in_batch,
)
from app.services.preinvoice import sale_preinvoice_pdf
from app.services.access_control import role_has_access
from app.services.settings import get_active_company, get_all_settings
from app.services.tally_access import allowed_ledger_names, resource_key
from app.services.relocation import find_location_by_code
from app.services.report_format import report_date
from app.services.sale_returns import (
    ensure_sale_scan_allowed,
    sale_return_state,
    scan_sale_return_product,
    validate_sale_returns_complete,
    verify_sale_return_on_shelf,
)
from app.services.shelf_verification import (
    ShelfVerificationError,
    ensure_product_scan_allowed,
    shelf_verification_state,
    verify_pending_items_on_shelf,
)
from app.services.tally import TALLY_XML_SUPPORTED_BATCH_TYPES, TallySyncError, build_voucher_xml, sync_batch
from app.services.tally_excel import (
    MAX_TALLY_EXCEL_UPLOAD_BYTES,
    TALLY_ACCOUNTING_REQUIRED_EXPORT_FIELDS,
    TALLY_EXCEL_EXPORT_BATCH_TYPES,
    TALLY_EXCEL_IMPORT_BATCH_TYPES,
    batch_tally_xlsx,
    import_tally_excel_to_batch,
    tally_accounting_default_deselected_fields,
)
from app.services.voucher import calculate_voucher_summary, validate_priced_batch
from app.templates import templates

router = APIRouter(prefix="/batches")


INDIAN_STATE_OPTIONS = (
    "Andaman and Nicobar Islands",
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chandigarh",
    "Chhattisgarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jammu and Kashmir",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Ladakh",
    "Lakshadweep",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Puducherry",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
)
GST_REGISTRATION_OPTIONS = tuple(
    (
        registration_type.value,
        "Registered" if registration_type == GstRegistrationType.REGULAR else registration_type.value,
    )
    for registration_type in GstRegistrationType
)

BATCH_LIST_SCOPES = {
    "all": {
        "title": "Batches",
        "eyebrow": "Transactions",
        "permission": "batch_list",
        "types": None,
        "empty_message": "No batches yet",
    },
    "purchase": {
        "title": "Purchase batches",
        "eyebrow": "Incoming stock",
        "permission": "purchase_data",
        "types": (BatchType.PURCHASE.value, BatchType.RECEIVE.value),
        "empty_message": "No purchase batches yet",
    },
    "sales": {
        "title": "Sales batches",
        "eyebrow": "Outgoing stock",
        "permission": "sales_data",
        "types": (BatchType.SALE.value,),
        "empty_message": "No sales batches yet",
    },
}


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def action_key_for_batch(batch_type: BatchType) -> str:
    return {
        BatchType.PURCHASE: "batch_purchase",
        BatchType.RECEIVE: "batch_purchase",
        BatchType.SALE: "batch_sale",
        BatchType.AUDIT: "batch_audit",
        BatchType.SALES_RETURN: "batch_sales_return",
        BatchType.PURCHASE_RETURN: "batch_purchase_return",
        BatchType.ISSUE: "batch_issue",
        BatchType.QR_ASSIGNMENT: "barcode_assignment",
    }[batch_type]


def data_key_for_batch(batch_type: BatchType) -> str:
    return {
        BatchType.PURCHASE: "purchase_data",
        BatchType.RECEIVE: "purchase_data",
        BatchType.SALE: "sales_data",
        BatchType.AUDIT: "audit_data",
        BatchType.SALES_RETURN: "sales_data",
        BatchType.PURCHASE_RETURN: "purchase_data",
        BatchType.ISSUE: "issue_data",
        BatchType.QR_ASSIGNMENT: "barcode_assignment",
    }[batch_type]


def can_use_manual_scan(db: Session, user) -> bool:
    return has_any_role(user.role, (Role.ADMIN, Role.SUPER_ADMIN)) and role_has_access(
        db,
        user.role,
        "manual_serial_entry",
        {"edit", "yes"},
    )


def scan_source_allowed(db: Session, user, scan_source: str) -> bool:
    return can_use_manual_scan(db, user) or scan_source == "camera"


def sale_gst_treatment_for_state(party_state: str | None) -> str:
    state = (party_state or "").strip().casefold()
    local_state = DEFAULT_UNREGISTERED_SALE_STATE.casefold()
    if state and state != local_state:
        return GstTreatment.INTER_STATE.value
    return GstTreatment.INTRA_STATE.value


def normalize_sale_gst_treatment(value: str | None, party_state: str | None) -> str:
    treatment = value.strip().upper() if value else ""
    if not treatment:
        return sale_gst_treatment_for_state(party_state)
    valid_treatments = {GstTreatment.INTRA_STATE.value, GstTreatment.INTER_STATE.value}
    if treatment not in valid_treatments:
        raise ValueError("Choose either CGST + SGST or IGST for this sale.")
    return treatment


def fefo_product_options_for_type(db: Session, batch_type: str) -> list[dict[str, object]]:
    statuses = fefo_available_statuses(batch_type)
    if not statuses:
        return []
    available_count = func.count(Serial.id)
    selected_in_draft_batch = (
        select(BatchItem.serial_id)
        .join(Batch, BatchItem.batch_id == Batch.id)
        .where(Batch.status == BatchStatus.DRAFT.value)
    )
    rows = db.execute(
        select(Product, available_count.label("available_quantity"))
        .join(Serial, Serial.product_id == Product.id)
        .where(
            Product.active.is_(True),
            Serial.active.is_(True),
            Serial.status.in_(statuses),
            ~Serial.id.in_(selected_in_draft_batch),
        )
        .group_by(Product.id)
        .having(available_count > 0)
        .order_by(Product.product_code)
    ).all()
    return [
        {
            "id": product.id,
            "product_code": product.product_code,
            "product_name": product.product_name,
            "nickname": product.nickname,
            "tally_stock_item_name": product.tally_stock_item_name,
            "alternate_tally_stock_item_name": product.alternate_tally_stock_item_name,
            "available_quantity": int(available_quantity),
        }
        for product, available_quantity in rows
    ]


def sale_product_options_for_type(db: Session, batch_type: str) -> list[dict[str, object]]:
    statuses = fefo_available_statuses(batch_type)
    if not statuses:
        return []
    selected_in_draft_batch = (
        select(BatchItem.serial_id)
        .join(Batch, BatchItem.batch_id == Batch.id)
        .where(Batch.status == BatchStatus.DRAFT.value)
    )
    available = (
        select(Serial.product_id, func.count(Serial.id).label("available_quantity"))
        .where(
            Serial.active.is_(True),
            Serial.status.in_(statuses),
            ~Serial.id.in_(selected_in_draft_batch),
        )
        .group_by(Serial.product_id)
        .subquery()
    )
    rows = db.execute(
        select(Product, func.coalesce(available.c.available_quantity, 0).label("available_quantity"))
        .outerjoin(available, Product.id == available.c.product_id)
        .where(Product.active.is_(True))
        .order_by(Product.product_code)
    ).all()
    return [
        {
            "id": product.id,
            "product_code": product.product_code,
            "product_name": product.product_name,
            "nickname": product.nickname,
            "tally_stock_item_name": product.tally_stock_item_name,
            "alternate_tally_stock_item_name": product.alternate_tally_stock_item_name,
            "available_quantity": int(available_quantity or 0),
        }
        for product, available_quantity in rows
    ]


def fefo_product_options(db: Session, batch: Batch) -> list[dict[str, object]]:
    return fefo_product_options_for_type(db, batch.batch_type)


def party_ledger_options(db: Session, batch_type: BatchType, user=None) -> list[str]:
    related_types = {
        BatchType.SALE: (BatchType.SALE.value, BatchType.SALES_RETURN.value),
        BatchType.SALES_RETURN: (BatchType.SALE.value, BatchType.SALES_RETURN.value),
        BatchType.PURCHASE: (BatchType.PURCHASE.value, BatchType.RECEIVE.value, BatchType.PURCHASE_RETURN.value),
        BatchType.RECEIVE: (BatchType.PURCHASE.value, BatchType.RECEIVE.value, BatchType.PURCHASE_RETURN.value),
        BatchType.PURCHASE_RETURN: (BatchType.PURCHASE.value, BatchType.RECEIVE.value, BatchType.PURCHASE_RETURN.value),
    }.get(batch_type)
    if not related_types:
        return []
    rows = db.scalars(
        select(Batch.party_name)
        .where(
            Batch.batch_type.in_(related_types),
            Batch.party_name.is_not(None),
            Batch.party_name != "",
        )
        .distinct()
        .order_by(Batch.party_name)
    ).all()
    names = {name.strip() for name in rows if name and name.strip()}

    active_company = get_active_company(db)
    if active_company:
        parent_group = (
            "sundry debtors"
            if batch_type in {BatchType.SALE, BatchType.SALES_RETURN}
            else "sundry creditors"
        )
        cached_rows = db.scalars(
            select(TallyLedgerCache)
            .where(TallyLedgerCache.company_id == active_company.id)
            .order_by(TallyLedgerCache.name)
        ).all()
        names.update(
            row.name.strip()
            for row in cached_rows
            if row.name.strip() and resource_key(row.parent) == parent_group
        )
        if user is not None:
            allowed = allowed_ledger_names(db, user, active_company.id)
            if allowed is not None:
                names = {name for name in names if resource_key(name) in allowed}
    return sorted(names, key=str.casefold)


def party_ledger_is_allowed(db: Session, user, party_name: str) -> bool:
    active_company = get_active_company(db)
    if not active_company:
        return True
    allowed = allowed_ledger_names(db, user, active_company.id)
    return allowed is None or resource_key(party_name) in allowed


def batch_permission_context(db: Session, user, batch: Batch) -> dict[str, bool]:
    action_key = action_key_for_batch(BatchType(batch.batch_type))
    can_edit = role_has_access(db, user.role, action_key)
    can_fefo = can_edit and role_has_access(db, user.role, "fefo_pick", {"edit", "yes"})
    can_tally_xml = role_has_access(db, user.role, "tally_xml", {"edit", "yes"})
    can_tally_excel_export = has_any_role(user.role, ADMIN_ROLES) and role_has_access(
        db,
        user.role,
        "tally_excel_export",
        {"edit", "yes"},
    )
    return {
        "can_edit_batch": can_edit,
        "can_fefo": can_fefo,
        "can_tally_xml": can_tally_xml,
        "can_tally_excel_export": can_tally_excel_export,
        "can_tally_excel_import": can_fefo,
        "can_retry_sync": role_has_access(db, user.role, "tally_sync_retry", {"edit", "yes"}),
        "can_view_attempts": role_has_access(db, user.role, "tally_attempts"),
        "can_view_batch_list": role_has_access(db, user.role, "batch_list"),
    }


def parse_batch_type(value: str) -> BatchType:
    try:
        return BatchType(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid batch type") from exc


def batch_form_context(
    request: Request,
    user,
    batch_type: BatchType,
    *,
    party_name: str = "",
    party_state: str = "",
    party_gst_registration_type: str | None = None,
    party_gst_name: str = "",
    party_gstin: str = "",
    gst_treatment: str = "",
    party_name_options: list[str] | None = None,
    sale_product_options: list[dict[str, object]] | None = None,
    sale_product_id: str = "",
    sale_quantity: str = "1",
    audit_assignments: list[AuditAssignment] | None = None,
    audit_assignment_id: str = "",
    notes: str = "",
    error: str | None = None,
) -> dict[str, object]:
    selected_registration = party_gst_registration_type or GstRegistrationType.UNREGISTERED_CONSUMER.value
    selected_state = party_state
    if batch_type == BatchType.SALE and not selected_state and selected_registration == GstRegistrationType.UNREGISTERED_CONSUMER.value:
        selected_state = DEFAULT_UNREGISTERED_SALE_STATE
    selected_gst_treatment = (
        gst_treatment or sale_gst_treatment_for_state(selected_state)
        if batch_type == BatchType.SALE
        else ""
    )
    return {
        "request": request,
        "user": user,
        "batch_type": batch_type,
        "party_name": party_name,
        "party_state": selected_state,
        "party_gst_registration_type": (
            selected_registration
        ),
        "party_gst_name": party_gst_name,
        "party_gstin": party_gstin,
        "gst_treatment": selected_gst_treatment,
        "party_name_options": party_name_options or [],
        "sale_product_options": sale_product_options or [],
        "sale_product_id": sale_product_id,
        "sale_quantity": sale_quantity,
        "audit_assignments": audit_assignments or [],
        "audit_assignment_id": audit_assignment_id,
        "gst_registration_options": GST_REGISTRATION_OPTIONS,
        "state_options": INDIAN_STATE_OPTIONS,
        "notes": notes,
        "error": error,
    }


def batch_list_rows(db: Session, batch_types: tuple[str, ...] | None = None) -> list[Batch]:
    query = select(Batch).options(selectinload(Batch.items))
    if batch_types:
        query = query.where(Batch.batch_type.in_(batch_types))
    return db.scalars(query.order_by(desc(Batch.created_at)).limit(80)).all()


def open_audit_assignments(db: Session, user) -> list[AuditAssignment]:
    now = datetime.now(timezone.utc)
    return db.scalars(
        select(AuditAssignment)
        .where(
            AuditAssignment.auditor_id == user.id,
            AuditAssignment.starts_at <= now,
            AuditAssignment.ends_at >= now,
        )
        .options(
            selectinload(AuditAssignment.product),
            selectinload(AuditAssignment.batches),
        )
        .order_by(AuditAssignment.ends_at, AuditAssignment.id)
    ).all()


def _money_text(value) -> str:
    return f"{value:.2f}"


def _summary_payload(batch: Batch) -> dict[str, object]:
    summary = calculate_voucher_summary(batch)
    return {
        "lines": [
            {
                "product_id": line.product_id,
                "product_code": line.product_code,
                "product_name": line.product_name,
                "tally_stock_item_name": line.tally_stock_item_name,
                "hsn": line.hsn,
                "quantity": line.quantity,
                "unit": line.unit,
                "rate": _money_text(line.rate),
                "discount_rate": _money_text(line.discount_rate),
                "discount_amount": _money_text(line.discount_amount),
                "taxable_value": _money_text(line.taxable_value),
                "cgst_amount": _money_text(line.cgst_amount),
                "sgst_amount": _money_text(line.sgst_amount),
                "igst_amount": _money_text(line.igst_amount),
                "line_total": _money_text(line.line_total),
            }
            for line in summary.lines
        ],
        "taxable_value": _money_text(summary.taxable_value),
        "cgst_amount": _money_text(summary.cgst_amount),
        "sgst_amount": _money_text(summary.sgst_amount),
        "igst_amount": _money_text(summary.igst_amount),
        "round_off": _money_text(summary.round_off),
        "final_value": _money_text(summary.final_value),
    }


def _batch_items_payload(batch: Batch) -> list[dict[str, object]]:
    shelf_controlled = batch.batch_type in {BatchType.PURCHASE.value, BatchType.RECEIVE.value, BatchType.AUDIT.value}
    return [
        {
            "id": item.id,
            "serial_number": item.serial.serial_number,
            "product_name": item.serial.product.product_name,
            "product_batch_number": item.serial.product_batch_number or "-",
            "expiry_date": report_date(item.serial.expiry_date) if item.serial.expiry_date else "-",
            "fefo_picked": bool(item.fefo_picked),
            "shelf_code": item.shelf_location.code if item.shelf_location else "",
            "shelf_verified_at": report_date(item.shelf_verified_at) if item.shelf_verified_at else "",
            "shelf_pending": bool(
                shelf_controlled
                and item.serial.product.shelf_verification_interval
                and not item.shelf_verified_at
            ),
            "shelf_required": bool(shelf_controlled and item.serial.product.shelf_verification_interval),
            "status": item.serial.display_status,
            "rate": _money_text(item.rate if item.rate is not None else item.serial.product.default_rate),
        }
        for item in batch.items
    ]


def batch_scan_state(db: Session, batch_id: int) -> dict[str, object]:
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(
            selectinload(Batch.items)
            .selectinload(BatchItem.serial)
            .selectinload(Serial.product),
            selectinload(Batch.items).selectinload(BatchItem.shelf_location),
        )
    )
    if not batch:
        return {}
    return {
        "item_count": len(batch.items),
        "summary": _summary_payload(batch),
        "items": _batch_items_payload(batch),
        "sale_return": sale_return_state(db, batch),
    }


def batch_list_response(request: Request, db: Session, scope: str):
    config = BATCH_LIST_SCOPES[scope]
    user = require_permission(request, db, config["permission"])
    return templates.TemplateResponse(
        request,
        "batches.html",
        {
            "request": request,
            "user": user,
            "batches": batch_list_rows(db, config["types"]),
            "batch_scope": scope,
            "page_title": config["title"],
            "page_eyebrow": config["eyebrow"],
            "empty_message": config["empty_message"],
        },
    )


def tally_excel_import_message(request: Request) -> str | None:
    imported = request.query_params.get("excel_imported")
    if not imported:
        return None
    try:
        quantity = int(imported)
    except ValueError:
        return None
    item_label = "item" if quantity == 1 else "items"
    return f"Imported {quantity} {item_label} from Excel."


@router.get("")
def batches(request: Request, db: Session = Depends(get_db)):
    return batch_list_response(request, db, "all")


@router.get("/purchase")
def purchase_batches(request: Request, db: Session = Depends(get_db)):
    return batch_list_response(request, db, "purchase")


@router.get("/sales")
def sales_batches(request: Request, db: Session = Depends(get_db)):
    return batch_list_response(request, db, "sales")


@router.get("/new")
def new_batch(
    request: Request,
    batch_type: str = BatchType.PURCHASE.value,
    audit_assignment_id: str = "",
    db: Session = Depends(get_db),
):
    parsed = parse_batch_type(batch_type)
    user = require_permission(request, db, action_key_for_batch(parsed))
    return templates.TemplateResponse(
        request,
        "batch_new.html",
        batch_form_context(
            request,
            user,
            parsed,
            party_name_options=party_ledger_options(db, parsed, user),
            audit_assignments=open_audit_assignments(db, user) if parsed == BatchType.AUDIT else [],
            audit_assignment_id=audit_assignment_id,
        ),
    )


@router.post("")
def create_batch_route(
    request: Request,
    batch_type: str = Form(...),
    party_name: str = Form(""),
    party_state: str = Form(""),
    party_gst_registration_type: str = Form(""),
    party_gst_name: str = Form(""),
    party_gstin: str = Form(""),
    gst_treatment: str = Form(""),
    reason_code: str = Form(""),
    audit_assignment_id: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    parsed = parse_batch_type(batch_type)
    user = require_permission(request, db, action_key_for_batch(parsed))
    assignment = None
    if parsed == BatchType.AUDIT:
        try:
            assignment_id = int(audit_assignment_id)
        except (TypeError, ValueError):
            assignment_id = 0
        assignment = db.scalar(
            select(AuditAssignment)
            .where(
                AuditAssignment.id == assignment_id,
                AuditAssignment.auditor_id == user.id,
            )
            .options(
                selectinload(AuditAssignment.product),
                selectinload(AuditAssignment.batches),
            )
        )
        now = datetime.now(timezone.utc)
        if (
            not assignment
            or as_utc(assignment.starts_at) > now
            or as_utc(assignment.ends_at) < now
        ):
            return templates.TemplateResponse(
                request,
                "batch_new.html",
                batch_form_context(
                    request,
                    user,
                    parsed,
                    audit_assignments=open_audit_assignments(db, user),
                    audit_assignment_id=audit_assignment_id,
                    notes=notes,
                    error="Choose one of your active audit assignments.",
                ),
                status_code=400,
            )
        if any(batch.status == BatchStatus.DRAFT.value for batch in assignment.batches):
            draft = next(
                batch
                for batch in assignment.batches
                if batch.status == BatchStatus.DRAFT.value
            )
            return RedirectResponse(f"/batches/{draft.id}", status_code=303)
        party_name = assignment.product.product_name
    party_state = party_state.strip() if parsed == BatchType.SALE else ""
    selected_gst_registration_type = ""
    selected_gst_treatment = ""
    cgst_rate = sgst_rate = igst_rate = None
    if parsed == BatchType.SALE:
        try:
            selected_gst_registration_type = normalize_gst_registration_type(
                party_gst_registration_type,
                parsed,
            ) or ""
            if (
                selected_gst_registration_type == GstRegistrationType.UNREGISTERED_CONSUMER.value
                and not party_state
            ):
                party_state = DEFAULT_UNREGISTERED_SALE_STATE
            if gst_registration_requires_gstin(selected_gst_registration_type):
                normalize_gstin(party_gstin)
            selected_gst_treatment = normalize_sale_gst_treatment(gst_treatment, party_state)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "batch_new.html",
                batch_form_context(
                    request,
                    user,
                    parsed,
                    party_name=party_name,
                    party_state=party_state,
                    party_gst_registration_type=(
                        selected_gst_registration_type or party_gst_registration_type
                    ),
                    party_gst_name=party_gst_name,
                    party_gstin=party_gstin,
                    gst_treatment=selected_gst_treatment or gst_treatment,
                    party_name_options=party_ledger_options(db, parsed, user),
                    notes=notes,
                    error=str(exc),
                ),
                status_code=400,
            )
    party_required = parsed in {
        BatchType.SALE,
        BatchType.SALES_RETURN,
        BatchType.PURCHASE,
        BatchType.RECEIVE,
        BatchType.PURCHASE_RETURN,
    }
    if party_required and not party_name.strip():
        party_label = "Customer" if parsed in {BatchType.SALE, BatchType.SALES_RETURN} else "Supplier"
        return templates.TemplateResponse(
            request,
            "batch_new.html",
            batch_form_context(
                request,
                user,
                parsed,
                party_name=party_name,
                party_state=party_state,
                party_gst_registration_type=(
                    selected_gst_registration_type or party_gst_registration_type
                ),
                party_gst_name=party_gst_name,
                party_gstin=party_gstin,
                gst_treatment=selected_gst_treatment or gst_treatment,
                party_name_options=party_ledger_options(db, parsed, user),
                notes=notes,
                error=f"{party_label} is required.",
            ),
            status_code=400,
        )
    if party_required and not party_ledger_is_allowed(db, user, party_name):
        return templates.TemplateResponse(
            request,
            "batch_new.html",
            batch_form_context(
                request,
                user,
                parsed,
                party_name=party_name,
                party_state=party_state,
                party_gst_registration_type=(
                    selected_gst_registration_type or party_gst_registration_type
                ),
                party_gst_name=party_gst_name,
                party_gstin=party_gstin,
                gst_treatment=selected_gst_treatment or gst_treatment,
                party_name_options=party_ledger_options(db, parsed, user),
                notes=notes,
                error="This Tally ledger is not assigned to your account.",
            ),
            status_code=403,
        )
    try:
        batch = create_batch(
            db,
            user,
            parsed,
            party_name,
            notes,
            reason_code,
            party_state=party_state if parsed == BatchType.SALE else None,
            party_gst_registration_type=(
                selected_gst_registration_type if parsed == BatchType.SALE else None
            ),
            party_gst_name=party_gst_name if parsed == BatchType.SALE else None,
            party_gstin=party_gstin if parsed == BatchType.SALE else None,
            gst_treatment=selected_gst_treatment if parsed == BatchType.SALE else None,
            gst_cgst_rate=cgst_rate,
            gst_sgst_rate=sgst_rate,
            gst_igst_rate=igst_rate,
            audit_assignment_id=assignment.id if assignment else None,
        )
    except InventoryError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "batch_new.html",
            batch_form_context(
                request,
                user,
                parsed,
                party_name=party_name,
                party_state=party_state,
                party_gst_registration_type=(
                    selected_gst_registration_type or party_gst_registration_type
                ),
                party_gst_name=party_gst_name,
                party_gstin=party_gstin,
                gst_treatment=selected_gst_treatment or gst_treatment,
                party_name_options=party_ledger_options(db, parsed, user),
                audit_assignments=open_audit_assignments(db, user) if parsed == BatchType.AUDIT else [],
                audit_assignment_id=audit_assignment_id,
                notes=notes,
                error=str(exc),
            ),
            status_code=400,
        )
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.get("/{batch_id}")
def batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(
            selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product),
            selectinload(Batch.sync_attempts),
            selectinload(Batch.audit_findings).selectinload(AuditFinding.serial),
        )
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    if batch.batch_type == BatchType.QR_ASSIGNMENT.value:
        return RedirectResponse(f"/barcode-assignment/{batch.id}", status_code=303)
    user = require_permission(request, db, data_key_for_batch(BatchType(batch.batch_type)))
    return templates.TemplateResponse(
        request,
        "batch_detail.html",
        {
            "request": request,
            "user": user,
            "batch": batch,
            "products": fefo_product_options(db, batch),
            "summary": calculate_voucher_summary(batch),
            "audit_summary": summarize_audit_findings(batch),
            "shelf_state": shelf_verification_state(batch),
            "sale_return_state": sale_return_state(db, batch),
            "can_manual_scan": can_use_manual_scan(db, user),
            "tally_excel_import_types": TALLY_EXCEL_IMPORT_BATCH_TYPES,
            "tally_excel_export_types": TALLY_EXCEL_EXPORT_BATCH_TYPES,
            "tally_accounting_required_fields": TALLY_ACCOUNTING_REQUIRED_EXPORT_FIELDS,
            "tally_accounting_deselected_fields": tally_accounting_default_deselected_fields(batch),
            "tally_excel_message": tally_excel_import_message(request),
            **batch_permission_context(db, user, batch),
            "error": None,
        },
    )


@router.post("/{batch_id}/scan")
def scan_into_batch(
    request: Request,
    batch_id: int,
    serial_number: str = Form(...),
    scan_source: str = Form("manual"),
    scan_mode: str = Form("sale"),
    db: Session = Depends(get_db),
):
    batch = db.get(Batch, batch_id)
    if not batch:
        return JSONResponse({"ok": False, "error": "Batch not found"}, status_code=404)
    user = require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    if not scan_source_allowed(db, user, scan_source):
        return JSONResponse({"ok": False, "error": "Use camera scan to add serials"}, status_code=403)
    normalized_scan_mode = scan_mode.strip().lower()
    location = find_location_by_code(db, serial_number)
    if location:
        if batch.batch_type == BatchType.SALE.value:
            try:
                verified_count = verify_sale_return_on_shelf(
                    db,
                    batch=batch,
                    location=location,
                    user=user,
                )
            except InventoryError as exc:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": str(exc),
                        **batch_scan_state(db, batch.id),
                    },
                    status_code=400,
                )
            return JSONResponse(
                {
                    "ok": True,
                    "scan_type": "sale_return_shelf",
                    "location_code": location.code,
                    "location": location.full_path,
                    "verified_count": verified_count,
                    **batch_scan_state(db, batch.id),
                }
            )
        try:
            verified_count = verify_pending_items_on_shelf(
                db,
                batch=batch,
                location=location,
                user=user,
            )
        except ShelfVerificationError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    **shelf_verification_state(batch),
                    **batch_scan_state(db, batch.id),
                },
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": True,
                "scan_type": "shelf",
                "location_code": location.code,
                "location": location.full_path,
                "verified_count": verified_count,
                **shelf_verification_state(batch),
                **batch_scan_state(db, batch.id),
            }
        )
    try:
        if batch.batch_type == BatchType.SALE.value and normalized_scan_mode == "return":
            serial = scan_sale_return_product(db, batch, user, serial_number)
            return JSONResponse(
                {
                    "ok": True,
                    "scan_type": "sale_return_product",
                    "serial": serial.serial_number,
                    "product": serial.product.product_name,
                    **batch_scan_state(db, batch.id),
                }
            )
        ensure_sale_scan_allowed(db, batch)
        ensure_product_scan_allowed(batch)
        item = add_serial_to_batch(db, batch, user, serial_number)
    except (InventoryError, ShelfVerificationError) as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                **shelf_verification_state(batch),
                **batch_scan_state(db, batch.id),
            },
            status_code=400,
        )
    return JSONResponse(
        {
            "ok": True,
            "scan_type": "product",
            "serial": item.serial.serial_number,
            "product": item.serial.product.product_name,
            "status": item.serial.display_status,
            **shelf_verification_state(batch),
            **batch_scan_state(db, batch.id),
        }
    )


@router.post("/{batch_id}/fefo")
def fefo_pick_into_batch(
    request: Request,
    batch_id: int,
    product_id: int = Form(...),
    quantity: int = Form(...),
    db: Session = Depends(get_db),
):
    batch = db.get(Batch, batch_id)
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    user = require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    require_permission(request, db, "fefo_pick", {"edit", "yes"})
    try:
        add_fefo_serials_to_batch(db, batch, user, product_id, quantity)
    except InventoryError as exc:
        batch = db.scalar(
            select(Batch)
            .where(Batch.id == batch_id)
            .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
        ) or batch
        return templates.TemplateResponse(
            request,
            "batch_detail.html",
            {
                "request": request,
                "user": user,
                "batch": batch,
                "products": fefo_product_options(db, batch),
                "summary": calculate_voucher_summary(batch),
                "audit_summary": summarize_audit_findings(batch),
                "shelf_state": shelf_verification_state(batch),
                "sale_return_state": sale_return_state(db, batch),
                "can_manual_scan": can_use_manual_scan(db, user),
                **batch_permission_context(db, user, batch),
                "fefo_error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/{batch_id}/items/{item_id}/delete")
def delete_batch_item(request: Request, batch_id: int, item_id: int, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    try:
        remove_batch_item(db, batch, item_id)
    except InventoryError:
        pass
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/{batch_id}/items/{item_id}/rate")
def update_item_rate(
    request: Request,
    batch_id: int,
    item_id: int,
    rate: float = Form(...),
    db: Session = Depends(get_db),
):
    batch = db.get(Batch, batch_id)
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    try:
        update_batch_item_rate(db, batch, item_id, rate)
    except InventoryError as exc:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    if wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/{batch_id}/products/{product_id}/rate")
def update_product_rate(
    request: Request,
    batch_id: int,
    product_id: int,
    rate: float = Form(...),
    db: Session = Depends(get_db),
):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    try:
        update_product_rate_in_batch(db, batch, product_id, rate)
    except InventoryError as exc:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    if wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/{batch_id}/submit")
def submit_batch(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    user = require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    if batch.status != BatchStatus.DRAFT.value:
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    try:
        validate_sale_returns_complete(db, batch)
        validate_priced_batch(batch)
        apply_batch_statuses(db, batch, user)
        if batch.batch_type == BatchType.AUDIT.value:
            reconcile_audit_batch(db, batch)
    except (InventoryError, ValueError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "batch_detail.html",
            {
                "request": request,
                "user": user,
                "batch": batch,
                "products": fefo_product_options(db, batch),
                "summary": calculate_voucher_summary(batch),
                "audit_summary": summarize_audit_findings(batch),
                "shelf_state": shelf_verification_state(batch),
                "sale_return_state": sale_return_state(db, batch),
                "can_manual_scan": can_use_manual_scan(db, user),
                **batch_permission_context(db, user, batch),
                "error": str(exc),
            },
            status_code=400,
        )
    db.commit()
    sync_batch(db, batch)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/{batch_id}/retry")
def retry_batch(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, "tally_sync_retry", {"edit", "yes"})
    if batch.status in {BatchStatus.PENDING_SYNC.value, BatchStatus.FAILED.value, BatchStatus.SUBMITTED.value}:
        sync_batch(db, batch)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.get("/{batch_id}/audit.pdf")
def audit_pdf(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.audit_findings), selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, data_key_for_batch(BatchType(batch.batch_type)))
    if batch.batch_type != BatchType.AUDIT.value:
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    return Response(
        audit_report_pdf(batch),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_number}-audit.pdf"},
    )


@router.get("/{batch_id}/preinvoice.pdf")
def sale_preinvoice(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(
            selectinload(Batch.items)
            .selectinload(BatchItem.serial)
            .selectinload(Serial.product)
        )
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, "sales_data")
    if batch.batch_type != BatchType.SALE.value:
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    if not batch.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add at least one item before generating a pre-invoice.",
        )
    return Response(
        sale_preinvoice_pdf(batch, get_all_settings(db)),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"attachment; filename={batch.batch_number}-preinvoice.pdf"
            )
        },
    )


@router.get("/{batch_id}/tally.xml")
def tally_xml_preview(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_permission(request, db, "tally_xml", {"edit", "yes"})
    if batch.batch_type == BatchType.AUDIT.value:
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    if batch.batch_type not in TALLY_XML_SUPPORTED_BATCH_TYPES:
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    try:
        xml = build_voucher_xml(batch, get_all_settings(db))
    except TallySyncError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(
        xml,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_number}-tally.xml"},
    )


@router.get("/{batch_id}/tally.xlsx")
def tally_excel_export(
    request: Request,
    batch_id: int,
    fields: str = "",
    voucher_type: str = "",
    voucher_number: str = "",
    party_ledger: str = "",
    db: Session = Depends(get_db),
):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    require_user(request, db, ADMIN_ROLES)
    require_permission(request, db, "tally_excel_export", {"edit", "yes"})
    if batch.batch_type not in TALLY_EXCEL_EXPORT_BATCH_TYPES or not batch.items:
        return RedirectResponse(f"/batches/{batch.id}", status_code=303)
    try:
        data = batch_tally_xlsx(
            batch,
            get_all_settings(db),
            fields.split("|"),
            {
                "voucher_type": voucher_type,
                "voucher_number": voucher_number,
                "party_ledger": party_ledger,
            },
        )
    except TallySyncError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_number}-tally.xlsx"},
    )


@router.post("/{batch_id}/tally.xlsx/import")
def tally_excel_import(
    request: Request,
    batch_id: int,
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .options(selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product))
    )
    if not batch:
        return RedirectResponse("/batches", status_code=303)
    user = require_permission(request, db, action_key_for_batch(BatchType(batch.batch_type)))
    require_permission(request, db, "fefo_pick", {"edit", "yes"})
    try:
        data = upload.file.read(MAX_TALLY_EXCEL_UPLOAD_BYTES + 1)
        if len(data) > MAX_TALLY_EXCEL_UPLOAD_BYTES:
            raise InventoryError("Upload an Excel file up to 5 MB")
        result = import_tally_excel_to_batch(db, batch, user, data)
    except InventoryError as exc:
        db.rollback()
        batch = db.scalar(
            select(Batch)
            .where(Batch.id == batch_id)
            .options(
                selectinload(Batch.items).selectinload(BatchItem.serial).selectinload(Serial.product),
                selectinload(Batch.sync_attempts),
                selectinload(Batch.audit_findings).selectinload(AuditFinding.serial),
            )
        ) or batch
        return templates.TemplateResponse(
            request,
            "batch_detail.html",
            {
                "request": request,
                "user": user,
                "batch": batch,
                "products": fefo_product_options(db, batch),
                "summary": calculate_voucher_summary(batch),
                "audit_summary": summarize_audit_findings(batch),
                "shelf_state": shelf_verification_state(batch),
                "sale_return_state": sale_return_state(db, batch),
                "can_manual_scan": can_use_manual_scan(db, user),
                "tally_excel_import_types": TALLY_EXCEL_IMPORT_BATCH_TYPES,
                "tally_excel_export_types": TALLY_EXCEL_EXPORT_BATCH_TYPES,
                "tally_excel_error": str(exc),
                **batch_permission_context(db, user, batch),
                "error": None,
            },
            status_code=400,
        )
    return RedirectResponse(f"/batches/{batch.id}?excel_imported={result.quantity}", status_code=303)


@router.get("/{batch_id}/sync-attempts/{attempt_id}")
def sync_attempt_detail(request: Request, batch_id: int, attempt_id: int, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    attempt = db.get(SyncAttempt, attempt_id)
    if not batch or not attempt or attempt.batch_id != batch.id:
        return RedirectResponse("/batches", status_code=303)
    user = require_permission(request, db, "tally_attempts")
    return templates.TemplateResponse(
        request,
        "sync_attempt_detail.html",
        {"request": request, "user": user, "batch": batch, "attempt": attempt},
    )
