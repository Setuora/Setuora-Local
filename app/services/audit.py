from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, delete, desc, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.models import (
    AuditAssignment,
    AuditAssignmentItem,
    AuditFinding,
    Batch,
    BatchItem,
    BatchType,
    Product,
    Role,
    Serial,
    SerialStatus,
    User,
    has_role,
)
from app.services.change_audit import record_change
from app.services.expiry import STOCK_STATUSES
from app.services.inventory import InventoryError


@dataclass(frozen=True)
class AuditSummary:
    verified: int
    missing: int
    extra: int
    pending: int
    total: int


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def audit_assignment_state(
    assignment: AuditAssignment,
    now: datetime | None = None,
    *,
    verified: int = 0,
    expected: int | None = None,
) -> str:
    moment = _utc(now or datetime.now(timezone.utc))
    starts_at = _utc(assignment.starts_at)
    ends_at = _utc(assignment.ends_at)
    expected_count = len(assignment.expected_items) if expected is None else expected
    if expected_count and verified >= expected_count:
        return "COMPLETE"
    if moment < starts_at:
        return "UPCOMING"
    if moment <= ends_at:
        return "ACTIVE"
    return "EXPIRED"


def create_audit_assignment(
    db: Session,
    *,
    product: Product,
    auditor: User,
    assigned_by: User,
    starts_at: datetime,
    ends_at: datetime,
    notes: str | None = None,
) -> AuditAssignment:
    if not auditor.active or auditor.deleted_at or not has_role(auditor.role, Role.AUDITOR):
        raise InventoryError("Choose an active user with the auditor role")
    starts_at = _utc(starts_at)
    ends_at = _utc(ends_at)
    if ends_at <= starts_at:
        raise InventoryError("Audit end must be after audit start")
    if ends_at <= datetime.now(timezone.utc):
        raise InventoryError("Audit end must be in the future")

    expected = db.scalars(
        select(Serial)
        .where(
            Serial.product_id == product.id,
            Serial.active.is_(True),
            Serial.status == SerialStatus.IN_STOCK.value,
        )
        .order_by(Serial.serial_number)
    ).all()
    if not expected:
        raise InventoryError("This product has no in-stock serials to audit")

    assignment = AuditAssignment(
        product_id=product.id,
        auditor_id=auditor.id,
        assigned_by_id=assigned_by.id,
        starts_at=starts_at,
        ends_at=ends_at,
        notes=notes.strip() if notes else None,
    )
    db.add(assignment)
    db.flush()
    db.add_all(
        AuditAssignmentItem(assignment_id=assignment.id, serial_id=serial.id)
        for serial in expected
    )

    # Every assignment has an anchor batch so an untouched, expired assignment can
    # still produce a complete missing-stock report.
    from app.services.inventory import create_batch

    create_batch(
        db,
        auditor,
        BatchType.AUDIT,
        product.product_name,
        notes,
        audit_assignment_id=assignment.id,
        commit=False,
    )
    db.commit()
    return db.scalar(
        select(AuditAssignment)
        .where(AuditAssignment.id == assignment.id)
        .options(
            selectinload(AuditAssignment.product),
            selectinload(AuditAssignment.auditor),
            selectinload(AuditAssignment.assigned_by),
            selectinload(AuditAssignment.expected_items),
            selectinload(AuditAssignment.batches),
        )
    )


def audit_assignment_snapshot(assignment: AuditAssignment) -> dict[str, object]:
    return {
        "id": assignment.id,
        "product_id": assignment.product_id,
        "auditor_id": assignment.auditor_id,
        "assigned_by_id": assignment.assigned_by_id,
        "starts_at": _utc(assignment.starts_at),
        "ends_at": _utc(assignment.ends_at),
        "notes": assignment.notes,
    }


def extend_audit_assignment_deadline(
    db: Session,
    *,
    assignment: AuditAssignment,
    actor: User,
    ends_at: datetime,
    now: datetime | None = None,
) -> AuditAssignment:
    moment = _utc(now or datetime.now(timezone.utc))
    new_end = _utc(ends_at)
    current_end = _utc(assignment.ends_at)
    starts_at = _utc(assignment.starts_at)
    if new_end <= starts_at:
        raise InventoryError("Audit end must be after audit start")
    if new_end <= current_end:
        raise InventoryError("Choose a deadline after the current audit end")
    if new_end <= moment:
        raise InventoryError("Audit end must be in the future")

    before = audit_assignment_snapshot(assignment)
    assignment.ends_at = new_end
    if assignment.batches:
        latest = max(assignment.batches, key=lambda row: (row.created_at, row.id))
        reconcile_audit_assignment(db, assignment, latest, now=moment)
    record_change(
        db,
        actor,
        entity_type="audit_assignment",
        entity_id=assignment.id,
        action="extend_deadline",
        before=before,
        after=audit_assignment_snapshot(assignment),
    )
    db.commit()
    db.refresh(assignment)
    return assignment


