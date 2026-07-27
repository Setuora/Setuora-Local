from app.models import BatchType, GstTreatment, Product, SerialStatus, User
from app.services.inventory import add_serial_to_batch, create_batch, generate_serials, update_batch_item_rate
from app.services.voucher import calculate_voucher_summary, validate_priced_batch


def make_product(code="SG020", rate=500):
    return Product(
        product_code=code,
        product_name="Biryani Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=rate,
        sales_discount_rate=0,
        tally_stock_item_name="Sg Biriyani Masala 100grm",
    )


def test_voucher_summary_calculates_gst_and_round_off(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = make_product()
    db_session.add_all([user, product])
    db_session.commit()
    serials = generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    for serial in serials:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)
    summary = calculate_voucher_summary(batch)
    assert len(summary.lines) == 1
    assert summary.lines[0].quantity == 2
    assert str(summary.taxable_value) == "952.38"
    assert str(summary.cgst_amount) == "23.81"
    assert str(summary.sgst_amount) == "23.81"
    assert str(summary.final_value) == "1000.00"


def test_sale_rate_is_treated_as_gst_inclusive_amount(db_session):
    user = User(username="sales-inclusive", password_hash="x", role="sales")
    product = make_product("SGINCL", 45)
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)

    summary = calculate_voucher_summary(batch)

    assert str(summary.lines[0].rate) == "45.00"
    assert str(summary.lines[0].taxable_value) == "42.86"
    assert str(summary.lines[0].cgst_amount) == "1.07"
    assert str(summary.lines[0].sgst_amount) == "1.07"
    assert str(summary.lines[0].line_total) == "45.00"
    assert str(summary.final_value) == "45.00"


def test_interstate_sale_uses_igst_instead_of_cgst_sgst(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = make_product("SGIGST", 500)
    db_session.add_all([user, product])
    db_session.commit()
    serials = generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Interstate Customer",
        "",
        party_state="Tamil Nadu",
        gst_treatment=GstTreatment.INTER_STATE.value,
    )
    for serial in serials:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)

    summary = calculate_voucher_summary(batch)

    assert str(summary.taxable_value) == "952.38"
    assert str(summary.cgst_amount) == "0.00"
    assert str(summary.sgst_amount) == "0.00"
    assert str(summary.igst_amount) == "47.62"
    assert str(summary.final_value) == "1000.00"


def test_sale_splits_entered_local_gst_equally_between_cgst_and_sgst(db_session):
    user = User(username="sales-local-gst", password_hash="x", role="sales")
    product = make_product("SGLGST", 500)
    product.gst_rate = 18
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Local Customer",
        "",
        gst_treatment=GstTreatment.INTRA_STATE.value,
        gst_cgst_rate=2,
        gst_sgst_rate=3,
    )
    add_serial_to_batch(db_session, batch, user, serial.serial_number)

    summary = calculate_voucher_summary(batch)

    assert str(summary.lines[0].gst_rate) == "5.00"
    assert str(summary.lines[0].cgst_rate) == "2.50"
    assert str(summary.lines[0].sgst_rate) == "2.50"
    assert str(summary.cgst_amount) == "11.90"
    assert str(summary.sgst_amount) == "11.90"
    assert str(summary.final_value) == "500.00"


def test_interstate_sale_can_use_entered_igst_rate(db_session):
    user = User(username="sales-igst-rate", password_hash="x", role="sales")
    product = make_product("SGIGSTR", 500)
    product.gst_rate = 5
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(
        db_session,
        user,
        BatchType.SALE,
        "Interstate Customer",
        "",
        gst_treatment=GstTreatment.INTER_STATE.value,
        gst_igst_rate=12,
    )
    add_serial_to_batch(db_session, batch, user, serial.serial_number)

    summary = calculate_voucher_summary(batch)

    assert str(summary.lines[0].gst_rate) == "12.00"
    assert str(summary.lines[0].igst_rate) == "12.00"
    assert str(summary.igst_amount) == "53.57"
    assert str(summary.final_value) == "500.00"


def test_sales_discount_rate_reduces_sales_taxable_value(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = make_product("SGD01", 500)
    product.sales_discount_rate = 10
    db_session.add_all([user, product])
    db_session.commit()
    serials = generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    for serial in serials:
        add_serial_to_batch(db_session, batch, user, serial.serial_number)

    summary = calculate_voucher_summary(batch)

    assert str(summary.lines[0].gross_value) == "1000.00"
    assert str(summary.lines[0].discount_rate) == "10.00"
    assert str(summary.lines[0].discount_amount) == "100.00"
    assert str(summary.taxable_value) == "857.14"
    assert str(summary.gst_amount) == "42.86"
    assert str(summary.final_value) == "900.00"


def test_sales_discount_rate_does_not_change_purchase_value(db_session):
    user = User(username="purchase", password_hash="x", role="purchase")
    product = make_product("SGD02", 500)
    product.sales_discount_rate = 10
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1)[0]
    batch = create_batch(db_session, user, BatchType.RECEIVE, "Supplier", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)

    summary = calculate_voucher_summary(batch)

    assert str(summary.lines[0].discount_rate) == "0.00"
    assert str(summary.lines[0].discount_amount) == "0.00"
    assert str(summary.taxable_value) == "500.00"


def test_mixed_rates_split_product_lines(db_session):
    user = User(username="sales", password_hash="x", role="sales")
    product = make_product("SG021", 500)
    db_session.add_all([user, product])
    db_session.commit()
    serials = generate_serials(db_session, product, 2, initial_status=SerialStatus.IN_STOCK)
    batch = create_batch(db_session, user, BatchType.SALE, "SANGEETHA", "")
    first = add_serial_to_batch(db_session, batch, user, serials[0].serial_number)
    add_serial_to_batch(db_session, batch, user, serials[1].serial_number)
    update_batch_item_rate(db_session, batch, first.id, 600)
    summary = calculate_voucher_summary(batch)
    assert len(summary.lines) == 2
    assert sorted(str(line.rate) for line in summary.lines) == ["500.00", "600.00"]


def test_priced_batch_requires_positive_rate(db_session):
    user = User(username="purchase", password_hash="x", role="purchase")
    product = make_product("SG022", 0)
    db_session.add_all([user, product])
    db_session.commit()
    serial = generate_serials(db_session, product, 1)[0]
    batch = create_batch(db_session, user, BatchType.RECEIVE, "Supplier", "")
    add_serial_to_batch(db_session, batch, user, serial.serial_number)
    try:
        validate_priced_batch(batch)
    except ValueError as exc:
        assert "Set a positive rate" in str(exc)
    else:
        assert False
