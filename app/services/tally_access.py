from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    Role,
    TallyLedgerCache,
    TallySalesVoucherCache,
    User,
    UserTallyAccess,
    has_role,
)
from app.services.tally_masters import TallyLedger, TallySalesVoucher


COMPANY_RESOURCE = "company"
LEDGER_RESOURCE = "ledger"
TALLY_USER_RESOURCE = "tally_user"


def resource_key(value: str) -> str:
    return value.strip().casefold()


def access_key(company_id: int, value: str) -> str:
    return f"{company_id}:{resource_key(value)}"


def _assignments(db: Session, user: User, resource_type: str) -> list[UserTallyAccess]:
    if has_role(user.role, Role.SUPER_ADMIN):
        return []
    return list(
        db.scalars(
            select(UserTallyAccess).where(
                UserTallyAccess.user_id == user.id,
                UserTallyAccess.resource_type == resource_type,
            )
        )
    )


def scoped_companies(db: Session, user: User) -> list[Company]:
    query = select(Company).order_by(Company.name)
    company_assignments = _assignments(db, user, COMPANY_RESOURCE)
    if company_assignments:
        query = query.where(
            Company.id.in_(tuple(row.company_id for row in company_assignments))
        )
    return list(db.scalars(query))


def can_access_company(db: Session, user: User, company_id: int) -> bool:
    company_assignments = _assignments(db, user, COMPANY_RESOURCE)
    return not company_assignments or company_id in {
        row.company_id for row in company_assignments
    }


def can_access_tally_company(
    db: Session,
    user: User,
    company: Company,
    tally_company: str,
) -> bool:
    company_assignments = _assignments(db, user, COMPANY_RESOURCE)
    if not company_assignments:
        return True
    return (
        company.id in {row.company_id for row in company_assignments}
        and bool(company.tally_company_name.strip())
        and resource_key(tally_company) == resource_key(company.tally_company_name)
    )


def filter_tally_company_names(
    db: Session,
    user: User,
    company: Company,
    names: Iterable[str],
) -> list[str]:
    company_assignments = _assignments(db, user, COMPANY_RESOURCE)
    available = list(names)
    if not company_assignments:
        return available
    if company.id not in {row.company_id for row in company_assignments}:
        return []
    configured_key = resource_key(company.tally_company_name)
    if not configured_key:
        return []
    return [name for name in available if resource_key(name) == configured_key]


def filter_ledgers(
    db: Session,
    user: User,
    company_id: int,
    ledgers: Iterable[TallyLedger],
) -> list[TallyLedger]:
    ledger_assignments = _assignments(db, user, LEDGER_RESOURCE)
    if not ledger_assignments:
        return list(ledgers)
    allowed = {
        row.resource_key
        for row in ledger_assignments
        if row.company_id == company_id
    }
    return [ledger for ledger in ledgers if resource_key(ledger.name) in allowed]


def filter_sales_vouchers(
    db: Session,
    user: User,
    company_id: int,
    vouchers: Iterable[TallySalesVoucher],
) -> list[TallySalesVoucher]:
    visible = list(vouchers)
    ledger_assignments = _assignments(db, user, LEDGER_RESOURCE)
    if ledger_assignments:
        allowed_ledgers = {
            row.resource_key
            for row in ledger_assignments
            if row.company_id == company_id
        }
        visible = [
            voucher
            for voucher in visible
            if resource_key(voucher.party_ledger) in allowed_ledgers
        ]

    tally_user_assignments = _assignments(db, user, TALLY_USER_RESOURCE)
    if tally_user_assignments:
        allowed_users = {
            row.resource_key
            for row in tally_user_assignments
            if row.company_id == company_id
        }
        visible = [
            voucher
            for voucher in visible
            if resource_key(voucher.tally_user) in allowed_users
        ]
    return visible


def allowed_ledger_names(db: Session, user: User, company_id: int) -> set[str] | None:
    ledger_assignments = _assignments(db, user, LEDGER_RESOURCE)
    if not ledger_assignments:
        return None
    return {
        row.resource_key
        for row in ledger_assignments
        if row.company_id == company_id
    }