def assignment_progress(db: Session, assignment: AuditAssignment) -> dict[str, object]:
    expected_ids = {item.serial_id for item in assignment.expected_items}
    scanned_ids = set(
        db.scalars(
            select(BatchItem.serial_id)
            .join(Batch, BatchItem.batch_id == Batch.id)
            .where(
                Batch.audit_assignment_id == assignment.id,
                BatchItem.created_at >= assignment.starts_at,
                BatchItem.created_at <= assignment.ends_at,
            )
        ).all()
    )
    verified = len(expected_ids & scanned_ids)
    pending = len(expected_ids - scanned_ids)
    return {
        "expected": len(expected_ids),
        "verified": verified,
        "pending": pending,
        "extra": len(scanned_ids - expected_ids),
        "state": audit_assignment_state(
            assignment,
            verified=verified,
            expected=len(expected_ids),
        ),
    }


def validate_assignment_scan(
    batch: Batch,
    user: User,
    serial: Serial,
    now: datetime | None = None,
) -> None:
    assignment = batch.audit_assignment
    if not assignment:
        return
    moment = _utc(now or datetime.now(timezone.utc))
    if user.id != assignment.auditor_id:
        raise InventoryError("This audit is assigned to another auditor")
    if moment < _utc(assignment.starts_at):
        raise InventoryError("This audit window has not started")
    if moment > _utc(assignment.ends_at):
        raise InventoryError("This audit window has ended")
    if serial.product_id != assignment.product_id:
        raise InventoryError(
            f"This assignment is only for {assignment.product.product_name}"
        )


def reconcile_audit_batch(
    db: Session,
    batch: Batch,
    *,
    now: datetime | None = None,
) -> AuditSummary:
    if batch.batch_type != BatchType.AUDIT.value:
        return AuditSummary(0, 0, 0, 0, 0)
    if batch.audit_assignment_id:
        assignment = db.scalar(
            select(AuditAssignment)
            .where(AuditAssignment.id == batch.audit_assignment_id)
            .options(
                selectinload(AuditAssignment.expected_items)
                .selectinload(AuditAssignmentItem.serial)
                .selectinload(Serial.product),
                selectinload(AuditAssignment.batches),
            )
        )
        if assignment:
            return reconcile_audit_assignment(db, assignment, batch, now=now)

    db.execute(delete(AuditFinding).where(AuditFinding.batch_id == batch.id))
    scanned = {item.serial_id: item.serial for item in batch.items}
    expected = db.scalars(
            select(Serial).where(
                Serial.status == SerialStatus.IN_STOCK.value,
                Serial.active.is_(True),
            )
    ).all()
    expected_ids = {serial.id for serial in expected}
    findings = []

    for serial in expected:
        if serial.id in scanned:
            findings.append(
                make_finding(
                    batch,
                    serial,
                    "VERIFIED",
                    SerialStatus.IN_STOCK.value,
                    serial.status,
                )
            )
        else:
            findings.append(
                make_finding(
                    batch,
                    serial,
                    "MISSING",
                    SerialStatus.IN_STOCK.value,
                    None,
                )
            )

    for serial_id, serial in scanned.items():
        if serial_id not in expected_ids:
            findings.append(
                make_finding(
                    batch,
                    serial,
                    "EXTRA",
                    SerialStatus.IN_STOCK.value,
                    serial.status,
                )
            )

    db.add_all(findings)
    db.flush()
    return _summary(findings)


