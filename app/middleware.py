import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from app.auth import SESSION_COOKIE
from app.config import get_settings
from app.security import create_session_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
BACKGROUND_REQUEST_HEADER = "x-setuora-background"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), geolocation=(), microphone=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _authority(value: str | None) -> tuple[str, int | None] | None:
    if not value:
        return None
    parsed = urlparse(value if "//" in value else f"//{value}")
    if not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.hostname.lower(), port


def _origin(value: str | None) -> tuple[str, str, int] | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), port or (443 if parsed.scheme == "https" else 80)


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            source_origin = _origin(source)
            expected = request.headers.get("x-forwarded-host") or request.headers.get("host")
            expected_authority = _authority(expected.split(",")[0] if expected else None)
            expected_scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].lower()
            expected_port = 443 if expected_scheme == "https" else 80
            if expected_authority and expected_authority[1] is not None:
                expected_port = expected_authority[1]
            expected_origin = (
                (expected_scheme, expected_authority[0], expected_port)
                if expected_authority and expected_scheme in {"http", "https"}
                else None
            )
            if source_origin is None or expected_origin is None or source_origin != expected_origin:
                return PlainTextResponse("CSRF origin check failed", status_code=403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(18)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                (
                    "default-src 'self'",
                    f"script-src 'self' 'nonce-{nonce}'",
                    "style-src 'self' 'unsafe-inline'",
                    "img-src 'self' data: blob:",
                    "connect-src 'self'",
                    "font-src 'self' data:",
                    "object-src 'none'",
                    "base-uri 'self'",
                    "form-action 'self'",
                    "frame-ancestors 'none'",
                )
            ),
        )
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class SessionActivityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        user_id = getattr(request.state, "session_user_id", None)
        if not user_id or request.headers.get(BACKGROUND_REQUEST_HEADER, "").lower() == "true":
            return response

        settings = get_settings()
        response.set_cookie(
            SESSION_COOKIE,
            create_session_token(user_id),
            max_age=settings.session_timeout_minutes * 60,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
        )
        return response
