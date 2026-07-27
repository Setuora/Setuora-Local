from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_permission, require_user
from app.database import get_db
from app.models import Company, Role
from app.services.access_control import ROLE_COLUMNS, config_from_form, role_access_sections, save_role_access_config
from app.services.change_audit import record_change
from app.services.settings import (
    DEFAULT_SETTINGS,
    activate_company,
    add_company,
    company_config,
    delete_company,
    get_active_company,
    get_all_settings,
    list_companies,
    parse_sales_gst_ledger_mappings,
    persist_settings_and_active_company,
)
from app.services.tally_masters import live_sync_readiness
from app.templates import templates

router = APIRouter(prefix="/settings")


def settings_snapshot(values: dict[str, str], keys) -> dict[str, str]:
    return {key: values.get(key, "") for key in keys}


def company_snapshot(company: Company | None) -> dict[str, object] | None:
    if not company:
        return None
    return {
        "id": company.id,
        "name": company.name,
        "is_active": company.is_active,
        "config": company_config(company),
    }


def validate_settings(requested: dict[str, str]) -> str | None:
    if not requested["company_name"]:
        return "Company name is required."
    if not requested["tally_host"]:
        return "Tally host is required."
    port = requested["tally_port"]
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        return "Tally port must be a whole number between 1 and 65535."
    interval = requested["retry_interval_seconds"]
    if not interval.isdigit() or not (30 <= int(interval) <= 86400):
        return "Retry interval must be a whole number of seconds between 30 and 86400."
    try:
        parse_sales_gst_ledger_mappings(requested.get("sales_gst_ledger_mappings"))
    except ValueError as exc:
        return str(exc)
    return None


def render_settings(request: Request, db: Session, *, settings: dict | None = None, error: str | None = None, status_code: int = 200, open_settings: bool = False):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "user": require_permission(request, db, "settings_edit"),
            "settings": settings if settings is not None else get_all_settings(db),
            "keys": DEFAULT_SETTINGS.keys(),
            "companies": list_companies(db),
            "active": get_active_company(db),
            "error": error,
            "open_settings": open_settings,
        },
        status_code=status_code,
    )


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "settings_edit")
    return render_settings(request, db)


@router.get("/access")
def access_overview(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, {Role.SUPER_ADMIN})
    return templates.TemplateResponse(
        request,
        "role_access.html",
        {
            "request": request,
            "user": user,
            "roles": ROLE_COLUMNS,
            "sections": role_access_sections(db),
        },
    )


