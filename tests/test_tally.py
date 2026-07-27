from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from starlette.requests import Request

from app.auth import SESSION_COOKIE
from app.models import (
    BatchStatus,
    BatchType,
    GstRegistrationType,
    GstTreatment,
    Product,
    SerialStatus,
    StorageLocation,
    User,
)
from app.routers import batches as batches_router
from app.security import create_session_token
from app.services.access_control import default_role_access_config
from app.services import tally as tally_service
from app.services.inventory import apply_batch_statuses, add_serial_to_batch, create_batch, generate_serials
from app.services.shelf_verification import verify_pending_items_on_shelf
from app.services.settings import update_settings
from app.services.tally import TallyResult, TallySyncError, build_voucher_xml, post_to_tally, sync_batch
from app.templates import templates


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_post_to_tally_treats_zero_created_as_failure(monkeypatch):
    body = "<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><EXCEPTIONS>1</EXCEPTIONS></RESPONSE>"
    monkeypatch.setattr(tally_service, "urlopen", lambda *a, **k: _FakeResponse(body))
    with pytest.raises(TallySyncError) as err:
        post_to_tally("<xml/>", {"tally_host": "localhost", "tally_port": "9000"})
    assert not err.value.retryable
    assert "created/altered nothing" in str(err.value).lower()


def test_post_to_tally_accepts_created_voucher(monkeypatch):
    body = "<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED></RESPONSE>"
    monkeypatch.setattr(tally_service, "urlopen", lambda *a, **k: _FakeResponse(body))
    result = post_to_tally("<xml/>", {"tally_host": "localhost", "tally_port": "9000"})
    assert result.reference == "CREATED=1; ALTERED=0"


VALID_SETTINGS = {
    "company_name": "Setuora Test Company",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales Ledger",
    "purchase_ledger_name": "Purchase Ledger",
    "cgst_ledger_name": "CGST Ledger",
    "sgst_ledger_name": "SGST Ledger",
    "sales_gst_ledger_mappings": (
        "5 | Sales Ledger | CGST Ledger | SGST Ledger | IGST Ledger\n"
        "18 | Sales Ledger | CGST Ledger | SGST Ledger | IGST Ledger"
    ),
    "round_off_ledger_name": "Round Off",
}


def _signed_request(user_id: int, path: str, method: str = "POST") -> Request:
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


