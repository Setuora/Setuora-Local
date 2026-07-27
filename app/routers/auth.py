from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, current_user, get_user_by_username
from app.config import get_settings
from app.database import get_db
from app.models import LoginAudit
from app.security import create_session_token, hash_password, verify_password
from app.services.access_control import get_role_access_config, landing_path_for
from app.templates import templates

router = APIRouter()

# Keeps missing/inactive usernames on the normal password-verify path.
_DUMMY_PASSWORD_HASH = hash_password("setuora-dummy-password-never-matches")


def recent_failed_logins(db: Session, username: str, window_minutes: int) -> int:
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    return db.scalar(
        select(func.count(LoginAudit.id)).where(
            LoginAudit.username == username,
            LoginAudit.success.is_(False),
            LoginAudit.created_at >= since,
        )
    ) or 0


@router.get("/login")
def login_page(request: Request, restored: str = "", db: Session = Depends(get_db)):
    user = current_user(request, db)
    if user:
        destination = "/account/password" if user.must_change_password else landing_path_for(get_role_access_config(db), user.role)
        return RedirectResponse(destination, status_code=303)
    message = "Backup import completed. Sign in with an account from the restored backup." if restored else None
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None, "message": message})


@router.post("/login")
def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    normalized = username.strip().lower()

    if recent_failed_logins(db, normalized, settings.login_lockout_minutes) >= settings.login_max_attempts:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "error": f"Too many failed attempts. Try again in about {settings.login_lockout_minutes} minutes.",
                "message": None,
            },
            status_code=429,
        )

    user = get_user_by_username(db, normalized)
    if user and user.active:
        ok = verify_password(password, user.password_hash)
    else:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        ok = False
    db.add(
        LoginAudit(
            username=normalized,
            success=ok,
            ip_address=request.client.host if request.client else None,
            message="OK" if ok else "Invalid username or password",
        )
    )
    if not ok:
        db.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid username or password", "message": None},
            status_code=400,
        )
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    destination = "/account/password" if user.must_change_password else landing_path_for(get_role_access_config(db), user.role)
    redirect = RedirectResponse(destination, status_code=303)
    redirect.set_cookie(
        SESSION_COOKIE,
        create_session_token(user.id),
        max_age=settings.session_timeout_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return redirect


@router.post("/logout")
def logout():
    settings = get_settings()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response