@router.post("/access")
async def save_access_overview(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, {Role.SUPER_ADMIN})
    form = await request.form()
    before = get_all_settings(db).get("role_access_config", "")
    try:
        save_role_access_config(db, config_from_form(form.multi_items()), commit=False)
        after = get_all_settings(db).get("role_access_config", "")
        record_change(
            db,
            user,
            entity_type="settings",
            entity_id="role_access_config",
            action="update",
            before={"role_access_config": before},
            after={"role_access_config": after},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return RedirectResponse("/settings/access", status_code=303)


@router.post("")
def save_settings(
    request: Request,
    company_name: str = Form(...),
    tally_enabled: str = Form("false"),
    tally_host: str = Form(...),
    tally_port: str = Form(...),
    sales_voucher_type: str | None = Form(None),
    purchase_voucher_type: str | None = Form(None),
    sales_ledger_name: str | None = Form(None),
    purchase_ledger_name: str | None = Form(None),
    cgst_ledger_name: str | None = Form(None),
    sgst_ledger_name: str | None = Form(None),
    sales_gst_ledger_mappings: str = Form(""),
    round_off_ledger_name: str = Form(...),
    retry_interval_seconds: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "settings_edit")
    current_settings = get_all_settings(db)
    requested = {
        "company_name": company_name.strip(),
        "tally_enabled": "true" if tally_enabled == "true" else "false",
        "tally_host": tally_host.strip(),
        "tally_port": tally_port.strip(),
        "sales_voucher_type": current_settings["sales_voucher_type"] if sales_voucher_type is None else sales_voucher_type.strip(),
        "purchase_voucher_type": current_settings["purchase_voucher_type"] if purchase_voucher_type is None else purchase_voucher_type.strip(),
        "sales_ledger_name": current_settings["sales_ledger_name"] if sales_ledger_name is None else sales_ledger_name.strip(),
        "purchase_ledger_name": current_settings["purchase_ledger_name"] if purchase_ledger_name is None else purchase_ledger_name.strip(),
        "cgst_ledger_name": current_settings["cgst_ledger_name"] if cgst_ledger_name is None else cgst_ledger_name.strip(),
        "sgst_ledger_name": current_settings["sgst_ledger_name"] if sgst_ledger_name is None else sgst_ledger_name.strip(),
        "sales_gst_ledger_mappings": sales_gst_ledger_mappings.strip(),
        "round_off_ledger_name": round_off_ledger_name.strip(),
        "retry_interval_seconds": retry_interval_seconds.strip(),
    }

    validation_error = validate_settings(requested)
    if validation_error:
        settings = {**current_settings, **requested}
        settings["tally_enabled"] = current_settings.get("tally_enabled", "false")
        return render_settings(request, db, settings=settings, error=validation_error, status_code=400, open_settings=True)

    if requested["tally_enabled"] == "true":
        try:
            persist_settings_and_active_company(db, requested, commit=False)
            db.flush()
            ready, counts = live_sync_readiness(db)
        except Exception:
            db.rollback()
            raise
        if not ready:
            db.rollback()
            settings = {**current_settings, **requested}
            settings["tally_enabled"] = current_settings.get("tally_enabled", "false")
            return render_settings(
                request,
                db,
                settings=settings,
                error=f"Complete Tally Check before enabling sync. Missing: {counts['missing']}, unchecked: {counts['unchecked']}.",
                status_code=400,
                open_settings=True,
            )

    try:
        if requested["tally_enabled"] != "true":
            persist_settings_and_active_company(db, requested, commit=False)
        record_change(
            db,
            user,
            entity_type="settings",
            entity_id="global",
            action="update",
            before=settings_snapshot(current_settings, requested.keys()),
            after=settings_snapshot(requested, requested.keys()),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return RedirectResponse("/settings", status_code=303)


@router.post("/autosave")
def autosave_settings(
    request: Request,
    company_name: str = Form(...),
    tally_host: str = Form(...),
    tally_port: str = Form(...),
    sales_voucher_type: str | None = Form(None),
    purchase_voucher_type: str | None = Form(None),
    sales_ledger_name: str | None = Form(None),
    purchase_ledger_name: str | None = Form(None),
    cgst_ledger_name: str | None = Form(None),
    sgst_ledger_name: str | None = Form(None),
    sales_gst_ledger_mappings: str = Form(""),
    round_off_ledger_name: str = Form(...),
    retry_interval_seconds: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "settings_edit")
    before_settings = get_all_settings(db)
    requested = {
        "company_name": company_name.strip(),
        "tally_host": tally_host.strip(),
        "tally_port": tally_port.strip(),
        "sales_voucher_type": before_settings["sales_voucher_type"] if sales_voucher_type is None else sales_voucher_type.strip(),
        "purchase_voucher_type": before_settings["purchase_voucher_type"] if purchase_voucher_type is None else purchase_voucher_type.strip(),
        "sales_ledger_name": before_settings["sales_ledger_name"] if sales_ledger_name is None else sales_ledger_name.strip(),
        "purchase_ledger_name": before_settings["purchase_ledger_name"] if purchase_ledger_name is None else purchase_ledger_name.strip(),
        "cgst_ledger_name": before_settings["cgst_ledger_name"] if cgst_ledger_name is None else cgst_ledger_name.strip(),
        "sgst_ledger_name": before_settings["sgst_ledger_name"] if sgst_ledger_name is None else sgst_ledger_name.strip(),
        "sales_gst_ledger_mappings": sales_gst_ledger_mappings.strip(),
        "round_off_ledger_name": round_off_ledger_name.strip(),
        "retry_interval_seconds": retry_interval_seconds.strip(),
    }
    error = validate_settings(requested)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    try:
        persist_settings_and_active_company(db, requested, commit=False)
        record_change(
            db,
            user,
            entity_type="settings",
            entity_id="global",
            action="autosave",
            before=settings_snapshot(before_settings, requested.keys()),
            after=settings_snapshot(requested, requested.keys()),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return JSONResponse({"ok": True})


@router.post("/companies")
def create_company(
    request: Request,
    name: str = Form(""),
    company_name: str = Form(...),
    tally_host: str = Form(...),
    tally_port: str = Form(...),
    sales_gst_ledger_mappings: str = Form(""),
    round_off_ledger_name: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "settings_edit")
    current_settings = get_all_settings(db)
    config = {
        "company_name": company_name,
        "tally_host": tally_host,
        "tally_port": tally_port,
        "sales_voucher_type": current_settings["sales_voucher_type"],
        "purchase_voucher_type": current_settings["purchase_voucher_type"],
        "sales_ledger_name": current_settings["sales_ledger_name"],
        "purchase_ledger_name": current_settings["purchase_ledger_name"],
        "cgst_ledger_name": current_settings["cgst_ledger_name"],
        "sgst_ledger_name": current_settings["sgst_ledger_name"],
        "sales_gst_ledger_mappings": sales_gst_ledger_mappings,
        "round_off_ledger_name": round_off_ledger_name,
    }
    try:
        company = add_company(db, name, config, commit=False)
        record_change(
            db,
            user,
            entity_type="company",
            entity_id=company.id,
            action="create",
            before=None,
            after=company_snapshot(company),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return render_settings(request, db, error=str(exc), status_code=400)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse("/settings", status_code=303)


@router.post("/companies/{company_id}/activate")
def activate(request: Request, company_id: int, db: Session = Depends(get_db)):
    user = require_permission(request, db, "settings_edit")
    before_active = company_snapshot(get_active_company(db))
    try:
        activate_company(db, company_id, commit=False)
        after_active = company_snapshot(db.get(Company, company_id))
        record_change(
            db,
            user,
            entity_type="company",
            entity_id=company_id,
            action="activate",
            before={"active_company": before_active},
            after={"active_company": after_active},
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return render_settings(request, db, error=str(exc), status_code=400)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse("/settings", status_code=303)


@router.post("/companies/{company_id}/delete")
def remove_company(request: Request, company_id: int, db: Session = Depends(get_db)):
    user = require_permission(request, db, "settings_edit")
    before = company_snapshot(db.get(Company, company_id))
    try:
        delete_company(db, company_id, commit=False)
        record_change(
            db,
            user,
            entity_type="company",
            entity_id=company_id,
            action="delete",
            before=before,
            after=None,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return render_settings(request, db, error=str(exc), status_code=400)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse("/settings", status_code=303)