def test_tally_xml_requires_party_on_the_batch(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    db_session.add(user)
    db_session.commit()
    batch = create_batch(db_session, user, BatchType.SALE, "", "")

    with pytest.raises(TallySyncError, match="customer or supplier"):
        build_voucher_xml(batch, VALID_SETTINGS)


@pytest.mark.parametrize(
    ("batch_type", "role", "initial_status"),
    [
        (BatchType.PURCHASE, "purchase", SerialStatus.GENERATED),
        (BatchType.SALE, "sales", SerialStatus.IN_STOCK),
    ],
)
def test_submitting_purchase_or_sale_automatically_starts_tally_sync(
    monkeypatch,
    db_session,
    batch_type,
    role,
    initial_status,
):
    user = User(username=f"auto-{role}", password_hash="x", role=role, active=True)
    product = Product(
        product_code=f"AUTO-{batch_type.value}",
        product_name=f"Automatic {batch_type.value}",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name=f"Automatic {batch_type.value}",
    )
    location = StorageLocation(
        code=f"AUTO-{batch_type.value}-SHELF",
        warehouse="MAIN",
        zone="A",
        section="1",
        rack="R1",
        shelf="S1",
        bin="B1",
    )
    db_session.add_all([user, product, location])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=initial_status)[0]
    batch = create_batch(db_session, user, batch_type, "Tally Party", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    if batch_type == BatchType.PURCHASE:
        verify_pending_items_on_shelf(db_session, batch=batch, location=location, user=user)

    synced_batch_ids: list[int] = []
    monkeypatch.setattr(
        batches_router,
        "sync_batch",
        lambda _db, submitted_batch: synced_batch_ids.append(submitted_batch.id),
    )

    response = batches_router.submit_batch(
        _signed_request(user.id, f"/batches/{batch.id}/submit"),
        batch.id,
        db_session,
    )

    assert response.status_code == 303
    assert synced_batch_ids == [batch.id]
    assert batch.status == BatchStatus.SUBMITTED.value


def test_sale_batch_xml_groups_serials_by_product(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = Product(
        product_code="SG003",
        product_name="Biryani Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=500,
        tally_stock_item_name="Sg Biriyani Masala 100grm",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serials = generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    for serial in serials:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    xml = build_voucher_xml(batch, VALID_SETTINGS)
    assert xml.count("<ALLINVENTORYENTRIES.LIST>") == 1
    assert "2 Pcs" in xml
    assert "Sg Biriyani Masala 100grm" in xml
    voucher = ET.fromstring(xml).find(".//VOUCHER")
    assert voucher is not None
    assert voucher.attrib["REMOTEID"]
    assert build_voucher_xml(batch, VALID_SETTINGS) == xml


def test_sale_batch_xml_includes_sales_discount(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = Product(
        product_code="SG004",
        product_name="Biryani Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=500,
        sales_discount_rate=10,
        tally_stock_item_name="Sg Biriyani Masala 100grm",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)

    xml = build_voucher_xml(batch, VALID_SETTINGS)

    assert "<DISCOUNT>10.00</DISCOUNT>" in xml
    assert "<AMOUNT>428.57</AMOUNT>" in xml
    assert "<AMOUNT>-450.00</AMOUNT>" in xml


def test_sale_batch_xml_includes_buyer_gst_details(db_session):
    user = User(username="sales-gst-buyer", password_hash="x", role="sales")
    product = Product(
        product_code="SGGSTBUY",
        product_name="Buyer GST Product",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Buyer GST Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Buyer Ledger",
        "",
        party_state="Karnataka",
        party_gst_registration_type=GstRegistrationType.REGULAR.value,
        party_gst_name="Buyer Registered Name",
        party_gstin="29abcde1234f1z5",
    )
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)

    xml = build_voucher_xml(batch, VALID_SETTINGS)

    assert "<PARTYLEDGERNAME>Buyer Ledger</PARTYLEDGERNAME>" in xml
    assert "<BASICBUYERNAME>Buyer Registered Name</BASICBUYERNAME>" in xml
    assert "<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>" in xml
    assert "<PARTYGSTIN>29ABCDE1234F1Z5</PARTYGSTIN>" in xml


def _accounting_sum(xml: str) -> Decimal:
    root = ET.fromstring(xml)
    total = Decimal("0")
    for container in root.iter():
        if container.tag in {"LEDGERENTRIES.LIST", "ACCOUNTINGALLOCATIONS.LIST"}:
            amount = container.findtext("AMOUNT")
            if amount is not None:
                total += Decimal(amount)
    return total


def test_sale_voucher_xml_is_balanced_with_tax_and_party(db_session):
    user = User(username="sales-bal", password_hash="x", role="sales")
    product = Product(
        product_code="SG005",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=500,
        tally_stock_item_name="Masala",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)

    xml = build_voucher_xml(batch, VALID_SETTINGS)

    assert VALID_SETTINGS["cgst_ledger_name"] in xml
    assert VALID_SETTINGS["sgst_ledger_name"] in xml
    assert "SANGEETHA" in xml
    assert "<AMOUNT>-500.00</AMOUNT>" in xml
    assert _accounting_sum(xml) == Decimal("0.00")


def test_sale_voucher_uses_product_gst_rate_ledger_mappings(db_session):
    user = User(username="sales-multi-gst", password_hash="x", role="sales")
    product_5 = Product(
        product_code="GST005",
        product_name="Five Percent Product",
        hsn="0901",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Five Percent Product",
    )
    product_18 = Product(
        product_code="GST018",
        product_name="Eighteen Percent Product",
        hsn="0902",
        gst_rate=18,
        unit="Pcs",
        default_rate=200,
        tally_stock_item_name="Eighteen Percent Product",
    )
    db_session.add_all([user, product_5, product_18])
    db_session.commit()
    serial_5 = generate_serials(db_session, product_5, 1, initial_status=SerialStatus.IN_STOCK)[0]
    serial_18 = generate_serials(db_session, product_18, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, batch, user, serial_5.serial_number)
    add_serial_to_batch(db_session, batch, user, serial_18.serial_number)
    apply_batch_statuses(db_session, batch, user)
    settings = {
        **VALID_SETTINGS,
        "sales_gst_ledger_mappings": (
            "5 | Sales @ 5% | Output CGST @ 2.5% | Output SGST @ 2.5% | Output IGST @ 5%\n"
            "18 | Sales @ 18% | Output CGST @ 9% | Output SGST @ 9% | Output IGST @ 18%"
        ),
    }

    xml = build_voucher_xml(batch, settings)
    root = ET.fromstring(xml)
    allocation_names = [
        entry.findtext("LEDGERNAME")
        for entry in root.iter("ACCOUNTINGALLOCATIONS.LIST")
    ]
    ledger_amounts = {
        entry.findtext("LEDGERNAME"): Decimal(entry.findtext("AMOUNT"))
        for entry in root.iter("LEDGERENTRIES.LIST")
    }

    assert allocation_names == ["Sales @ 5%", "Sales @ 18%"]
    assert ledger_amounts["Output CGST @ 2.5%"] == Decimal("2.38")
    assert ledger_amounts["Output SGST @ 2.5%"] == Decimal("2.38")
    assert ledger_amounts["Output CGST @ 9%"] == Decimal("15.25")
    assert ledger_amounts["Output SGST @ 9%"] == Decimal("15.25")
    assert VALID_SETTINGS["sales_ledger_name"] not in xml
    assert _accounting_sum(xml) == Decimal("0.00")


def test_sales_return_credit_note_xml_reverses_sales_postings(db_session):
    user = User(username="sales-return-xml", password_hash="x", role="sales")
    product = Product(
        product_code="RET005",
        product_name="Returned Product",
        hsn="0901",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Returned Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.SOLD)[0]
    batch = create_batch(db_session, user, BatchType.SALES_RETURN, "Customer", "", "GOOD")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)

    xml = build_voucher_xml(batch, VALID_SETTINGS)
    root = ET.fromstring(xml)
    voucher = root.find(".//VOUCHER")
    inventory = root.find(".//ALLINVENTORYENTRIES.LIST")
    allocation = inventory.find("ACCOUNTINGALLOCATIONS.LIST") if inventory is not None else None
    ledger_amounts = {
        entry.findtext("LEDGERNAME"): Decimal(entry.findtext("AMOUNT"))
        for entry in root.iter("LEDGERENTRIES.LIST")
    }

    assert voucher is not None
    assert voucher.attrib["VCHTYPE"] == "Credit Note"
    assert "<VOUCHERTYPENAME>Credit Note</VOUCHERTYPENAME>" in xml
    assert inventory is not None
    assert inventory.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert inventory.findtext("AMOUNT") == "-95.24"
    assert allocation is not None
    assert allocation.findtext("LEDGERNAME") == "Sales Ledger"
    assert allocation.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert allocation.findtext("AMOUNT") == "-95.24"
    assert ledger_amounts["CGST Ledger"] == Decimal("-2.38")
    assert ledger_amounts["SGST Ledger"] == Decimal("-2.38")
    assert ledger_amounts["Customer"] == Decimal("100.00")
    assert _accounting_sum(xml) == Decimal("0.00")


def test_sale_voucher_requires_product_gst_rate_mapping(db_session):
    user = User(username="sales-missing-gst-map", password_hash="x", role="sales")
    product = Product(
        product_code="GST012",
        product_name="Twelve Percent Product",
        hsn="0901",
        gst_rate=12,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Twelve Percent Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)

    with pytest.raises(TallySyncError, match="12%"):
        build_voucher_xml(batch, VALID_SETTINGS)


def test_sale_voucher_uses_mapping_with_removed_legacy_defaults_blank(db_session):
    user = User(username="sales-blank-legacy", password_hash="x", role="sales")
    product = Product(
        product_code="GST005BLANK",
        product_name="Five Percent Product",
        hsn="0901",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Five Percent Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    settings = {
        **VALID_SETTINGS,
        "sales_voucher_type": "",
        "sales_ledger_name": "",
        "cgst_ledger_name": "",
        "sgst_ledger_name": "",
        "sales_gst_ledger_mappings": (
            "5 | Sales @ 5% | Output CGST @ 2.5% | "
            "Output SGST @ 2.5% | Output IGST @ 5%"
        ),
    }

    xml = build_voucher_xml(batch, settings)

    assert "<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>" in xml
    assert "Sales @ 5%" in xml
    assert "Output CGST @ 2.5%" in xml
    assert "Output SGST @ 2.5%" in xml
    assert _accounting_sum(xml) == Decimal("0.00")


def test_interstate_sale_voucher_uses_mapped_igst_ledger(db_session):
    user = User(username="sales-igst", password_hash="x", role="sales")
    product = Product(
        product_code="GST-IGST",
        product_name="Interstate Product",
        hsn="0901",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Interstate Product",
    )
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Interstate Customer",
        "",
        party_state="Tamil Nadu",
        gst_treatment=GstTreatment.INTER_STATE.value,
    )
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    settings = {
        **VALID_SETTINGS,
        "sales_gst_ledger_mappings": (
            "5 | Sales @ 5% | Output CGST @ 2.5% | "
            "Output SGST @ 2.5% | Output IGST @ 5%"
        ),
    }

    xml = build_voucher_xml(batch, settings)
    ledger_amounts = {
        entry.findtext("LEDGERNAME"): Decimal(entry.findtext("AMOUNT"))
        for entry in ET.fromstring(xml).iter("LEDGERENTRIES.LIST")
    }

    assert ledger_amounts["Output IGST @ 5%"] == Decimal("4.76")
    assert "Output CGST @ 2.5%" not in ledger_amounts
    assert "Output SGST @ 2.5%" not in ledger_amounts
    assert "<PLACEOFSUPPLY>Tamil Nadu</PLACEOFSUPPLY>" in xml
    assert _accounting_sum(xml) == Decimal("0.00")


def test_purchase_voucher_xml_is_balanced(db_session):
    user = User(username="purch-bal", password_hash="x", role="purchase")
    product = Product(
        product_code="SG006",
        product_name="Masala",
        hsn="0910",
        gst_rate=18,
        unit="Pcs",
        default_rate=333,
        tally_stock_item_name="Masala",
    )
    location = StorageLocation(
        code="TALLY-PURCHASE-SHELF",
        warehouse="MAIN",
        zone="A",
        section="1",
        rack="R1",
        shelf="S1",
        bin="B1",
    )
    db_session.add_all([user, product, location])
    db_session.commit()
    serials = generate_serials(db_session, product, 3)
    batch = create_batch(db_session, user, BatchType.PURCHASE, "Vendor", "")
    for serial in serials:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)
    verify_pending_items_on_shelf(db_session, batch=batch, location=location, user=user)
    apply_batch_statuses(db_session, batch, user)

    xml = build_voucher_xml(batch, VALID_SETTINGS)

    assert _accounting_sum(xml) == Decimal("0.00")


def test_batch_list_exposes_purchase_sale_and_sales_return_tally_xml_exports():
    user = SimpleNamespace(
        username="admin",
        role="admin",
        _access_config=default_role_access_config(),
    )
    request = SimpleNamespace(url=SimpleNamespace(path="/batches"), query_params={})
    created_at = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    batches = [
        SimpleNamespace(
            id=101,
            batch_number="PUR-20260629-0001",
            batch_type=BatchType.PURCHASE.value,
            party_name="Supplier",
            reason_code=None,
            status=BatchStatus.PENDING_SYNC.value,
            items=[object()],
            retry_count=0,
            created_at=created_at,
        ),
        SimpleNamespace(
            id=102,
            batch_number="SAL-20260629-0001",
            batch_type=BatchType.SALE.value,
            party_name="Customer",
            reason_code=None,
            status=BatchStatus.PENDING_SYNC.value,
            items=[object()],
            retry_count=0,
            created_at=created_at,
        ),
        SimpleNamespace(
            id=103,
            batch_number="SRT-20260629-0001",
            batch_type=BatchType.SALES_RETURN.value,
            party_name="Customer",
            reason_code="GOOD",
            status=BatchStatus.PENDING_SYNC.value,
            items=[object()],
            retry_count=0,
            created_at=created_at,
        ),
    ]

    html = templates.env.get_template("batches.html").render(
        request=request,
        user=user,
        batches=batches,
    )

    assert 'href="/batches/new?batch_type=PURCHASE">Purchase</a>' in html
    assert 'href="/batches/new?batch_type=SALE">Sale</a>' in html
    assert 'href="/batches/101/tally.xml"' in html
    assert 'href="/batches/102/tally.xml"' in html
    assert 'href="/batches/103/tally.xml"' in html
    assert 'action="/batches/101/retry"' in html
    assert 'action="/batches/102/retry"' in html
    assert 'action="/batches/103/retry"' in html


def test_purchase_and_sale_batches_sync_to_tally(monkeypatch, db_session):
    user = User(username="tally-admin", password_hash="x", role="admin")
    purchase_product = Product(
        product_code="PUR-SYNC",
        product_name="Purchase Sync Item",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        shelf_verification_interval=0,
        tally_stock_item_name="Purchase Sync Item",
    )
    sale_product = Product(
        product_code="SAL-SYNC",
        product_name="Sale Sync Item",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=200,
        tally_stock_item_name="Sale Sync Item",
    )
    location = StorageLocation(
        code="SYNC-SHELF",
        warehouse="MAIN",
        zone="A",
        section="1",
        rack="R1",
        shelf="S1",
        bin="B1",
    )
    db_session.add_all([user, purchase_product, sale_product, location])
    db_session.commit()
    update_settings(db_session, {**VALID_SETTINGS, "tally_enabled": "true"})
    purchase_serial = generate_serials(db_session, purchase_product, 1)[0]
    sale_serial = generate_serials(db_session, sale_product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    purchase_batch = create_batch(db_session, user, BatchType.PURCHASE, "Supplier", "")
    sale_batch = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db_session, purchase_batch, user, purchase_serial.serial_number)
    add_serial_to_batch(db_session, sale_batch, user, sale_serial.serial_number)
    verify_pending_items_on_shelf(db_session, batch=purchase_batch, location=location, user=user)
    apply_batch_statuses(db_session, purchase_batch, user)
    apply_batch_statuses(db_session, sale_batch, user)
    db_session.commit()
    posted_xml: list[str] = []

    def fake_post(xml, _settings):
        posted_xml.append(xml)
        return TallyResult(xml, "<RESPONSE><CREATED>1</CREATED></RESPONSE>", "CREATED=1; ALTERED=0")

    monkeypatch.setattr(tally_service, "post_to_tally", fake_post)

    sync_batch(db_session, purchase_batch)
    sync_batch(db_session, sale_batch)

    assert purchase_batch.status == BatchStatus.SYNCED.value
    assert sale_batch.status == BatchStatus.SYNCED.value
    assert any("<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>" in xml for xml in posted_xml)
    assert any("<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>" in xml for xml in posted_xml)


def test_sales_return_batch_syncs_to_tally_as_credit_note(monkeypatch, db_session):
    user = User(username="return-sync-admin", password_hash="x", role="admin")
    product = Product(
        product_code="RET-SYNC",
        product_name="Sales Return Sync Item",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=200,
        tally_stock_item_name="Sales Return Sync Item",
    )
    db_session.add_all([user, product])
    db_session.commit()
    update_settings(db_session, {**VALID_SETTINGS, "tally_enabled": "true"})
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.SOLD)[0]
    batch = create_batch(db_session, user, BatchType.SALES_RETURN, "Customer", "", "GOOD")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    apply_batch_statuses(db_session, batch, user)
    db_session.commit()
    posted_xml: list[str] = []

    def fake_post(xml, _settings):
        posted_xml.append(xml)
        return TallyResult(xml, "<RESPONSE><CREATED>1</CREATED></RESPONSE>", "CREATED=1; ALTERED=0")

    monkeypatch.setattr(tally_service, "post_to_tally", fake_post)

    sync_batch(db_session, batch)

    assert batch.status == BatchStatus.SYNCED.value
    assert len(posted_xml) == 1
    assert "<VOUCHERTYPENAME>Credit Note</VOUCHERTYPENAME>" in posted_xml[0]
