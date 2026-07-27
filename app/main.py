from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.middleware import CSRFOriginMiddleware, SecurityHeadersMiddleware, SessionActivityMiddleware
from app.routers import account, audit_assignments, auth, barcode_assignment, batches, dashboard, expiry, maintenance, products, replacements, reports, serials, settings, stock_movement, tally_check, users, warehouse
from app.services.backup_worker import start_backup_worker, stop_backup_worker
from app.services.bootstrap import bootstrap
from app.services.schema import ensure_runtime_schema
from app.services.sync_worker import start_retry_worker, stop_retry_worker

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if get_settings().using_default_secret:
        raise RuntimeError(
            "APP_SECRET_KEY is insecure. Set it to a long random string before startup."
        )
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()
    with SessionLocal() as db:
        bootstrap(db)
    start_retry_worker(app)
    start_backup_worker(app)
    try:
        yield
    finally:
        await stop_retry_worker(app)
        await stop_backup_worker(app)


def create_app() -> FastAPI:
    app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().trusted_hosts)
    app.add_middleware(SessionActivityMiddleware)
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(dashboard.router)
    app.include_router(barcode_assignment.router)
    app.include_router(products.router)
    app.include_router(serials.router)
    app.include_router(audit_assignments.router)
    app.include_router(batches.router)
    app.include_router(reports.router)
    app.include_router(stock_movement.router)
    app.include_router(expiry.router)
    app.include_router(settings.router)
    app.include_router(tally_check.router)
    app.include_router(maintenance.router)
    app.include_router(replacements.router)
    app.include_router(users.router)
    app.include_router(warehouse.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse("app/static/favicon.svg", media_type="image/svg+xml")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
