from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_permission
from app.database import get_db
from app.models import AuditAssignment, AuditFinding, Batch, InventoryTransaction, Product, Role, ScanLog, Serial, TransactionType, has_any_role, has_role
from app.services.audit import assignment_progress, current_missing_stock_findings_query, refresh_expired_audit_assignments
from app.services.charts import bar_chart, donut_chart
from app.services.director_reports import (
    director_audit_batch_report,
    director_audit_reconciliation_report,
    director_product_filter_options,
    director_report,
    director_warehouse_filter_options,
)
from app.services.expiry import expiry_summary
from app.services.exports import audit_reconciliation_xlsx, missing_stock_xlsx, scans_xlsx, transactions_xlsx
from app.services.losses import loss_summary
from app.services.log_fields import barcode_sold_by, invoice_created_by, product_audited_by
from app.services.report_format import report_date
from app.templates import templates

router = APIRouter(prefix="/reports")
MISSING_STOCK_ACTION = "MISSING"


def parse_filter_date(value: str, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} date",
        ) from exc


def parse_period_datetime(value: str, field_name: str, *, end: bool = False) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} date/time",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if end and len(raw) == 10:
        parsed = parsed + timedelta(days=1)
    return parsed


def export_url(path: str, params: dict[str, str]) -> str:
    filtered = {key: value for key, value in params.items() if value}
    return f"{path}?{urlencode(filtered)}" if filtered else path


def parse_optional_product_id(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        product_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid product filter",
        ) from exc
    if product_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid product filter",
        )
    return product_id


def scan_query(action: str = "", start: str = "", end: str = "", product_id: int | None = None):
    conditions = []
    if action:
        conditions.append(ScanLog.action == action)
    if product_id is not None:
        conditions.append(Serial.product_id == product_id)
    start_dt = parse_filter_date(start, "start")
    if start_dt:
        conditions.append(ScanLog.created_at >= start_dt)
    end_dt = parse_filter_date(end, "end")
    if end_dt:
        end_dt = end_dt + timedelta(days=1)
        conditions.append(ScanLog.created_at < end_dt)
    query = (
        select(ScanLog)
        .order_by(desc(ScanLog.created_at))
        .limit(500)
        .options(
            selectinload(ScanLog.user),
            selectinload(ScanLog.batch),
            selectinload(ScanLog.serial),
        )
    )
    if product_id is not None:
        query = query.join(Serial, ScanLog.serial_id == Serial.id)
    if conditions:
        query = query.where(and_(*conditions))
    return query


def transaction_query(
    action: str = "",
    q: str = "",
    start: str = "",
    end: str = "",
    product_id: int | None = None,
):
    conditions = []
    if action:
        conditions.append(InventoryTransaction.transaction_type == action)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                InventoryTransaction.serial_number.ilike(like),
                InventoryTransaction.tally_reference.ilike(like),
                InventoryTransaction.reference_number.ilike(like),
                Product.product_code.ilike(like),
                Product.product_name.ilike(like),
            )
        )
    if product_id is not None:
        conditions.append(InventoryTransaction.product_id == product_id)
    start_dt = parse_filter_date(start, "start")
    if start_dt:
        conditions.append(InventoryTransaction.created_at >= start_dt)
    end_dt = parse_filter_date(end, "end")
    if end_dt:
        end_dt = end_dt + timedelta(days=1)
        conditions.append(InventoryTransaction.created_at < end_dt)
    query = (
        select(InventoryTransaction)
        .outerjoin(Product, InventoryTransaction.product_id == Product.id)
        .order_by(desc(InventoryTransaction.created_at))
        .limit(500)
        .options(
            selectinload(InventoryTransaction.user),
            selectinload(InventoryTransaction.serial),
            selectinload(InventoryTransaction.product),
            selectinload(InventoryTransaction.batch).selectinload(Batch.user),
        )
    )
    if conditions:
        query = query.where(and_(*conditions))
    return query


