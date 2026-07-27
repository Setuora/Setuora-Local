from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import INSECURE_BOOTSTRAP_PASSWORDS, get_settings
from app.models import Role, User, has_role
from app.security import MIN_PASSWORD_LENGTH, hash_password, verify_password
from app.services.settings import clear_legacy_placeholder_settings, ensure_company_records, ensure_default_settings

DEFAULT_ADMIN_PASSWORD = "admin123"


def _validate_first_admin_settings() -> None:
    settings = get_settings()
    username = settings.bootstrap_admin_username.strip()
    password = settings.bootstrap_admin_password
    if not username:
        raise RuntimeError("BOOTSTRAP_ADMIN_USERNAME must be set before the first startup.")
    if password in INSECURE_BOOTSTRAP_PASSWORDS or len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(
            "Set BOOTSTRAP_ADMIN_PASSWORD to a unique password of at least "
            f"{MIN_PASSWORD_LENGTH} characters before the first startup."
        )


def _flag_default_password_admins(db: Session) -> None:
    admins = [
        user
        for user in db.scalars(select(User).where(User.deleted_at.is_(None))).all()
        if has_role(user.role, Role.SUPER_ADMIN)
    ]
    changed = False
    for admin in admins:
        if not admin.must_change_password and verify_password(DEFAULT_ADMIN_PASSWORD, admin.password_hash):
            admin.must_change_password = True
            changed = True
    if changed:
        db.commit()


def bootstrap(db: Session) -> None:
    ensure_default_settings(db)
    clear_legacy_placeholder_settings(db)
    ensure_company_records(db)
    if db.scalar(select(User.id).limit(1)):
        _flag_default_password_admins(db)
        return
    _validate_first_admin_settings()
    settings = get_settings()
    using_default_password = settings.bootstrap_admin_password == DEFAULT_ADMIN_PASSWORD
    db.add(
        User(
            username=settings.bootstrap_admin_username.strip().lower(),
            password_hash=hash_password(settings.bootstrap_admin_password),
            role=Role.SUPER_ADMIN.value,
            active=True,
            must_change_password=using_default_password,
        )
    )
    db.commit()
