from app.models import Role, User
from app.routers.batches import can_use_manual_scan, scan_source_allowed
from app.services.access_control import save_role_access_config


def test_admin_roles_can_use_manual_scan_source(db_session):
    admin = User(username="admin", password_hash="x", role=Role.ADMIN.value)
    super_admin = User(username="root", password_hash="x", role=Role.SUPER_ADMIN.value)

    assert can_use_manual_scan(db_session, admin)
    assert can_use_manual_scan(db_session, super_admin)
    assert scan_source_allowed(db_session, admin, "manual")
    assert scan_source_allowed(db_session, super_admin, "manual")


def test_staff_roles_must_use_camera_scan_source(db_session):
    purchase = User(username="purchase", password_hash="x", role=Role.PURCHASE.value)
    sales = User(username="sales", password_hash="x", role=Role.SALES.value)
    auditor = User(username="auditor", password_hash="x", role=Role.AUDITOR.value)

    for user in [purchase, sales, auditor]:
        assert not can_use_manual_scan(db_session, user)
        assert scan_source_allowed(db_session, user, "camera")
        assert not scan_source_allowed(db_session, user, "manual")


def test_manual_scan_source_cannot_be_granted_to_staff_role(db_session):
    purchase = User(username="purchase", password_hash="x", role=Role.PURCHASE.value)
    save_role_access_config(db_session, {"manual_serial_entry": {Role.PURCHASE.value: "edit"}})

    assert not can_use_manual_scan(db_session, purchase)
    assert not scan_source_allowed(db_session, purchase, "manual")
