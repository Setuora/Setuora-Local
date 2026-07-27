import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, require_permission, require_user
from app.config import get_settings
from app.database import get_db
from app.models import LoginAudit, Role
from app.security import verify_password
from app.services.backup import (
    backup_choice_path,
    backup_status,
    create_sqlite_backup,
    restore_sqlite_backup_file,
    sqlite_database_path,
    update_backup_settings,
)
from app.services.backup_worker import start_backup_worker, stop_backup_worker
from app.services.database_reset import reset_database_and_cache
from app.services.sync_worker import start_retry_worker, stop_retry_worker
from app.templates import templates

router = APIRouter(prefix="/maintenance")
logger = logging.getLogger("setuora")
MAX_BACKUP_UPLOAD_BYTES = 512 * 1024 * 1024


@router.get("")
def maintenance_page(request: Request, error: str = "", success: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "backup_data")
    error_message = {
        "bad_password": "Password was incorrect. Database was not reset.",
        "confirm_required": "Type RESET to confirm the database reset.",
        "restore_bad_password": "Password was incorrect. Backup was not imported.",
        "restore_confirm_required": "Type IMPORT to confirm the backup import.",
        "restore_file_required": "Choose a backup file to import.",
        "restore_too_large": "Backup file is too large to import from the browser.",
        "restore_failed": "Backup import failed. Choose a verified Setuora backup file.",
        "backup_failed": "Backup could not be created. Check the database file and try again.",
        "reset_failed": "Database reset failed. No data was cleared. Check the server logs and try again.",
    }.get(error, error)
    success_message = {
        "database_reset": "Database and cache reset completed.",
        "backup_settings": "Backup settings saved.",
    }.get(success, success)
    return templates.TemplateResponse(
        request,
        "maintenance.html",
        {
            "request": request,
            "user": user,
            "database_path": sqlite_database_path(),
            "backup_status": backup_status(),
            "backup_config": get_settings(),
            "error": error_message or None,
            "success": success_message or None,
        },
    )


@router.post("/backup-settings")
async def save_backup_settings(
    request: Request,
    automatic_backups_enabled: str = Form("false"),
    backup_directory: str = Form(...),
    backup_interval_hours: str = Form(...),
    backup_retention_count: str = Form(...),
    backup_offsite_directory: str = Form(""),
    db: Session = Depends(get_db),
):
    require_user(request, db, {Role.SUPER_ADMIN})
    try:
        status = update_backup_settings(
            enabled=automatic_backups_enabled == "true",
            backup_directory=backup_directory,
            interval_hours=backup_interval_hours,
            retention_count=backup_retention_count,
            offsite_directory=backup_offsite_directory,
        )
    except ValueError as exc:
        return RedirectResponse(f"/maintenance?error={quote(str(exc))}", status_code=303)

    await stop_backup_worker(request.app)
    if status.enabled:
        start_backup_worker(request.app)
    return RedirectResponse("/maintenance?success=backup_settings", status_code=303)


@router.get("/backup.db")
def download_backup(request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "backup_download")
    try:
        backup = create_sqlite_backup()
    except Exception:
        logger.exception("Backup download failed")
        return RedirectResponse("/maintenance?error=backup_failed", status_code=303)
    return Response(
        backup.data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={backup.filename}"},
    )


@router.post("/restore-existing")
async def restore_existing_backup(
    request: Request,
    selected_backup: str = Form(...),
    super_admin_password: str = Form(...),
    confirm_restore: str = Form(...),
    db: Session = Depends(get_db),
):
    authorization = _require_restore_authorization(request, db, super_admin_password, confirm_restore)
    if isinstance(authorization, RedirectResponse):
        return authorization
    try:
        backup_path = backup_choice_path(selected_backup)
    except RuntimeError:
        return RedirectResponse("/maintenance?error=restore_failed", status_code=303)
    return await _restore_from_path(request, db, backup_path)


@router.post("/restore-upload")
async def restore_uploaded_backup(
    request: Request,
    upload: UploadFile = File(...),
    super_admin_password: str = Form(...),
    confirm_restore: str = Form(...),
    db: Session = Depends(get_db),
):
    authorization = _require_restore_authorization(request, db, super_admin_password, confirm_restore)
    if isinstance(authorization, RedirectResponse):
        return authorization
    data = upload.file.read(MAX_BACKUP_UPLOAD_BYTES + 1)
    if not data:
        return RedirectResponse("/maintenance?error=restore_file_required", status_code=303)
    if len(data) > MAX_BACKUP_UPLOAD_BYTES:
        return RedirectResponse("/maintenance?error=restore_too_large", status_code=303)

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir) / "uploaded-setuora-backup.db"
        temp_path.write_bytes(data)
        return await _restore_from_path(request, db, temp_path)


@router.post("/reset")
async def reset_database(
    request: Request,
    super_admin_password: str = Form(...),
    confirm_reset: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_user(request, db, {Role.SUPER_ADMIN})
    if confirm_reset.strip() != "RESET":
        return RedirectResponse("/maintenance?error=confirm_required", status_code=303)
    if not verify_password(super_admin_password, user.password_hash):
        db.add(
            LoginAudit(
                username=user.username,
                success=False,
                ip_address=request.client.host if request.client else None,
                message="Database reset password verification failed",
            )
        )
        db.commit()
        return RedirectResponse("/maintenance?error=bad_password", status_code=303)

    await _stop_maintenance_workers(request)
    try:
        reset_database_and_cache(db, user.id)
    except Exception:
        logger.exception("Database reset failed")
        return RedirectResponse("/maintenance?error=reset_failed", status_code=303)
    finally:
        _start_maintenance_workers(request)
    return RedirectResponse("/maintenance?success=database_reset", status_code=303)


def _require_restore_authorization(request: Request, db: Session, password: str, confirmation: str):
    user = require_user(request, db, {Role.SUPER_ADMIN})
    if confirmation.strip() != "IMPORT":
        return RedirectResponse("/maintenance?error=restore_confirm_required", status_code=303)
    if not verify_password(password, user.password_hash):
        db.add(
            LoginAudit(
                username=user.username,
                success=False,
                ip_address=request.client.host if request.client else None,
                message="Database restore password verification failed",
            )
        )
        db.commit()
        return RedirectResponse("/maintenance?error=restore_bad_password", status_code=303)
    return user


async def _restore_from_path(request: Request, db: Session, backup_path: Path):
    db.close()
    await _stop_maintenance_workers(request)
    try:
        await asyncio.to_thread(restore_sqlite_backup_file, backup_path)
    except Exception:
        logger.exception("Backup import failed")
        return RedirectResponse("/maintenance?error=restore_failed", status_code=303)
    finally:
        _start_maintenance_workers(request)

    request.state.session_user_id = None
    settings = get_settings()
    response = RedirectResponse("/login?restored=1", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


async def _stop_maintenance_workers(request: Request) -> None:
    await stop_retry_worker(request.app)
    await stop_backup_worker(request.app)


def _start_maintenance_workers(request: Request) -> None:
    start_retry_worker(request.app)
    start_backup_worker(request.app)
