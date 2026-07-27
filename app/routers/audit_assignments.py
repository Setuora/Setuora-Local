from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_permission
from app.database import get_db
from app.models import AuditAssignment, Product, Role, User, has_role
from app.services.access_control import configured_role_has_access
from app.services.audit import (
    assignment_progress,
    create_audit_assignment,
    extend_audit_assignment_deadline,
)
from app.services.inventory import InventoryError
from app.templates import templates

router = APIRouter(prefix="/audit-assignments")
_IST = timezone(timedelta(hours=5, minutes=30))


def _parse_local_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise InventoryError(f"Choose a valid audit {label} date and time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_IST)
    return parsed.astimezone(timezone.utc)


def _can_manage(user: User) -> bool:
    return configured_role_has_access(
        user._access_config,
        user.role,
        "audit_assignment_manage",
        {"edit"},
    )


def _page_context(
    db: Session,
    request: Request,
    user: User,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, object]:
    query = (
        select(AuditAssignment)
        .options(
            selectinload(AuditAssignment.product),
            selectinload(AuditAssignment.auditor),
            selectinload(AuditAssignment.assigned_by),
            selectinload(AuditAssignment.expected_items),
            selectinload(AuditAssignment.batches),
        )
        .order_by(desc(AuditAssignment.ends_at), desc(AuditAssignment.id))
    )
    if not _can_manage(user):
        query = query.where(AuditAssignment.auditor_id == user.id)
    assignments = db.scalars(query).all()
    rows = [
        {
            "assignment": assignment,
            "progress": assignment_progress(db, assignment),
            "starts_local": (
                assignment.starts_at.replace(tzinfo=timezone.utc)
                if assignment.starts_at.tzinfo is None
                else assignment.starts_at
            ).astimezone(_IST),
            "ends_local": (
                assignment.ends_at.replace(tzinfo=timezone.utc)
                if assignment.ends_at.tzinfo is None
                else assignment.ends_at
            ).astimezone(_IST),
            "latest_batch": max(
                assignment.batches,
                key=lambda batch: (batch.created_at, batch.id),
            ),
        }
        for assignment in assignments
    ]
    auditors = [
        candidate
        for candidate in db.scalars(
            select(User)
            .where(User.active.is_(True), User.deleted_at.is_(None))
            .order_by(User.username)
        ).all()
        if has_role(candidate.role, Role.AUDITOR)
    ]
    products = db.scalars(
        select(Product).where(Product.active.is_(True)).order_by(Product.product_name)
    ).all()
    return {
        "request": request,
        "user": user,
        "rows": rows,
        "auditors": auditors,
        "products": products,
        "can_manage": _can_manage(user),
        "error": error,
        "form": form or {},
    }


def _assignment_for_deadline_extension(
    db: Session,
    assignment_id: int,
) -> AuditAssignment | None:
    return db.scalar(
        select(AuditAssignment)
        .where(AuditAssignment.id == assignment_id)
        .options(
            selectinload(AuditAssignment.product),
            selectinload(AuditAssignment.auditor),
            selectinload(AuditAssignment.assigned_by),
            selectinload(AuditAssignment.expected_items),
            selectinload(AuditAssignment.batches),
        )
    )


@router.get("")
def audit_assignments(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "audit_assignment_manage")
    return templates.TemplateResponse(
        request,
        "audit_assignments.html",
        _page_context(db, request, user),
    )


@router.post("")
def assign_audit(
    request: Request,
    product_id: int = Form(...),
    auditor_id: int = Form(...),
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_permission(
        request,
        db,
        "audit_assignment_manage",
        {"edit"},
    )
    form = {
        "product_id": str(product_id),
        "auditor_id": str(auditor_id),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "notes": notes,
    }
    product = db.get(Product, product_id)
    auditor = db.get(User, auditor_id)
    try:
        if not product or not product.active:
            raise InventoryError("Choose an active product")
        if not auditor:
            raise InventoryError("Choose an auditor")
        create_audit_assignment(
            db,
            product=product,
            auditor=auditor,
            assigned_by=user,
            starts_at=_parse_local_datetime(starts_at, "start"),
            ends_at=_parse_local_datetime(ends_at, "end"),
            notes=notes,
        )
    except InventoryError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "audit_assignments.html",
            _page_context(db, request, user, error=str(exc), form=form),
            status_code=400,
        )
    return RedirectResponse("/audit-assignments", status_code=303)


@router.post("/{assignment_id}/extend")
def extend_audit_deadline(
    request: Request,
    assignment_id: int,
    ends_at: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_permission(
        request,
        db,
        "audit_assignment_manage",
        {"edit"},
    )
    form = {
        "extend_assignment_id": str(assignment_id),
        "extend_ends_at": ends_at,
    }
    assignment = _assignment_for_deadline_extension(db, assignment_id)
    try:
        if not assignment:
            raise InventoryError("Choose an audit assignment")
        extend_audit_assignment_deadline(
            db,
            assignment=assignment,
            actor=user,
            ends_at=_parse_local_datetime(ends_at, "end"),
        )
    except InventoryError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "audit_assignments.html",
            _page_context(db, request, user, error=str(exc), form=form),
            status_code=400,
        )
    return RedirectResponse("/audit-assignments", status_code=303)
