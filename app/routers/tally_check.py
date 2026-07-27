from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_permission
from app.database import get_db
from app.models import Company
from app.services.access_control import role_has_access
from app.services.change_audit import record_change
from app.services.settings import (
    company_config,
    get_active_company,
    get_all_settings,
    update_company,
)
from app.services.tally_access import (
    can_access_company,
    can_access_tally_company,
    filter_ledgers,
    filter_sales_vouchers,
    filter_tally_company_names,
    scoped_companies,
)
from app.services.tally_cache import (
    cached_ledgers,
    cached_sales_book,
    latest_cache_refresh,
    replace_cached_ledgers,
    replace_cached_sales_book,
)
from app.services.tally_masters import (
    collect_master_requirements,
    confirmation_lookup,
    confirm_master,
    fetch_tally_companies,
    fetch_tally_ledgers,
    fetch_tally_sales_book,
    readiness_counts,
    remove_confirmation,
    TallyDataError,
    test_tally_gateway,
)
from app.templates import templates

router = APIRouter(prefix="/tally-check")


def company_snapshot(company: Company | None) -> dict[str, object] | None:
    if not company:
        return None
    return {
        "id": company.id,
        "name": company.name,
        "is_active": company.is_active,
        "config": company_config(company),
    }


def render_check_page(
    request: Request,
    db: Session,
    result=None,
    open_company_id: int | None = None,
):
    user = require_permission(request, db, "tally_check_edit")
    requirements = collect_master_requirements(db)
    confirmations = confirmation_lookup(db)
    companies = scoped_companies(db, user)
    active = get_active_company(db)
    if active and active.id not in {company.id for company in companies}:
        active = None
    if open_company_id not in {company.id for company in companies}:
        open_company_id = None
    today = date.today()
    financial_year_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    return templates.TemplateResponse(
        request,
        "tally_check.html",
        {
            "request": request,
            "user": user,
            "requirements": requirements,
            "confirmations": confirmations,
            "counts": readiness_counts(requirements, confirmations),
            "settings": get_all_settings(db),
            "result": result,
            "companies": [
                {"company": company, "config": company_config(company)}
                for company in companies
            ],
            "active": active,
            "can_edit_companies": role_has_access(db, user.role, "settings_edit"),
            "live_sales_from": financial_year_start.isoformat(),
            "live_sales_to": today.isoformat(),
            "open_company_id": (
                open_company_id
                or (active.id if result is not None and active is not None else None)
            ),
        },
    )


def _live_company_config(db: Session, company_id: int) -> tuple[Company | None, dict[str, str] | None]:
    company = db.get(Company, company_id)
    return company, company_config(company) if company else None


def _scoped_live_company_config(
    db: Session,
    user,
    company_id: int,
) -> tuple[Company | None, dict[str, str] | None]:
    if not can_access_company(db, user, company_id):
        return None, None
    return _live_company_config(db, company_id)


def _live_error(message: str, status_code: int = 502) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


@router.get("")
def tally_check_page(
    request: Request,
    company: int | None = None,
    db: Session = Depends(get_db),
):
    return render_check_page(request, db, open_company_id=company)


