from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import current_user
from app.database import get_db
from app.security import MIN_PASSWORD_LENGTH, hash_password, verify_password
from app.services.access_control import get_role_access_config, landing_path_for
from app.templates import templates

router = APIRouter()

@router.get("/account/password")
def change_password_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {"request": request, "user": user, "error": None, "forced": user.must_change_password},
    )


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    def fail(message: str):
        return templates.TemplateResponse(
            request,
            "account_password.html",
            {"request": request, "user": user, "error": message, "forced": user.must_change_password},
            status_code=400,
        )

    if not verify_password(current_password, user.password_hash):
        return fail("Current password is incorrect.")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return fail(f"New password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if new_password != confirm_password:
        return fail("New password and confirmation do not match.")
    if verify_password(new_password, user.password_hash):
        return fail("New password must be different from the current one.")

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(landing_path_for(get_role_access_config(db), user.role), status_code=303)
