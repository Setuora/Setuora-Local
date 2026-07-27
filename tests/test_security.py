import asyncio

from starlette.requests import Request
from starlette.responses import Response

from app.auth import SESSION_COOKIE, current_user
from app.config import get_settings
from app.middleware import SessionActivityMiddleware
from app.models import User
from app.security import create_session_token, hash_password, read_session_token, verify_password


def test_password_hash_roundtrip():
    password_hash = hash_password("secret")
    assert verify_password("secret", password_hash)
    assert not verify_password("wrong", password_hash)


def test_session_token_roundtrip():
    token = create_session_token(42)
    assert read_session_token(token) == 42
    assert read_session_token(token + "x") is None


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers or [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def test_current_user_marks_session_activity(db_session):
    db_session.add(
        User(
            id=1,
            username="admin",
            password_hash=hash_password("admin123"),
            role="super_admin",
            active=True,
            must_change_password=False,
        )
    )
    db_session.commit()
    token = create_session_token(1)
    request = _request([(b"cookie", f"{SESSION_COOKIE}={token}".encode())])

    assert current_user(request, db_session).id == 1
    assert request.state.session_user_id == 1


def test_authenticated_activity_renews_session_cookie():
    middleware = SessionActivityMiddleware(app=lambda scope, receive, send: None)
    request = _request()
    request.state.session_user_id = 1

    async def call_next(_request):
        return Response()

    response = asyncio.run(middleware.dispatch(request, call_next))

    cookie = response.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE}=" in cookie
    assert f"Max-Age={get_settings().session_timeout_minutes * 60}" in cookie


def test_background_dashboard_refresh_does_not_renew_session_cookie():
    middleware = SessionActivityMiddleware(app=lambda scope, receive, send: None)
    request = _request([(b"x-setuora-background", b"true")])
    request.state.session_user_id = 1

    async def call_next(_request):
        return Response()

    response = asyncio.run(middleware.dispatch(request, call_next))

    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
