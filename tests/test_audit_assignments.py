from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.auth import SESSION_COOKIE
from app.models import AuditAssignment, Batch, ChangeAudit, Product, SerialStatus, User
from app.routers.audit_assignments import assign_audit, audit_assignments, extend_audit_deadline
from app.security import create_session_token
from app.services.audit import refresh_expired_audit_assignments, summarize_audit_findings
from app.services.inventory import generate_serials

_IST = timezone(timedelta(hours=5, minutes=30))


def signed_request(user_id: int, method: str = "GET", path: str = "/audit-assignments") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (
                    b"cookie",
                    f"{SESSION_COOKIE}={create_session_token(user_id)}".encode(),
                )
            ],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@pytest.mark.parametrize("manager_role", ["admin", "directors"])
def test_admin_and_director_can_assign_timed_product_audit(
    db_session,
    manager_role,
):
    manager = User(
        username=f"{manager_role}-manager",
        password_hash="x",
        role=manager_role,
    )
    auditor = User(username=f"{manager_role}-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code=f"{manager_role[:3].upper()}-AUD",
        product_name=f"{manager_role} audit product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name=f"{manager_role} audit product",
    )
    db_session.add_all([manager, auditor, product])
    db_session.commit()
    generate_serials(
        db_session,
        product,
        2,
        initial_status=SerialStatus.IN_STOCK,
    )
    now = datetime.now(_IST)

    response = assign_audit(
        signed_request(manager.id, "POST"),
        product_id=product.id,
        auditor_id=auditor.id,
        starts_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        ends_at=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        notes="Count this product",
        db=db_session,
    )
    assignment = db_session.scalar(select(AuditAssignment))

    assert response.status_code == 303
    assert assignment.assigned_by_id == manager.id
    assert assignment.auditor_id == auditor.id
    assert len(assignment.expected_items) == 2
    assert len(assignment.batches) == 1
    assert assignment.batches[0].user_id == auditor.id

    auditor_page = audit_assignments(signed_request(auditor.id), db=db_session)
    assert auditor_page.status_code == 200
    assert product.product_name in auditor_page.body.decode()
    assert f"/audit-assignments/{assignment.id}/extend" not in auditor_page.body.decode()


@pytest.mark.parametrize("manager_role", ["admin", "super_admin", "directors"])
def test_admin_super_admin_and_director_can_extend_audit_deadline(
    db_session,
    manager_role,
):
    manager = User(
        username=f"{manager_role}-deadline-manager",
        password_hash="x",
        role=manager_role,
    )
    auditor = User(username=f"{manager_role}-deadline-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code=f"{manager_role[:3].upper()}-EXT",
        product_name=f"{manager_role} extension product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name=f"{manager_role} extension product",
    )
    db_session.add_all([manager, auditor, product])
    db_session.commit()
    generate_serials(
        db_session,
        product,
        1,
        initial_status=SerialStatus.IN_STOCK,
    )
    now = datetime.now(_IST)
    assign_audit(
        signed_request(manager.id, "POST"),
        product_id=product.id,
        auditor_id=auditor.id,
        starts_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        ends_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        notes="",
        db=db_session,
    )
    assignment = db_session.scalar(select(AuditAssignment))
    old_end = assignment.ends_at

    response = extend_audit_deadline(
        signed_request(
            manager.id,
            "POST",
            f"/audit-assignments/{assignment.id}/extend",
        ),
        assignment_id=assignment.id,
        ends_at=(now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
        db=db_session,
    )
    db_session.expire_all()
    updated = db_session.get(AuditAssignment, assignment.id)
    audit = db_session.scalar(
        select(ChangeAudit).where(
            ChangeAudit.entity_type == "audit_assignment",
            ChangeAudit.entity_id == str(assignment.id),
        )
    )
    manager_page = audit_assignments(signed_request(manager.id), db=db_session)

    assert response.status_code == 303
    assert as_utc(updated.ends_at) > as_utc(old_end)
    assert audit.actor_username == manager.username
    assert audit.action == "extend_deadline"
    assert f'action="/audit-assignments/{assignment.id}/extend"' in manager_page.body.decode()


def test_audit_deadline_extension_must_be_after_current_deadline(db_session):
    manager = User(username="deadline-admin", password_hash="x", role="admin")
    auditor = User(username="deadline-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code="SHORT-EXT",
        product_name="Short extension product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Short extension product",
    )
    db_session.add_all([manager, auditor, product])
    db_session.commit()
    generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)
    now = datetime.now(_IST)
    assign_audit(
        signed_request(manager.id, "POST"),
        product_id=product.id,
        auditor_id=auditor.id,
        starts_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        ends_at=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        notes="",
        db=db_session,
    )
    assignment = db_session.scalar(select(AuditAssignment))
    old_end = assignment.ends_at

    response = extend_audit_deadline(
        signed_request(
            manager.id,
            "POST",
            f"/audit-assignments/{assignment.id}/extend",
        ),
        assignment_id=assignment.id,
        ends_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        db=db_session,
    )
    db_session.expire_all()
    updated = db_session.get(AuditAssignment, assignment.id)

    assert response.status_code == 400
    assert "after the current audit end" in response.body.decode()
    assert as_utc(updated.ends_at) == as_utc(old_end)


