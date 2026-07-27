from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, User, has_any_role, normalize_role_values
from app.security import read_session_token
from app.services.access_control import configured_role_has_access, get_role_access_config

SESSION_COOKIE = "setuora_session"


def redirect_exception(url: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": url})


def current_user(request: Request, db: Session) -> User | None:
    user_id = read_session_token(request.cookies.get(SESSION_COOKIE))
    if not user_id:
        return None
    user = db.get(User, user_id)
    if not user or not user.active or user.deleted_at:
        return None
    request.state.session_user_id = user.id
    return user


PASSWORD_CHANGE_PATH = "/account/password"
_PASSWORD_GATE_EXEMPT = {PASSWORD_CHANGE_PATH, "/logout"}


def require_user(request: Request, db: Session, roles: set[Role] | None = None) -> User:
    user = current_user(request, db)
    if not user:
        raise redirect_exception("/login")
    if user.must_change_password and request.url.path not in _PASSWORD_GATE_EXEMPT:
        raise redirect_exception(PASSWORD_CHANGE_PATH)
    if not normalize_role_values(user.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    if roles and not has_any_role(user.role, roles):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    user._access_config = get_role_access_config(db)
    return user


def require_permission(request: Request, db: Session, access_key: str, allowed_values: set[str] | None = None) -> User:
    user = require_user(request, db)
    if not configured_role_has_access(user._access_config, user.role, access_key, allowed_values):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    return user


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == username.strip().lower()))


ADMIN_ROLES = {Role.ADMIN, Role.SUPER_ADMIN}
PURCHASE_ROLES = {Role.PURCHASE, Role.ADMIN, Role.SUPER_ADMIN}
SALES_ROLES = {Role.SALES, Role.ADMIN, Role.SUPER_ADMIN}
AUDIT_ROLES = {Role.AUDITOR, Role.ADMIN, Role.SUPER_ADMIN}
