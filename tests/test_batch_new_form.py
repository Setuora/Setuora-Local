from types import SimpleNamespace

from sqlalchemy import select
from starlette.requests import Request

from app.auth import SESSION_COOKIE
from app.models import Batch, BatchItem, BatchType, GstTreatment, Product, SerialStatus, User
from app.routers.batches import create_batch_route, party_ledger_options, sale_gst_treatment_for_state
from app.security import create_session_token
from app.services.access_control import default_role_access_config
from app.services.inventory import create_batch, generate_serials
from app.templates import templates


def signed_request(user_id: int, path: str = "/batches", method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"cookie", f"{SESSION_COOKIE}={create_session_token(user_id)}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_sale_new_batch_has_fallback_state_and_scan_driven_products():
    html = templates.env.get_template("batch_new.html").render(
        user=None,
        batch_type=SimpleNamespace(value="SALE"),
        party_name="",
        party_state="",
        notes="",
        error=None,
    )

    assert 'name="party_state" list="sale-state-options"' in html
    assert 'name="party_state" list="sale-state-options" placeholder="State" value="Karnataka"' in html
    assert "Debtor ledger name" in html
    assert 'name="party_name"' in html
    assert 'list="party-ledger-options"' in html
    assert 'id="party-ledger-options"' in html
    assert "Sale product" not in html
    assert 'name="sale_product_id"' not in html
    assert 'name="sale_quantity"' not in html
    assert 'name="party_gst_registration_type"' in html
    assert '<option value="Unregistered/Consumer" selected>Unregistered/Consumer</option>' in html
    assert 'name="party_gst_name"' in html
    assert 'data-gst-number-field hidden' in html
    assert 'name="party_gstin"' in html
    assert "disabled" in html
    assert '<option value="Karnataka"></option>' in html
    assert 'name="gst_treatment"' in html
    assert 'value="INTRA_STATE"' in html
    assert 'value="INTER_STATE"' in html
    assert "CGST + SGST" in html
    assert "IGST" in html
    assert 'name="gst_cgst_rate"' not in html
    assert 'name="gst_sgst_rate"' not in html
    assert 'name="gst_igst_rate"' not in html
    assert "CGST %" not in html
    assert "SGST %" not in html
    assert "IGST %" not in html


def test_registered_sale_new_batch_shows_gst_number():
    html = templates.env.get_template("batch_new.html").render(
        user=None,
        batch_type=SimpleNamespace(value="SALE"),
        party_name="",
        party_state="",
        party_gst_registration_type="Regular",
        party_gst_name="",
        party_gstin="",
        notes="",
        error=None,
    )

    assert '<option value="Regular" selected>Registered</option>' in html
    assert 'name="party_state" list="sale-state-options" placeholder="State" value="Karnataka"' not in html
    assert 'data-gst-number-field hidden' not in html
    assert 'name="party_gst_name"' in html
    assert 'name="party_gstin"' in html


def test_sale_new_batch_preserves_selected_igst_treatment():
    html = templates.env.get_template("batch_new.html").render(
        user=None,
        batch_type=SimpleNamespace(value="SALE"),
        party_name="",
        party_state="Tamil Nadu",
        gst_treatment=GstTreatment.INTER_STATE.value,
        notes="",
        error=None,
    )

    assert 'name="gst_treatment"' in html
    inter_state_input = html.split('value="INTER_STATE"', 1)[1].split(">", 1)[0]
    assert "checked" in inter_state_input


def test_sale_gst_treatment_is_inferred_from_customer_state():
    assert sale_gst_treatment_for_state("Karnataka") == GstTreatment.INTRA_STATE.value
    assert sale_gst_treatment_for_state("") == GstTreatment.INTRA_STATE.value
    assert sale_gst_treatment_for_state("Tamil Nadu") == GstTreatment.INTER_STATE.value


def test_sale_ledger_options_use_previous_sale_parties(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    user._access_config = default_role_access_config()
    db_session.add(user)
    db_session.commit()
    create_batch(db_session, user, BatchType.SALE, "Asha Traders", "")
    create_batch(db_session, user, BatchType.SALES_RETURN, "Bharat Stores", "")
    create_batch(db_session, user, BatchType.PURCHASE, "Supplier Ledger", "")

    options = party_ledger_options(db_session, BatchType.SALE)
    html = templates.env.get_template("batch_new.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/batches/new"), query_params={}),
        user=user,
        batch_type=BatchType.SALE,
        party_name="",
        party_state="",
        party_name_options=options,
        notes="",
        error=None,
    )

    assert options == ["Asha Traders", "Bharat Stores"]
    assert '<option value="Asha Traders"></option>' in html
    assert '<option value="Bharat Stores"></option>' in html
    assert "Supplier Ledger" not in html


def test_new_sale_batch_starts_empty_and_records_logged_in_user(db_session):
    user = User(username="sales", password_hash="x", role="sales", active=True)
    product = Product(
        product_code="SALEPICK",
        product_name="Sale Pick Product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Sale Pick Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)

    response = create_batch_route(
        signed_request(user.id),
        batch_type=BatchType.SALE.value,
        party_name="Customer Ledger",
        party_state="Karnataka",
        party_gst_registration_type="Unregistered/Consumer",
        party_gst_name="",
        party_gstin="",
        gst_treatment=GstTreatment.INTRA_STATE.value,
        reason_code="",
        notes="",
        db=db_session,
    )

    batch = db_session.scalar(select(Batch).where(Batch.party_name == "Customer Ledger"))
    items = db_session.scalars(select(BatchItem).where(BatchItem.batch_id == batch.id)).all()

    assert response.status_code == 303
    assert response.headers["location"] == f"/batches/{batch.id}"
    assert batch.user_id == user.id
    assert batch.gst_treatment == GstTreatment.INTRA_STATE.value
    assert items == []


def test_new_sale_batch_records_selected_igst_treatment(db_session):
    user = User(username="sales", password_hash="x", role="sales", active=True)
    db_session.add(user)
    db_session.commit()

    response = create_batch_route(
        signed_request(user.id),
        batch_type=BatchType.SALE.value,
        party_name="Customer Ledger",
        party_state="Karnataka",
        party_gst_registration_type="Unregistered/Consumer",
        party_gst_name="",
        party_gstin="",
        gst_treatment=GstTreatment.INTER_STATE.value,
        reason_code="",
        notes="",
        db=db_session,
    )

    batch = db_session.scalar(select(Batch).where(Batch.party_name == "Customer Ledger"))

    assert response.status_code == 303
    assert batch.gst_treatment == GstTreatment.INTER_STATE.value


def test_sale_form_does_not_show_product_dropdown_even_when_stock_exists(db_session):
    user = User(username="sales", password_hash="x", role="sales", active=True)
    user._access_config = default_role_access_config()
    product = Product(
        product_code="SCANONLY",
        product_name="Scan Only Product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="No Stock Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)

    html = templates.env.get_template("batch_new.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/batches/new"), query_params={}),
        user=user,
        batch_type=BatchType.SALE,
        party_name="",
        party_state="",
        notes="",
        error=None,
    )

    assert "Scan Only Product" not in html
    assert 'name="sale_product_id"' not in html
    assert 'name="sale_quantity"' not in html
