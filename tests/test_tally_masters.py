from datetime import date
from unittest.mock import patch

from app.models import Product, User
from app.services.tally_masters import (
    build_company_list_xml,
    build_ledger_list_xml,
    build_sales_book_xml,
    collect_master_requirements,
    confirm_master,
    confirmation_lookup,
    fetch_tally_companies,
    fetch_tally_ledgers,
    fetch_tally_sales_book,
    live_sync_readiness,
    readiness_counts,
    test_tally_gateway as check_tally_gateway,
)
from app.services.settings import update_settings


VALID_SETTINGS = {
    "company_name": "Setuora Test Company",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales Ledger",
    "purchase_ledger_name": "Purchase Ledger",
    "cgst_ledger_name": "CGST Ledger",
    "sgst_ledger_name": "SGST Ledger",
    "round_off_ledger_name": "Round Off",
}


def test_collect_master_requirements_includes_products_and_settings(db_session):
    update_settings(
        db_session,
        {
            **VALID_SETTINGS,
            "sales_gst_ledger_mappings": (
                "5 | Sales @ 5% | Output CGST @ 2.5% | "
                "Output SGST @ 2.5% | Output IGST @ 5%"
            ),
        },
    )
    product = Product(
        product_code="SG010",
        product_name="Pepper",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=120,
        tally_stock_item_name="Sg Pepper 100grm",
    )
    db_session.add(product)
    db_session.commit()
    requirements = collect_master_requirements(db_session)
    names = {(item.master_type, item.master_name) for item in requirements}
    assert ("Stock Item", "Sg Pepper 100grm") in names
    assert ("Unit", "Pcs") in names
    assert ("Voucher Type", "Sales") not in names
    assert ("Ledger", "Sales Ledger") not in names
    assert ("Ledger", "Sales @ 5%") in names
    assert ("Ledger", "Output CGST @ 2.5%") in names
    assert ("Ledger", "Output SGST @ 2.5%") in names
    assert ("Ledger", "Output IGST @ 5%") in names


def test_removed_legacy_fields_do_not_create_missing_requirements(db_session):
    update_settings(
        db_session,
        {
            **VALID_SETTINGS,
            "sales_voucher_type": "",
            "purchase_voucher_type": "",
            "sales_ledger_name": "",
            "purchase_ledger_name": "",
            "cgst_ledger_name": "",
            "sgst_ledger_name": "",
        },
    )

    requirements = collect_master_requirements(db_session)

    assert all(item.master_name for item in requirements)
    assert {item.master_type for item in requirements} == {"Company", "Ledger"}
    assert {(item.master_type, item.master_name) for item in requirements} == {
        ("Company", VALID_SETTINGS["company_name"]),
        ("Ledger", VALID_SETTINGS["round_off_ledger_name"]),
    }


def test_confirmation_updates_readiness_counts(db_session):
    update_settings(db_session, VALID_SETTINGS)
    user = User(username="admin", password_hash="x", role="admin")
    db_session.add(user)
    db_session.commit()
    requirements = collect_master_requirements(db_session)
    company = next(item for item in requirements if item.master_type == "Company")
    confirm_master(db_session, user, company.master_type, company.master_name, company.source)
    counts = readiness_counts(requirements, confirmation_lookup(db_session))
    assert counts["confirmed"] == 1


def test_company_list_xml_is_read_only_export_request():
    xml = build_company_list_xml()
    assert "<TALLYREQUEST>Export</TALLYREQUEST>" in xml
    assert "<TYPE>Collection</TYPE>" in xml
    assert "<ID>List of Companies</ID>" in xml
    assert '<COLLECTION NAME="List of Companies">' in xml
    assert "<TYPE>Company</TYPE>" in xml
    assert "<NATIVEMETHOD>Name</NATIVEMETHOD>" in xml
    assert "VOUCHER" not in xml


def test_ledger_and_sales_book_xml_are_scoped_to_selected_company():
    ledger_xml = build_ledger_list_xml("Selected Company")
    sales_xml = build_sales_book_xml(
        "Selected Company",
        date(2026, 4, 1),
        date(2026, 7, 15),
    )

    assert "<ID>Setuora Ledger List</ID>" in ledger_xml
    assert "<SVCURRENTCOMPANY>Selected Company</SVCURRENTCOMPANY>" in ledger_xml
    assert "<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>" in ledger_xml
    assert "<ID>Setuora Sales Book</ID>" in sales_xml
    assert '<SVFROMDATE TYPE="Date">20260401</SVFROMDATE>' in sales_xml
    assert '<SVTODATE TYPE="Date">20260715</SVTODATE>' in sales_xml
    assert "<NATIVEMETHOD>GUID</NATIVEMETHOD>" in sales_xml
    assert "<NATIVEMETHOD>MasterID</NATIVEMETHOD>" in sales_xml
    assert "<NATIVEMETHOD>EnteredBy</NATIVEMETHOD>" in sales_xml
    assert "$$IsSales:$VoucherTypeName" in sales_xml


class _GatewayResponse:
    def __init__(self, body: str):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body.encode()