@router.post("/companies/{company_id}")
def save_company(
    request: Request,
    company_id: int,
    name: str = Form(...),
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
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "settings_edit")
    if not can_access_company(db, user, company_id):
        return JSONResponse(
            {"ok": False, "error": "This company is not assigned to your account."},
            status_code=403,
        )
    company = db.get(Company, company_id)
    before = company_snapshot(company)
    current_config = company_config(company) if company else {}
    config = {
        "company_name": company_name,
        "tally_host": tally_host,
        "tally_port": tally_port,
        "sales_voucher_type": current_config.get("sales_voucher_type", "") if sales_voucher_type is None else sales_voucher_type,
        "purchase_voucher_type": current_config.get("purchase_voucher_type", "") if purchase_voucher_type is None else purchase_voucher_type,
        "sales_ledger_name": current_config.get("sales_ledger_name", "") if sales_ledger_name is None else sales_ledger_name,
        "purchase_ledger_name": current_config.get("purchase_ledger_name", "") if purchase_ledger_name is None else purchase_ledger_name,
        "cgst_ledger_name": current_config.get("cgst_ledger_name", "") if cgst_ledger_name is None else cgst_ledger_name,
        "sgst_ledger_name": current_config.get("sgst_ledger_name", "") if sgst_ledger_name is None else sgst_ledger_name,
        "sales_gst_ledger_mappings": sales_gst_ledger_mappings,
        "round_off_ledger_name": round_off_ledger_name,
    }
    try:
        company = update_company(db, company_id, name, config, commit=False)
        record_change(
            db,
            user,
            entity_type="company",
            entity_id=company.id,
            action="update",
            before=before,
            after=company_snapshot(company),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        db.rollback()
        raise
    return JSONResponse(
        {
            "ok": True,
            "company": {
                "id": company.id,
                "name": company.name,
                "tally_company_name": company.tally_company_name,
            },
        }
    )


@router.post("/confirm")
def confirm(
    request: Request,
    master_type: str = Form(...),
    master_name: str = Form(...),
    source: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "tally_check_edit")
    confirm_master(db, user, master_type, master_name, source, notes)
    active = get_active_company(db)
    target = f"/tally-check?company={active.id}" if active else "/tally-check"
    return RedirectResponse(target, status_code=303)


@router.post("/unconfirm")
def unconfirm(
    request: Request,
    master_type: str = Form(...),
    master_name: str = Form(...),
    db: Session = Depends(get_db),
):
    require_permission(request, db, "tally_check_edit")
    remove_confirmation(db, master_type, master_name)
    active = get_active_company(db)
    target = f"/tally-check?company={active.id}" if active else "/tally-check"
    return RedirectResponse(target, status_code=303)


@router.post("/test-gateway")
def test_gateway(request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "tally_check_edit")
    result = test_tally_gateway(get_all_settings(db))
    active = get_active_company(db)
    return render_check_page(
        request,
        db,
        result,
        open_company_id=active.id if active else None,
    )


@router.get("/companies/{company_id}/live/companies")
def live_companies(
    request: Request,
    company_id: int,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "tally_check_edit")
    company, config = _scoped_live_company_config(db, user, company_id)
    if not company or config is None:
        return _live_error("Company profile not found or not assigned to your account.", 404)
    try:
        names = fetch_tally_companies(config)
    except TallyDataError as exc:
        return _live_error(str(exc))
    names = filter_tally_company_names(db, user, company, names)
    return JSONResponse(
        {
            "ok": True,
            "profile": {"id": company.id, "name": company.name},
            "selected_company": config.get("company_name", ""),
            "companies": names,
        }
    )


@router.get("/companies/{company_id}/live/ledgers")
def live_ledgers(
    request: Request,
    company_id: int,
    tally_company: str,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "tally_check_edit")
    company, config = _scoped_live_company_config(db, user, company_id)
    if not company or config is None:
        return _live_error("Company profile not found or not assigned to your account.", 404)
    if not can_access_tally_company(db, user, company, tally_company):
        return _live_error("This Tally company is not assigned to your account.", 403)
    try:
        ledgers = fetch_tally_ledgers(config, tally_company)
    except TallyDataError as exc:
        return _live_error(str(exc))
    replace_cached_ledgers(db, company.id, tally_company, ledgers)
    visible_ledgers = filter_ledgers(db, user, company.id, ledgers)
    return JSONResponse(
        {
            "ok": True,
            "company": tally_company.strip(),
            "count": len(visible_ledgers),
            "ledgers": [asdict(ledger) for ledger in visible_ledgers],
        }
    )


@router.get("/companies/{company_id}/live/sales-book")
def live_sales_book(
    request: Request,
    company_id: int,
    tally_company: str,
    from_date: date,
    to_date: date,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "tally_check_edit")
    company, config = _scoped_live_company_config(db, user, company_id)
    if not company or config is None:
        return _live_error("Company profile not found or not assigned to your account.", 404)
    if not can_access_tally_company(db, user, company, tally_company):
        return _live_error("This Tally company is not assigned to your account.", 403)
    if from_date > to_date:
        return _live_error("Sales book start date must be on or before the end date.", 400)
    if (to_date - from_date).days > 370:
        return _live_error("Choose a sales book period of 370 days or less.", 400)
    try:
        vouchers = fetch_tally_sales_book(config, tally_company, from_date, to_date)
    except TallyDataError as exc:
        return _live_error(str(exc))
    replace_cached_sales_book(
        db,
        company.id,
        tally_company,
        from_date,
        to_date,
        vouchers,
    )
    visible_vouchers = filter_sales_vouchers(db, user, company.id, vouchers)
    return JSONResponse(
        {
            "ok": True,
            "company": tally_company.strip(),
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "count": len(visible_vouchers),
            "vouchers": [asdict(voucher) for voucher in visible_vouchers],
        }
    )


@router.get("/companies/{company_id}/cached")
def cached_tally_data(
    request: Request,
    company_id: int,
    tally_company: str,
    from_date: date,
    to_date: date,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "tally_check_edit")
    company, _config = _scoped_live_company_config(db, user, company_id)
    if not company:
        return _live_error("Company profile not found or not assigned to your account.", 404)
    if not can_access_tally_company(db, user, company, tally_company):
        return _live_error("This Tally company is not assigned to your account.", 403)
    if from_date > to_date:
        return _live_error("Sales book start date must be on or before the end date.", 400)
    ledgers = cached_ledgers(db, company.id, tally_company)
    vouchers = cached_sales_book(db, company.id, tally_company, from_date, to_date)
    visible_ledgers = filter_ledgers(db, user, company.id, ledgers)
    visible_vouchers = filter_sales_vouchers(db, user, company.id, vouchers)
    refreshed_at = latest_cache_refresh(db, company.id, tally_company)
    return JSONResponse(
        {
            "ok": True,
            "source": "database",
            "company": tally_company.strip(),
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
            "ledger_count": len(visible_ledgers),
            "sales_count": len(visible_vouchers),
            "ledgers": [asdict(ledger) for ledger in visible_ledgers],
            "vouchers": [asdict(voucher) for voucher in visible_vouchers],
        }
    )
