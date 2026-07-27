from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import require_permission
from app.database import get_db
from app.services.expiry import expiry_summary
from app.templates import templates

router = APIRouter(prefix="/expiry")


@router.get("")
def expiry_dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_permission(request, db, "expiry_analytics")
    return templates.TemplateResponse(
        request,
        "expiry.html",
        {
            "request": request,
            "user": user,
            "expiry": expiry_summary(db),
        },
    )