def missing_stock_query(
    q: str = "",
    start: str = "",
    end: str = "",
    product_id: int | None = None,
):
    conditions = []
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                AuditFinding.serial_number.ilike(like),
                AuditFinding.product_code.ilike(like),
                AuditFinding.product_name.ilike(like),
                Batch.batch_number.ilike(like),
            )
        )
    start_dt = parse_filter_date(start, "start")
    if start_dt:
        conditions.append(AuditFinding.created_at >= start_dt)
    end_dt = parse_filter_date(end, "end")
    if end_dt:
        conditions.append(AuditFinding.created_at < end_dt + timedelta(days=1))
    if product_id is not None:
        conditions.append(Serial.product_id == product_id)
    query = current_missing_stock_findings_query().join(Batch, AuditFinding.batch_id == Batch.id)
    if conditions:
        query = query.where(and_(*conditions))
    return query.limit(500).options(
        selectinload(AuditFinding.batch).selectinload(Batch.user),
        selectinload(AuditFinding.serial).selectinload(Serial.location),
    )


def audit_assignment_rows(
    db: Session,
    start: str = "",
    end: str = "",
    product_id: int | None = None,
) -> list[dict[str, object]]:
    conditions = []
    if product_id is not None:
        conditions.append(AuditAssignment.product_id == product_id)
    start_dt = parse_filter_date(start, "start")
    if start_dt:
        conditions.append(AuditAssignment.ends_at >= start_dt)
    end_dt = parse_filter_date(end, "end")
    if end_dt:
        conditions.append(AuditAssignment.starts_at < end_dt + timedelta(days=1))
    query = (
        select(AuditAssignment)
        .order_by(desc(AuditAssignment.ends_at), desc(AuditAssignment.id))
        .limit(50)
        .options(
            selectinload(AuditAssignment.product),
            selectinload(AuditAssignment.auditor),
            selectinload(AuditAssignment.expected_items),
            selectinload(AuditAssignment.batches),
        )
    )
    if conditions:
        query = query.where(and_(*conditions))
    assignments = db.scalars(query).all()
    return [
        {
            "assignment": assignment,
            "progress": assignment_progress(db, assignment),
            "latest_batch": max(assignment.batches, key=lambda batch: (batch.created_at, batch.id), default=None),
        }
        for assignment in assignments
    ]


