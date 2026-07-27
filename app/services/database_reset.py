from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AuditAssignment,
    AuditAssignmentItem,
    AuditFinding,
    Batch,
    BatchItem,
    ChangeAudit,
    Company,
    InventoryTransaction,
    LoginAudit,
    Product,
    RelocationSerial,
    Role,
    ScanLog,
    Serial,
    Setting,
    StockRelocation,
    StorageLocation,
    SyncAttempt,
    TallyLedgerCache,
    TallyMasterConfirmation,
    TallySalesVoucherCache,
    User,
    UserTallyAccess,
    has_role,
)
from app.services.settings import DEFAULT_SETTINGS


DELETE_ALL_MODELS = (
    AuditFinding,
    SyncAttempt,
    BatchItem,
    AuditAssignmentItem,
    RelocationSerial,
    InventoryTransaction,
    ScanLog,
    TallyMasterConfirmation,
    UserTallyAccess,
    TallySalesVoucherCache,
    TallyLedgerCache,
    StockRelocation,
    Batch,
    AuditAssignment,
    Serial,
    StorageLocation,
    Product,
    ChangeAudit,
    LoginAudit,
    Company,
    Setting,
)

SQLITE_RESET_DELETE_GUARDS = {
    "prevent_relocation_serials_delete": "relocation_serials",
    "prevent_stock_relocations_delete": "stock_relocations",
}


@dataclass(frozen=True)
class ResetSummary:
    deleted_rows: dict[str, int]
    preserved_super_admin_id: int


def clear_application_cache() -> None:
    get_settings.cache_clear()


def _sqlite_maintenance(bind) -> None:
    if getattr(bind.dialect, "name", "") != "sqlite":
        return
    engine = bind if isinstance(bind, Engine) else bind.engine
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            connection.execute(text("VACUUM"))
            connection.execute(text("PRAGMA shrink_memory"))
    except SQLAlchemyError:
        # The data reset has already committed. Cache/file compaction is best effort.
        pass


def _is_sqlite_session(db: Session) -> bool:
    return getattr(db.get_bind().dialect, "name", "") == "sqlite"


def _drop_sqlite_reset_delete_guards(db: Session) -> None:
    if not _is_sqlite_session(db):
        return
    for trigger_name in SQLITE_RESET_DELETE_GUARDS:
        db.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))


def _restore_sqlite_reset_delete_guards(db: Session) -> None:
    if not _is_sqlite_session(db):
        return
    for trigger_name, table_name in SQLITE_RESET_DELETE_GUARDS.items():
        db.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger_name}
                BEFORE DELETE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, 'Relocation history is permanent');
                END
                """
            )
        )


def reset_database_and_cache(db: Session, super_admin_id: int) -> ResetSummary:
    user = db.get(User, super_admin_id)
    if not user or not user.active or user.deleted_at or not has_role(user.role, Role.SUPER_ADMIN):
        raise ValueError("A live super admin account is required to reset the database.")

    deleted_rows: dict[str, int] = {}
    try:
        _drop_sqlite_reset_delete_guards(db)
        for model in DELETE_ALL_MODELS:
            result = db.execute(delete(model).execution_options(synchronize_session=False))
            deleted_rows[model.__tablename__] = int(result.rowcount or 0)

        result = db.execute(
            delete(User)
            .where(User.id != super_admin_id)
            .execution_options(synchronize_session=False)
        )
        deleted_rows[User.__tablename__] = int(result.rowcount or 0)
        db.flush()
        db.expunge_all()

        preserved = db.get(User, super_admin_id)
        if not preserved:
            raise ValueError("The super admin account could not be preserved.")
        preserved.active = True
        preserved.deleted_at = None

        for key, value in DEFAULT_SETTINGS.items():
            db.add(Setting(key=key, value=value))

        _restore_sqlite_reset_delete_guards(db)
        db.commit()
    except Exception:
        db.rollback()
        raise

    clear_application_cache()
    _sqlite_maintenance(db.get_bind())
    return ResetSummary(deleted_rows=deleted_rows, preserved_super_admin_id=super_admin_id)