def test_extending_expired_audit_deadline_refreshes_missing_findings(db_session):
    manager = User(username="refresh-admin", password_hash="x", role="admin")
    auditor = User(username="refresh-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code="REFRESH-EXT",
        product_name="Refresh extension product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Refresh extension product",
    )
    db_session.add_all([manager, auditor, product])
    db_session.commit()
    generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    now = datetime.now(_IST)
    assign_audit(
        signed_request(manager.id, "POST"),
        product_id=product.id,
        auditor_id=auditor.id,
        starts_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        ends_at=(now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
        notes="",
        db=db_session,
    )
    assignment = db_session.scalar(select(AuditAssignment))
    batch_id = assignment.batches[0].id

    refresh_expired_audit_assignments(
        db_session,
        now=as_utc(assignment.ends_at) + timedelta(seconds=1),
    )
    db_session.expire_all()
    expired = summarize_audit_findings(db_session.get(Batch, batch_id))

    response = extend_audit_deadline(
        signed_request(
            manager.id,
            "POST",
            f"/audit-assignments/{assignment.id}/extend",
        ),
        assignment_id=assignment.id,
        ends_at=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        db=db_session,
    )
    db_session.expire_all()
    active = summarize_audit_findings(db_session.get(Batch, batch_id))

    assert expired.missing == 2
    assert expired.pending == 0
    assert response.status_code == 303
    assert active.missing == 0
    assert active.pending == 2


def test_auditor_cannot_create_assignment(db_session):
    auditor = User(username="self-assign-auditor", password_hash="x", role="auditor")
    db_session.add(auditor)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        assign_audit(
            signed_request(auditor.id, "POST"),
            product_id=1,
            auditor_id=auditor.id,
            starts_at="2026-07-05T09:00",
            ends_at="2026-07-05T17:00",
            db=db_session,
        )

    assert exc_info.value.status_code == 403


def test_auditor_cannot_extend_audit_deadline(db_session):
    manager = User(username="protect-admin", password_hash="x", role="admin")
    auditor = User(username="protect-auditor", password_hash="x", role="auditor")
    product = Product(
        product_code="PROTECT-EXT",
        product_name="Protected extension product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Protected extension product",
    )
    db_session.add_all([manager, auditor, product])
    db_session.commit()
    generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)
    now = datetime.now(_IST)
    assign_audit(
        signed_request(manager.id, "POST"),
        product_id=product.id,
        auditor_id=auditor.id,
        starts_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        ends_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        notes="",
        db=db_session,
    )
    assignment = db_session.scalar(select(AuditAssignment))

    with pytest.raises(HTTPException) as exc_info:
        extend_audit_deadline(
            signed_request(
                auditor.id,
                "POST",
                f"/audit-assignments/{assignment.id}/extend",
            ),
            assignment_id=assignment.id,
            ends_at=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            db=db_session,
        )

    assert exc_info.value.status_code == 403