def test_live_tally_data_parses_companies_ledgers_and_sales_vouchers():
    responses = [
        _GatewayResponse(
            """
            <ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA><COLLECTION>
              <COMPANY NAME="Second Company"><NAME>Second Company</NAME></COMPANY>
              <COMPANY><NAME>First Company</NAME></COMPANY>
            </COLLECTION></DATA></BODY></ENVELOPE>
            """
        ),
        _GatewayResponse(
            """
            <ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA><COLLECTION>
              <LEDGER NAME="Sales @ 5%"><NAME>Sales @ 5%</NAME><PARENT>Sales Accounts</PARENT><CLOSINGBALANCE>1250.00</CLOSINGBALANCE></LEDGER>
              <LEDGER><NAME>Customer A</NAME><PARENT>Sundry Debtors</PARENT><CLOSINGBALANCE>-500.00</CLOSINGBALANCE></LEDGER>
            </COLLECTION></DATA></BODY></ENVELOPE>
            """
        ),
        _GatewayResponse(
            """
            <ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA><COLLECTION>
              <VOUCHER>
                <DATE>20260715</DATE><VOUCHERNUMBER>42</VOUCHERNUMBER>
                <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME><PARTYLEDGERNAME>Customer A</PARTYLEDGERNAME>
                <AMOUNT>500.00</AMOUNT><NARRATION>Test sale</NARRATION><GUID>sale-guid-42</GUID>
                <ENTEREDBY>tally-sales-1</ENTEREDBY>
              </VOUCHER>
              <VOUCHER><VOUCHERNUMBER>metadata-only</VOUCHERNUMBER></VOUCHER>
            </COLLECTION></DATA></BODY></ENVELOPE>
            """
        ),
    ]
    settings = {"tally_host": "127.0.0.1", "tally_port": "9000"}
    with patch("app.services.tally_masters.urlopen", side_effect=responses):
        companies = fetch_tally_companies(settings)
        ledgers = fetch_tally_ledgers(settings, "First Company")
        vouchers = fetch_tally_sales_book(
            settings,
            "First Company",
            date(2026, 4, 1),
            date(2026, 7, 15),
        )

    assert companies == ["First Company", "Second Company"]
    assert [ledger.name for ledger in ledgers] == ["Customer A", "Sales @ 5%"]
    assert ledgers[0].parent == "Sundry Debtors"
    assert vouchers[0].date == "2026-07-15"
    assert vouchers[0].voucher_number == "42"
    assert vouchers[0].party_ledger == "Customer A"
    assert vouchers[0].remote_id == "sale-guid-42"
    assert vouchers[0].tally_user == "tally-sales-1"


def test_live_tally_data_removes_invalid_xml_character_references():
    response = _GatewayResponse(
        """
        <ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA><COLLECTION>
          <LEDGER NAME="Profit &amp; Loss A/c">
            <NAME>Profit &amp; Loss A/c</NAME>
            <PARENT>&#4; Primary</PARENT>
            <CLOSINGBALANCE>-1913547.66</CLOSINGBALANCE>
          </LEDGER>
          <LEDGER NAME="Control marker"><NAME>Control marker</NAME><PARENT>&#x4; Primary</PARENT></LEDGER>
        </COLLECTION></DATA></BODY></ENVELOPE>
        """
    )

    with patch("app.services.tally_masters.urlopen", return_value=response):
        ledgers = fetch_tally_ledgers(
            {"tally_host": "127.0.0.1", "tally_port": "9000"},
            "First Company",
        )

    assert [ledger.parent for ledger in ledgers] == ["Primary", "Primary"]


def test_gateway_check_rejects_tally_line_error():
    response = """
        <ENVELOPE>
          <HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER>
          <BODY><DATA><LINEERROR>Could not find Report 'List of Companies'!</LINEERROR></DATA></BODY>
        </ENVELOPE>
    """
    with patch("app.services.tally_masters.urlopen", return_value=_GatewayResponse(response)):
        result = check_tally_gateway({"tally_host": "127.0.0.1", "tally_port": "9000"})

    assert not result.ok
    assert result.message == "Tally rejected gateway check: Could not find Report 'List of Companies'!"


def test_gateway_check_accepts_successful_tally_xml():
    response = """
        <ENVELOPE>
          <HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>
          <BODY><DATA><COLLECTION><COMPANY><NAME>Setuora Test Company</NAME></COMPANY></COLLECTION></DATA></BODY>
        </ENVELOPE>
    """
    with patch("app.services.tally_masters.urlopen", return_value=_GatewayResponse(response)):
        result = check_tally_gateway({"tally_host": "127.0.0.1", "tally_port": "9000"})

    assert result.ok
    assert result.message == "Tally gateway responded"


def test_live_sync_readiness_requires_all_confirmations(db_session):
    update_settings(db_session, VALID_SETTINGS)
    user = User(username="admin2", password_hash="x", role="admin")
    db_session.add(user)
    db_session.commit()
    ready, counts = live_sync_readiness(db_session)
    assert not ready
    requirements = collect_master_requirements(db_session)
    for item in requirements:
        confirm_master(db_session, user, item.master_type, item.master_name, item.source)
    ready, counts = live_sync_readiness(db_session)
    assert ready
    assert counts["unchecked"] == 0