@router.get("")
def reports(
    request: Request,
    action: str = "",
    product_id: str = "",
    start: str = "",
    end: str = "",
    audit_start: str = "",
    audit_end: str = "",
    product_q: str = "",
    warehouse_q: str = "",
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "reports_data")
    parsed_product_id = parse_optional_product_id(product_id)
    refresh_expired_audit_assignments(db)
    if has_role(user.role, Role.DIRECTORS) and not has_any_role(user.role, {Role.ADMIN, Role.SUPER_ADMIN}):
        audit_start_dt = parse_period_datetime(audit_start, "audit start")
        audit_end_dt = parse_period_datetime(audit_end, "audit end", end=True)
        return templates.TemplateResponse(
            request,
            "director_reports.html",
            {
                "request": request,
                "user": user,
                "report": director_report(
                    db,
                    audit_start_dt,
                    audit_end_dt,
                    product_q,
                    warehouse_q,
                ),
                "audit_start": audit_start,
                "audit_end": audit_end,
                "product_q": product_q,
                "warehouse_q": warehouse_q,
                "director_product_options": director_product_filter_options(db),
                "director_warehouse_options": director_warehouse_filter_options(db),
                "audit_reconciliation_export_url": export_url(
                    "/reports/audit-reconciliation.xlsx",
                    {"start": audit_start, "end": audit_end},
                ),
                "director_live_url": export_url(
                    "/reports/live",
                    {
                        "audit_start": audit_start,
                        "audit_end": audit_end,
                        "product_q": product_q,
                        "warehouse_q": warehouse_q,
                    },
                ),
            },
        )

    start_dt = parse_filter_date(start, "start")
    end_dt = parse_filter_date(end, "end")
    if end_dt:
        end_dt = end_dt + timedelta(days=1)
    missing_stock_selected = action == MISSING_STOCK_ACTION
    products = db.scalars(select(Product).order_by(Product.product_code, Product.product_name)).all()
    selected_product = db.get(Product, parsed_product_id) if parsed_product_id is not None else None
    scans = [] if missing_stock_selected else db.scalars(scan_query(action, start, end, parsed_product_id)).all()
    transactions = [] if missing_stock_selected else db.scalars(transaction_query(action, "", start, end, parsed_product_id)).all()
    audit_assignments = audit_assignment_rows(db, start, end, parsed_product_id)
    missing_stock = (
        db.scalars(missing_stock_query("", start, end, parsed_product_id)).all()
        if not action or missing_stock_selected
        else []
    )
    transaction_counts = Counter(txn.transaction_type for txn in transactions)
    if missing_stock:
        transaction_counts[MISSING_STOCK_ACTION] = len(missing_stock)
    scan_status_counts = Counter(scan.status for scan in scans)
    pending = db.scalars(
        select(Batch)
        .where(Batch.status.in_(["PENDING_SYNC", "FAILED"]))
        .order_by(desc(Batch.created_at))
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "request": request,
            "user": user,
            "scans": scans,
            "transactions": transactions,
            "audit_assignments": audit_assignments,
            "missing_stock": missing_stock,
            "pending": pending,
            "transaction_chart": bar_chart(transaction_counts.items()),
            "scan_status_chart": donut_chart(scan_status_counts.items()),
            "expiry": expiry_summary(db, product_id=parsed_product_id),
            "losses": (
                loss_summary(db, action=action, q="", start=start_dt, end=end_dt, product_id=parsed_product_id)
                if has_any_role(user.role, {Role.ADMIN, Role.SUPER_ADMIN})
                else None
            ),
            "action": action,
            "product_id": parsed_product_id or "",
            "products": products,
            "selected_product": selected_product,
            "start": start,
            "end": end,
            "transaction_types": [item.value for item in TransactionType] + [MISSING_STOCK_ACTION],
            "invoice_created_by": invoice_created_by,
            "barcode_sold_by": barcode_sold_by,
            "product_audited_by": product_audited_by,
            "audit_reconciliation_export_url": export_url(
                "/reports/audit-reconciliation.xlsx",
                {"start": start, "end": end},
            ),
            "missing_stock_export_url": export_url(
                "/reports/missing-stock.xlsx",
                {"product_id": str(parsed_product_id or ""), "start": start, "end": end},
            ),
            "transaction_export_url": export_url(
                "/reports/transactions.xlsx",
                {"action": action, "product_id": str(parsed_product_id or ""), "start": start, "end": end},
            ),
            "scan_export_url": export_url(
                "/reports/scans.xlsx",
                {"action": action, "product_id": str(parsed_product_id or ""), "start": start, "end": end},
            ),
        },
    )


@router.get("/live")
def director_report_live(
    request: Request,
    audit_start: str = "",
    audit_end: str = "",
    product_q: str = "",
    warehouse_q: str = "",
    db: Session = Depends(get_db),
):
    """Return the current Director Report data for background refreshes."""
    require_permission(request, db, "reports_data")
    refresh_expired_audit_assignments(db)
    audit_start_at = parse_period_datetime(audit_start, "audit start")
    audit_end_at = parse_period_datetime(audit_end, "audit end", end=True)
    report = director_report(db, audit_start_at, audit_end_at, product_q, warehouse_q)
    latest_audit = report["latest_audit"]
    reconciliation = report["reconciliation"]

    return JSONResponse(
        {
            "director_metrics": {
                "audit_batch_count": report["audit_batch_count"],
                "latest_missing": report["latest_missing"],
                "latest_extra": report["latest_extra"],
                "expiry_risk": report["expiry"]["widgets"]["high_risk"],
                "dead_stock": len(report["dead_stock_rows"]),
                "latest_audit_at": report_date(latest_audit["audit_at"]) if latest_audit else "No audit",
                "total_products": report["products"]["total_products"],
                "total_stock": report["products"]["total_stock"],
            },
            "latest_audit_url": (
                f"/reports/audit-batches/{latest_audit['id']}" if latest_audit else "#audit-batches"
            ),
            "reconciliation": {
                "audit_batch_count": reconciliation["audit_batch_count"],
                "verified": reconciliation["verified"],
                "pending": reconciliation["pending"],
                "missing": reconciliation["missing"],
                "extra": reconciliation["extra"],
                "total": reconciliation["total"],
            },
            "product_rows_html": templates.env.get_template(
                "partials/product_stock_summary_rows.html"
            ).render(
                product_rows=report["product_rows"],
                show_audit_variance=True,
            ),
            "warehouse_rows_html": templates.env.get_template(
                "partials/director_warehouse_rows.html"
            ).render(report=report),
            "audit_batches_html": templates.env.get_template(
                "partials/director_audit_batch_rows.html"
            ).render(report=report),
            "expiry_risk_html": templates.env.get_template(
                "partials/director_expiry_risk_rows.html"
            ).render(report=report),
            "dead_stock_html": templates.env.get_template(
                "partials/director_dead_stock_rows.html"
            ).render(report=report),
        }
    )