def reconcile_audit_assignment(
    db: Session,
    assignment: AuditAssignment,
    anchor_batch: Batch | None = None,
    *,
    now: datetime | None = None,
) -> AuditSummary:
    if anchor_batch is None:
        anchor_batch = max(assignment.batches, key=lambda row: (row.created_at, row.id))
    batch_ids = select(Batch.id).where(Batch.audit_assignment_id == assignment.id)
    db.execute(delete(AuditFinding).where(AuditFinding.batch_id.in_(batch_ids)))

    expected = {item.serial_id: item.serial for item in assignment.expected_items}
    scanned_serials = db.scalars(
        select(Serial)
        .join(BatchItem, BatchItem.serial_id == Serial.id)
        .join(Batch, Batch.id == BatchItem.batch_id)
        .where(
            Batch.audit_assignment_id == assignment.id,
            BatchItem.created_at >= assignment.starts_at,
            BatchItem.created_at <= assignment.ends_at,
        )
        .distinct()
    ).all()
    scanned = {serial.id: serial for serial in scanned_serials}
    expired = _utc(now or datetime.now(timezone.utc)) > _utc(assignment.ends_at)
    findings: list[AuditFinding] = []

    for serial_id, serial in expected.items():
        if serial_id in scanned:
            finding_type = "VERIFIED"
            scanned_status = scanned[serial_id].status
        else:
            finding_type = "MISSING" if expired else "PENDING"
            scanned_status = None
        findings.append(
            make_finding(
                anchor_batch,
                serial,
                finding_type,
                SerialStatus.IN_STOCK.value,
                scanned_status,
            )
        )

    for serial_id, serial in scanned.items():
        if serial_id not in expected:
            findings.append(
                make_finding(
                    anchor_batch,
                    serial,
                    "EXTRA",
                    SerialStatus.IN_STOCK.value,
                    serial.status,
                )
            )

    db.add_all(findings)
    db.flush()
    return _summary(findings)


def refresh_expired_audit_assignments(
    db: Session,
    now: datetime | None = None,
) -> None:
    moment = _utc(now or datetime.now(timezone.utc))
    assignments = db.scalars(
        select(AuditAssignment)
        .where(AuditAssignment.ends_at < moment)
        .options(
            selectinload(AuditAssignment.expected_items)
            .selectinload(AuditAssignmentItem.serial)
            .selectinload(Serial.product),
            selectinload(AuditAssignment.batches),
        )
    ).all()
    changed = False
    for assignment in assignments:
        latest = max(assignment.batches, key=lambda row: (row.created_at, row.id))
        has_pending = db.scalar(
            select(AuditFinding.id)
            .where(
                AuditFinding.batch_id.in_(
                    select(Batch.id).where(
                        Batch.audit_assignment_id == assignment.id
                    )
                ),
                AuditFinding.finding_type == "PENDING",
            )
            .limit(1)
        )
        has_findings = db.scalar(
            select(AuditFinding.id)
            .where(
                AuditFinding.batch_id.in_(
                    select(Batch.id).where(
                        Batch.audit_assignment_id == assignment.id
                    )
                )
            )
            .limit(1)
        )
        if has_pending or not has_findings:
            reconcile_audit_assignment(db, assignment, latest, now=moment)
            changed = True
    if changed:
        db.commit()


def make_finding(
    batch: Batch,
    serial: Serial,
    finding_type: str,
    expected_status: str | None,
    scanned_status: str | None,
) -> AuditFinding:
    return AuditFinding(
        batch_id=batch.id,
        serial_id=serial.id,
        serial_number=serial.serial_number,
        product_code=serial.product.product_code,
        product_name=serial.product.product_name,
        finding_type=finding_type,
        expected_status=expected_status,
        scanned_status=scanned_status,
    )


def _summary(findings) -> AuditSummary:
    counts = Counter(finding.finding_type for finding in findings)
    return AuditSummary(
        verified=counts["VERIFIED"],
        missing=counts["MISSING"],
        extra=counts["EXTRA"],
        pending=counts["PENDING"],
        total=len(findings),
    )


def summarize_audit_findings(batch: Batch) -> AuditSummary:
    return _summary(batch.audit_findings)


def current_missing_stock_findings_query():
    """Return missing findings that have not been resolved by a newer audit scan."""
    newer_finding = aliased(AuditFinding)
    newer_for_same_serial = (
        select(newer_finding.id)
        .where(
            newer_finding.serial_id == AuditFinding.serial_id,
            or_(
                newer_finding.created_at > AuditFinding.created_at,
                and_(
                    newer_finding.created_at == AuditFinding.created_at,
                    newer_finding.id > AuditFinding.id,
                ),
            ),
        )
        .exists()
    )
    return (
        select(AuditFinding)
        .join(Serial, AuditFinding.serial_id == Serial.id)
        .where(
            AuditFinding.finding_type == "MISSING",
            AuditFinding.serial_id.is_not(None),
            Serial.active.is_(True),
            Serial.status.in_(STOCK_STATUSES),
            ~newer_for_same_serial,
        )
        .order_by(desc(AuditFinding.created_at), desc(AuditFinding.id))
    )
