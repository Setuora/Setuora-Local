from collections import Counter
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.auth import require_permission
from app.database import get_db
from app.models import Batch, Role, ScanLog, has_any_role
from app.services.charts import bar_chart, donut_chart
from app.services.director_reports import director_product_filter_options, director_product_stock_rows
from app.services.expiry import expiry_summary
from app.services.inventory import dashboard_counts, status_summary
from app.services.shelf_verification import pending_shelf_batches
from app.templates import templates

router = APIRouter()


def _recent_batches(db: Session):
    return db.scalars(select(Batch).order_by(desc(Batch.created_at)).limit(8)).all()


def _recent_scans(db: Session):
    return db.scalars(select(ScanLog).order_by(desc(ScanLog.created_at)).limit(8)).all()


def _scan_activity_chart(db: Session):
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    start_at = datetime.combine(days[0], time.min, tzinfo=timezone.utc)
    timestamps = db.scalars(select(ScanLog.created_at).where(ScanLog.created_at >= start_at)).all()
    counts = Counter(timestamp.date() for timestamp in timestamps)
    return bar_chart(((day.strftime("%d %b"), counts[day]) for day in days), include_zero=True)


def _chart_context(db: Session):
    serial_status = status_summary(db)
    return {
        "status_summary": serial_status,
        "stock_chart": donut_chart(serial_status.items()),
        "scan_activity_chart": _scan_activity_chart(db),
    }


def _admin_stock_context(db: Session, user, product_q: str) -> dict[str, object]:
    if not has_any_role(user.role, {Role.SUPER_ADMIN, Role.ADMIN}):
        return {
            "product_q": "",
            "product_stock_rows": [],
            "product_stock_options": [],
            "dashboard_live_url": "/dashboard/data",
        }
    query = product_q.strip()
    live_url = "/dashboard/data"
    if query:
        live_url = f"{live_url}?{urlencode({'product_q': query})}"
    return {
        "product_q": query,
        "product_stock_rows": director_product_stock_rows(db, query),
        "product_stock_options": director_product_filter_options(db),
        "dashboard_live_url": live_url,
    }


@router.get("/")
def dashboard(request: Request, product_q: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "dashboard_data")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "counts": dashboard_counts(db),
            **_admin_stock_context(db, user, product_q),
            **_chart_context(db),
            "expiry": expiry_summary(db),
            "recent_batches": _recent_batches(db),
            "recent_scans": _recent_scans(db),
            "shelf_alerts": pending_shelf_batches(db, limit=8)
            if has_any_role(user.role, {Role.SUPER_ADMIN, Role.ADMIN})
            else [],
        },
    )


@router.get("/dashboard/data")
def dashboard_data(request: Request, product_q: str = "", db: Session = Depends(get_db)):
    user = require_permission(request, db, "dashboard_data")
    batches_html = templates.env.get_template("partials/dashboard_batches.html").render(
        recent_batches=_recent_batches(db),
        user=user,
    )
    scans_html = templates.env.get_template("partials/dashboard_scans.html").render(
        recent_scans=_recent_scans(db)
    )
    charts_html = templates.env.get_template("partials/dashboard_charts.html").render(
        **_chart_context(db)
    )
    expiry_html = templates.env.get_template("partials/expiry_summary.html").render(
        expiry=expiry_summary(db),
        user=user,
    )
    shelf_alerts_html = templates.env.get_template("partials/dashboard_shelf_alerts.html").render(
        shelf_alerts=pending_shelf_batches(db, limit=8)
        if has_any_role(user.role, {Role.SUPER_ADMIN, Role.ADMIN})
        else [],
        user=user,
    )
    admin_stock = _admin_stock_context(db, user, product_q)
    product_stock_rows_html = templates.env.get_template(
        "partials/product_stock_summary_rows.html"
    ).render(product_rows=admin_stock["product_stock_rows"])
    return JSONResponse(
        {
            "counts": dashboard_counts(db),
            "charts_html": charts_html,
            "expiry_html": expiry_html,
            "batches_html": batches_html,
            "scans_html": scans_html,
            "shelf_alerts_html": shelf_alerts_html,
            "product_stock_rows_html": product_stock_rows_html,
        }
    )