@router.get("/missing-stock")
def missing_stock_report(
    request: Request,
    product_id: str = "",
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "reports_data")
    parsed_product_id = parse_optional_product_id(product_id)
    refresh_expired_audit_assignments(db)
    products = db.scalars(select(Product).order_by(Product.product_code, Product.product_name)).all()
    selected_product = db.get(Product, parsed_product_id) if parsed_product_id is not None else None
    findings = db.scalars(missing_stock_query("", start, end, parsed_product_id)).all()
    return templates.TemplateResponse(
        request,
        "missing_stock_report.html",
        {
            "request": request,
            "user": user,
            "findings": findings,
            "summary": {
                "total": len(findings),
                "products": len({finding.product_code for finding in findings if finding.product_code}),
                "audit_batches": len({finding.batch_id for finding in findings}),
                "warehouses": len(
                    {
                        finding.serial.warehouse
                        for finding in findings
                        if finding.serial and finding.serial.warehouse
                    }
                ),
            },
            "product_id": parsed_product_id or "",
            "products": products,
            "selected_product": selected_product,
            "missing_stock_export_url": export_url(
                "/reports/missing-stock.xlsx",
                {"product_id": str(parsed_product_id or ""), "start": start, "end": end},
            ),
            "start": start,
            "end": end,
        },
    )


@router.get("/audit-batches/{batch_id}")
def director_audit_batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = require_permission(request, db, "reports_data")
    refresh_expired_audit_assignments(db)
    report = director_audit_batch_report(db, batch_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit batch not found")
    return templates.TemplateResponse(
        request,
        "director_audit_batch.html",
        {
            "request": request,
            "user": user,
            "report": report,
        },
    )


@router.get("/audit-reconciliation.xlsx")
def audit_reconciliation_excel(
    request: Request,
    start: str = "",
    end: str = "",
    fields: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "reports_data")
    refresh_expired_audit_assignments(db)
    start_at = parse_period_datetime(start, "audit start")
    end_at = parse_period_datetime(end, "audit end", end=True)
    if start_at and end_at and start_at >= end_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Audit start must be before audit end",
        )
    report = director_audit_reconciliation_report(db, start_at, end_at)
    return Response(
        audit_reconciliation_xlsx(report, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=setuora-audit-reconciliation.xlsx"},
    )


@router.get("/scans.xlsx")
def scans_excel(
    request: Request,
    action: str = "",
    product_id: str = "",
    start: str = "",
    end: str = "",
    fields: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "reports_export")
    parsed_product_id = parse_optional_product_id(product_id)
    scans = db.scalars(scan_query(action, start, end, parsed_product_id)).all()
    return Response(
        scans_xlsx(scans, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=setuora-scans.xlsx"},
    )


@router.get("/transactions.xlsx")
def transactions_excel(
    request: Request,
    action: str = "",
    q: str = "",
    product_id: str = "",
    start: str = "",
    end: str = "",
    fields: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "reports_export")
    parsed_product_id = parse_optional_product_id(product_id)
    transactions = db.scalars(transaction_query(action, q, start, end, parsed_product_id)).all()
    return Response(
        transactions_xlsx(transactions, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=setuora-transactions.xlsx"},
    )


@router.get("/missing-stock.xlsx")
def missing_stock_excel(
    request: Request,
    q: str = "",
    product_id: str = "",
    start: str = "",
    end: str = "",
    fields: str = "",
    db: Session = Depends(get_db),
):
    require_permission(request, db, "reports_export")
    parsed_product_id = parse_optional_product_id(product_id)
    refresh_expired_audit_assignments(db)
    findings = db.scalars(missing_stock_query(q, start, end, parsed_product_id)).all()
    return Response(
        missing_stock_xlsx(findings, fields.split("|")),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=setuora-missing-stock.xlsx"},
    )
