from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.main import app
from app.models import (
    Batch,
    BatchType,
    TallyLedgerCache,
    TallySalesVoucherCache,
    User,
    UserTallyAccess,
)
from app.routers.users import create_user, users_page
from app.security import create_session_token, hash_password, verify_password
from app.services.settings import add_company


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def make_request(user_id: int, method: str = "GET", path: str = "/users") -> Request:
    token = create_session_token(user_id)
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def test_super_admin_can_delete_unused_user():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="root", password_hash="x", role="super_admin", active=True))
        db.add(User(id=2, username="temp", password_hash="x", role="purchase", active=False))
        db.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        cookies = {SESSION_COOKIE: create_session_token(1)}
        page = client.get("/users", cookies=cookies)
        delete = client.post("/users/2/delete", cookies=cookies)
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        deleted = db.get(User, 2)
    engine.dispose()

    assert page.status_code == 200
    assert 'action="/users/2/delete"' in page.text
    assert 'action="/users/1/delete"' not in page.text
    assert delete.status_code == 303
    assert delete.headers["location"] == "/users"
    assert deleted is None


def test_user_creation_accepts_multiple_roles():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="root", password_hash="x", role="super_admin", active=True))
        db.commit()
        page = users_page(make_request(1), db=db)
        response = create_user(
            make_request(1, method="POST"),
            username="dual",
            password="dual-pass",
            role=["purchase", "sales"],
            db=db,
        )
        after_page = users_page(make_request(1), db=db)
        dual = db.scalar(select(User).where(User.username == "dual"))
    engine.dispose()

    assert page.status_code == 200
    assert 'type="checkbox" name="role" value="purchase"' in page.body.decode()
    assert response.status_code == 303
    assert response.headers["location"] == "/users"
    assert dual is not None
    assert dual.role == "purchase,sales"
    assert "purchase, sales" in after_page.body.decode()


def test_user_delete_is_super_admin_only_and_archives_history_user():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role="admin", active=True))
        db.add(User(id=2, username="root", password_hash="x", role="super_admin", active=True))
        db.add(User(id=3, username="used", password_hash="x", role="sales", active=False))
        db.add(Batch(batch_number="B-1", batch_type=BatchType.SALE.value, user_id=3))
        db.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        admin_page = client.get("/users", cookies={SESSION_COOKIE: create_session_token(1)})
        admin_delete = client.post("/users/3/delete", cookies={SESSION_COOKIE: create_session_token(1)})
        self_delete = client.post("/users/2/delete", cookies={SESSION_COOKIE: create_session_token(2)})
        used_delete = client.post("/users/3/delete", cookies={SESSION_COOKIE: create_session_token(2)})
        after_delete_page = client.get("/users", cookies={SESSION_COOKIE: create_session_token(2)})
        deleted_session_page = client.get("/users", cookies={SESSION_COOKIE: create_session_token(3)})
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        used = db.scalar(select(User).where(User.username == "used"))
        root = db.scalar(select(User).where(User.username == "root"))
    engine.dispose()

    assert admin_page.status_code == 200
    assert "Delete user" not in admin_page.text
    assert admin_delete.status_code == 403
    assert self_delete.status_code == 303
    assert self_delete.headers["location"] == "/users?error=user_delete_self"
    assert used_delete.status_code == 303
    assert used_delete.headers["location"] == "/users"
    assert "used" not in after_delete_page.text
    assert deleted_session_page.status_code == 303
    assert deleted_session_page.headers["location"] == "/login"
    assert used is not None
    assert used.active is False
    assert used.deleted_at is not None
    assert root is not None


