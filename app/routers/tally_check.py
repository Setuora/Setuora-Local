from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_permission
from app.database import get_db
from app.models import Company, TallyDataJob
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
)
from app.services.sync_worker import (
    gateway_check_state,
    notify_retry_worker,
    queue_tally_gateway_check,
)
from app.services.tally_jobs import (
    COMPANIES_JOB,
    decode_job_payload,
    decode_job_result,
    LEDGERS_JOB,
    queue_tally_data_job,
    SALES_BOOK_JOB,
    STOCK_LOCATIONS_JOB,
    TallyDataQueueFull,
)
from app.services.tally_masters import (
    collect_master_requirements,
    confirmation_lookup,
    confirm_master,
    readiness_counts,
    remove_confirmation,
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
    if result is None:
        result = gateway_check_state(db)
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


def _queue_live_data_job(
    db: Session,
    *,
    user,
    company: Company,
    config: dict[str, str],
    job_type: str,
    tally_company: str = "",
    from_date: date | None = None,
    to_date: date | None = None,
) -> JSONResponse:
    try:
        job = queue_tally_data_job(
            db,
            job_type=job_type,
            company_id=company.id,
            requested_by_id=user.id,
            settings=config,
            tally_company=tally_company,
            from_date=from_date,
            to_date=to_date,
        )
    except TallyDataQueueFull as exc:
        return _live_error(str(exc), 429)
    notify_retry_worker()
    return JSONResponse(
        {
            "ok": True,
            "queued": True,
            "pending": True,
            "job_id": job.id,
            "status_url": f"/tally-check/jobs/{job.id}",
        },
        status_code=202,
    )


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
    tally_stock_location: str | None = Form(None),
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
        "tally_stock_location": (
            current_config.get("tally_stock_location", "Main Location")
            if tally_stock_location is None
            else tally_stock_location.strip() or "Main Location"
        ),
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
    result = queue_tally_gateway_check(db)
    active = get_active_company(db)
    return render_check_page(
        request,
        db,
        result,
        open_company_id=active.id if active else None,
    )


@router.get("/test-gateway/status")
def test_gateway_status(request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "tally_check_edit")
    result = gateway_check_state(db)
    if result is None:
        return JSONResponse({"ok": True, "status": "idle", "pending": False})
    return JSONResponse(
        {
            "ok": True,
            "request_id": result.request_id,
            "status": result.status,
            "pending": result.pending,
            "gateway_ok": result.ok,
            "message": result.message,
            "response_excerpt": result.response_excerpt,
        }
    )


@router.get("/jobs/{job_id}")
def tally_data_job_status(
    request: Request,
    job_id: int,
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "tally_check_edit")
    job = db.get(TallyDataJob, job_id)
    if job is None or not can_access_company(db, user, job.company_id):
        return _live_error("Tally data request not found or not assigned to your account.", 404)
    company = db.get(Company, job.company_id)
    if company is None:
        return _live_error("Company profile no longer exists.", 404)
    payload = decode_job_payload(job)
    tally_company = str(payload.get("tally_company", "")).strip()
    if tally_company and not can_access_tally_company(db, user, company, tally_company):
        return _live_error("This Tally company is not assigned to your account.", 403)
    if job.status in {"queued", "running"}:
        return JSONResponse(
            {
                "ok": True,
                "job_id": job.id,
                "status": job.status,
                "pending": True,
            }
        )
    if job.status == "failed":
        return _live_error(job.error or "Tally data request failed")
    if job.status != "succeeded":
        return _live_error("Tally data request has an unknown status.", 500)

    result = decode_job_result(job)
    if job.job_type == COMPANIES_JOB:
        names = result.get("companies", [])
        available_names = [str(name) for name in names] if isinstance(names, list) else []
        visible_names = filter_tally_company_names(db, user, company, available_names)
        return JSONResponse(
            {
                "ok": True,
                "pending": False,
                "profile": {"id": company.id, "name": company.name},
                "selected_company": company_config(company).get("company_name", ""),
                "companies": visible_names,
            }
        )
    if job.job_type == LEDGERS_JOB:
        ledgers = cached_ledgers(db, company.id, tally_company)
        visible_ledgers = filter_ledgers(db, user, company.id, ledgers)
        return JSONResponse(
            {
                "ok": True,
                "pending": False,
                "company": tally_company,
                "count": len(visible_ledgers),
                "ledgers": [asdict(ledger) for ledger in visible_ledgers],
            }
        )
    if job.job_type == STOCK_LOCATIONS_JOB:
        locations = result.get("locations", [])
        visible_locations = locations if isinstance(locations, list) else []
        return JSONResponse(
            {
                "ok": True,
                "pending": False,
                "company": tally_company,
                "count": len(visible_locations),
                "locations": visible_locations,
            }
        )
    if job.job_type == SALES_BOOK_JOB:
        try:
            from_date = date.fromisoformat(str(payload.get("from_date", "")))
            to_date = date.fromisoformat(str(payload.get("to_date", "")))
        except ValueError:
            return _live_error("Queued sales book dates are invalid.", 500)
        vouchers = cached_sales_book(db, company.id, tally_company, from_date, to_date)
        visible_vouchers = filter_sales_vouchers(db, user, company.id, vouchers)
        return JSONResponse(
            {
                "ok": True,
                "pending": False,
                "company": tally_company,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "count": len(visible_vouchers),
                "vouchers": [asdict(voucher) for voucher in visible_vouchers],
            }
        )
    return _live_error("Unsupported Tally data request.", 500)


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
    return _queue_live_data_job(
        db,
        user=user,
        company=company,
        config=config,
        job_type=COMPANIES_JOB,
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
    return _queue_live_data_job(
        db,
        user=user,
        company=company,
        config=config,
        job_type=LEDGERS_JOB,
        tally_company=tally_company,
    )


@router.get("/companies/{company_id}/live/stock-locations")
def live_stock_locations(
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
    return _queue_live_data_job(
        db,
        user=user,
        company=company,
        config=config,
        job_type=STOCK_LOCATIONS_JOB,
        tally_company=tally_company,
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
    return _queue_live_data_job(
        db,
        user=user,
        company=company,
        config=config,
        job_type=SALES_BOOK_JOB,
        tally_company=tally_company,
        from_date=from_date,
        to_date=to_date,
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