def replace_user_access(
    db: Session,
    user: User,
    *,
    company_ids: Iterable[int],
    ledger_ids: Iterable[int],
    tally_user_values: Iterable[str],
    commit: bool = True,
) -> None:
    if has_role(user.role, Role.SUPER_ADMIN):
        raise ValueError("Super admins always have unrestricted Tally access.")

    valid_companies = {
        company.id: company
        for company in db.scalars(
            select(Company).where(Company.id.in_(tuple(set(company_ids))))
        )
    }
    selected_ledger_ids = tuple(set(ledger_ids))
    ledger_rows = list(
        db.scalars(
            select(TallyLedgerCache).where(TallyLedgerCache.id.in_(selected_ledger_ids))
        )
    ) if selected_ledger_ids else []

    user_rows: list[tuple[int, str]] = []
    for raw in tally_user_values:
        company_token, separator, username = raw.partition(":")
        if not separator or not username.strip():
            continue
        try:
            company_id = int(company_token)
        except ValueError:
            continue
        if db.get(Company, company_id) is not None:
            user_rows.append((company_id, username.strip()))

    db.execute(
        delete(UserTallyAccess).where(UserTallyAccess.user_id == user.id)
    )

    for company in valid_companies.values():
        db.add(
            UserTallyAccess(
                user_id=user.id,
                company_id=company.id,
                resource_type=COMPANY_RESOURCE,
                resource_key=str(company.id),
                resource_label=company.name,
            )
        )
    seen_ledgers: set[tuple[int, str]] = set()
    for ledger in ledger_rows:
        identity = (ledger.company_id, resource_key(ledger.name))
        if identity in seen_ledgers:
            continue
        seen_ledgers.add(identity)
        db.add(
            UserTallyAccess(
                user_id=user.id,
                company_id=ledger.company_id,
                resource_type=LEDGER_RESOURCE,
                resource_key=identity[1],
                resource_label=ledger.name,
            )
        )
    seen_users: set[tuple[int, str]] = set()
    for company_id, username in user_rows:
        identity = (company_id, resource_key(username))
        if identity in seen_users:
            continue
        seen_users.add(identity)
        db.add(
            UserTallyAccess(
                user_id=user.id,
                company_id=company_id,
                resource_type=TALLY_USER_RESOURCE,
                resource_key=identity[1],
                resource_label=username,
            )
        )
    if commit:
        db.commit()
    else:
        db.flush()


def user_access_snapshot(db: Session, user_id: int) -> dict[str, list[dict[str, object]]]:
    rows = db.scalars(
        select(UserTallyAccess)
        .where(UserTallyAccess.user_id == user_id)
        .order_by(
            UserTallyAccess.resource_type,
            UserTallyAccess.company_id,
            UserTallyAccess.resource_label,
        )
    ).all()
    return {
        "assignments": [
            {
                "company_id": row.company_id,
                "resource_type": row.resource_type,
                "resource": row.resource_label,
            }
            for row in rows
        ]
    }


def access_page_data(db: Session, users: Iterable[User]) -> dict[str, object]:
    access_rows = list(
        db.scalars(
            select(UserTallyAccess).order_by(
                UserTallyAccess.user_id,
                UserTallyAccess.resource_type,
                UserTallyAccess.resource_label,
            )
        )
    )
    rows_by_user: dict[int, list[UserTallyAccess]] = {}
    for row in access_rows:
        rows_by_user.setdefault(row.user_id, []).append(row)

    user_access: dict[int, dict[str, object]] = {}
    for user in users:
        assignments = rows_by_user.get(user.id, [])
        counts = {
            resource_type: sum(
                row.resource_type == resource_type for row in assignments
            )
            for resource_type in (
                COMPANY_RESOURCE,
                LEDGER_RESOURCE,
                TALLY_USER_RESOURCE,
            )
        }
        labels: list[str] = []
        if counts[COMPANY_RESOURCE]:
            labels.append(
                f"{counts[COMPANY_RESOURCE]} compan"
                f"{'y' if counts[COMPANY_RESOURCE] == 1 else 'ies'}"
            )
        if counts[LEDGER_RESOURCE]:
            labels.append(
                f"{counts[LEDGER_RESOURCE]} ledger"
                f"{'s' if counts[LEDGER_RESOURCE] != 1 else ''}"
            )
        if counts[TALLY_USER_RESOURCE]:
            labels.append(
                f"{counts[TALLY_USER_RESOURCE]} Tally user"
                f"{'s' if counts[TALLY_USER_RESOURCE] != 1 else ''}"
            )
        if has_role(user.role, Role.SUPER_ADMIN):
            summary = "All Tally data (super admin)"
        else:
            summary = " · ".join(labels) if labels else "All Tally data"
        user_access[user.id] = {
            "summary": summary,
            "company_ids": [
                row.company_id
                for row in assignments
                if row.resource_type == COMPANY_RESOURCE
            ],
            "ledger_keys": [
                access_key(row.company_id, row.resource_key)
                for row in assignments
                if row.resource_type == LEDGER_RESOURCE
            ],
            "tally_user_keys": [
                access_key(row.company_id, row.resource_key)
                for row in assignments
                if row.resource_type == TALLY_USER_RESOURCE
            ],
            "tally_user_values": [
                f"{row.company_id}:{row.resource_label}"
                for row in assignments
                if row.resource_type == TALLY_USER_RESOURCE
            ],
        }

    ledger_options = list(
        db.scalars(
            select(TallyLedgerCache).order_by(
                TallyLedgerCache.company_id,
                TallyLedgerCache.name,
            )
        )
    )
    tally_user_rows = db.execute(
        select(
            TallySalesVoucherCache.company_id,
            TallySalesVoucherCache.tally_user,
        )
        .where(TallySalesVoucherCache.tally_user != "")
        .distinct()
        .order_by(
            TallySalesVoucherCache.company_id,
            TallySalesVoucherCache.tally_user,
        )
    ).all()
    return {
        "user_access": user_access,
        "ledger_options": ledger_options,
        "tally_user_options": [
            {
                "company_id": company_id,
                "username": username,
                "key": access_key(company_id, username),
                "value": f"{company_id}:{username}",
            }
            for company_id, username in tally_user_rows
        ],
    }