def test_super_admin_can_reset_another_users_password():
    engine, Session = make_session()
    with Session() as db:
        db.add(User(id=1, username="root", password_hash=hash_password("old-root-pass"), role="super_admin", active=True))
        db.add(User(id=2, username="staff", password_hash=hash_password("old-staff-pass"), role="sales", active=True))
        db.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        cookies = {SESSION_COOKIE: create_session_token(1)}
        page = client.get("/users", cookies=cookies)
        response = client.post(
            "/users/2/password",
            cookies=cookies,
            data={
                "new_password": "new-staff-pass",
                "confirm_password": "new-staff-pass",
                "force_change": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        staff = db.get(User, 2)
        assert staff is not None
        assert verify_password("new-staff-pass", staff.password_hash)
        assert not verify_password("old-staff-pass", staff.password_hash)
        assert staff.must_change_password is True
    engine.dispose()

    assert page.status_code == 200
    assert 'data-user-id="2"' in page.text
    assert 'action="/users/2/password"' not in page.text
    assert "old-staff-pass" not in page.text
    assert response.status_code == 303
    assert response.headers["location"] == "/users?success=password_reset"


def test_password_reset_is_super_admin_only_and_validates_input():
    engine, Session = make_session()
    original_hash = hash_password("original-pass")
    with Session() as db:
        db.add(User(id=1, username="admin", password_hash=hash_password("admin-pass"), role="admin", active=True))
        db.add(User(id=2, username="root", password_hash=hash_password("root-pass"), role="super_admin", active=True))
        db.add(User(id=3, username="staff", password_hash=original_hash, role="purchase", active=True))
        db.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        admin_page = client.get("/users", cookies={SESSION_COOKIE: create_session_token(1)})
        admin_reset = client.post(
            "/users/3/password",
            cookies={SESSION_COOKIE: create_session_token(1)},
            data={"new_password": "changed-pass", "confirm_password": "changed-pass"},
        )
        short_reset = client.post(
            "/users/3/password",
            cookies={SESSION_COOKIE: create_session_token(2)},
            data={"new_password": "short", "confirm_password": "short"},
        )
        mismatch_reset = client.post(
            "/users/3/password",
            cookies={SESSION_COOKIE: create_session_token(2)},
            data={"new_password": "changed-pass", "confirm_password": "different-pass"},
        )
        self_reset = client.post(
            "/users/2/password",
            cookies={SESSION_COOKIE: create_session_token(2)},
            data={"new_password": "changed-pass", "confirm_password": "changed-pass"},
        )
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        staff = db.get(User, 3)
        assert staff is not None
        assert staff.password_hash == original_hash
    engine.dispose()

    assert "Reset password" not in admin_page.text
    assert admin_reset.status_code == 403
    assert short_reset.headers["location"] == "/users?error=password_too_short"
    assert mismatch_reset.headers["location"] == "/users?error=password_mismatch"
    assert self_reset.headers["location"] == "/users?error=password_reset_self"


def test_users_menu_assigns_company_ledger_and_tally_user_access():
    engine, Session = make_session()
    with Session() as db:
        db.add_all(
            [
                User(id=1, username="root", password_hash="x", role="super_admin", active=True),
                User(id=2, username="staff", password_hash="x", role="sales", active=True),
            ]
        )
        db.commit()
        company = add_company(
            db,
            "Access Company",
            {
                "company_name": "Tally Access Company",
                "tally_host": "127.0.0.1",
                "tally_port": "9000",
                "round_off_ledger_name": "Round Off",
            },
        )
        ledger = TallyLedgerCache(
            company_id=company.id,
            tally_company="Tally Access Company",
            tally_company_key="tally access company",
            ledger_key="customer a",
            name="Customer A",
            parent="Sundry Debtors",
        )
        voucher = TallySalesVoucherCache(
            company_id=company.id,
            tally_company="Tally Access Company",
            tally_company_key="tally access company",
            remote_id="voucher-1",
            voucher_date="2026-07-15",
            party_ledger="Customer A",
            tally_user="operator-a",
        )
        db.add_all([ledger, voucher])
        db.commit()
        company_id = company.id
        ledger_id = ledger.id

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, follow_redirects=False, headers={"Origin": "http://testserver"})
        cookies = {SESSION_COOKIE: create_session_token(1)}
        before = client.get("/users", cookies=cookies)
        saved = client.post(
            "/users/2/tally-access",
            cookies=cookies,
            data={
                "company_id": str(company_id),
                "ledger_id": str(ledger_id),
                "tally_user": f"{company_id}:operator-a",
            },
        )
        after = client.get("/users", cookies=cookies)
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        assignments = db.scalars(
            select(UserTallyAccess)
            .where(UserTallyAccess.user_id == 2)
            .order_by(UserTallyAccess.resource_type)
        ).all()
    engine.dispose()

    assert before.status_code == 200
    assert 'data-user-id="2"' in before.text
    assert 'name="company_id"' in before.text
    assert 'name="ledger_id"' in before.text
    assert 'name="tally_user"' in before.text
    assert saved.status_code == 303
    assert saved.headers["location"] == "/users?success=tally_access_saved"
    assert [(row.resource_type, row.resource_label) for row in assignments] == [
        ("company", "Access Company"),
        ("ledger", "Customer A"),
        ("tally_user", "operator-a"),
    ]
    assert "1 company · 1 ledger · 1 Tally user" in after.text
