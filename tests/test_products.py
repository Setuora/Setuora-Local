from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.auth import SESSION_COOKIE
from app.database import Base
from app.models import ChangeAudit, Product, Serial, SerialStatus, User
from app.routers.products import create_product, delete_product, product_name_legacy_redirect, product_sales_pdf, products as products_route, update_product_pricing
from app.security import create_session_token


def signed_request(user_id: int, path: str = "/products", method: str = "GET", query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"cookie", f"{SESSION_COOKIE}={create_session_token(user_id)}".encode())],
            "query_string": query_string,
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_product_master_saves_and_updates_sales_discount_rate():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role="admin", active=True))
        db.commit()

    with Session() as db:
        page = products_route(signed_request(1), db=db)
        create = create_product(
            signed_request(1, method="POST"),
            product_code="D001",
            product_name="Discount Item",
            nickname="Snack Alias",
            category="",
            brand="",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            default_rate=500,
            sales_discount_rate=7.5,
            shelf_verification_interval=1,
            purchase_qr_print_allowed=False,
            tally_stock_item_name="Discount Item",
            alternate_tally_stock_item_name="Tally Discount Alias",
            db=db,
        )
        product = db.scalar(select(Product).where(Product.product_code == "D001"))
        product_id = product.id
        update = update_product_pricing(
            signed_request(1, path=f"/products/{product_id}/pricing", method="POST"),
            product_id,
            default_rate=525,
            sales_discount_rate=12,
            purchase_qr_print_allowed=True,
            nickname="Updated Alias",
            category=None,
            brand=None,
            hsn="091099",
            gst_rate=12,
            shelf_verification_interval=1,
            tally_stock_item_name="Discount Item Primary",
            alternate_tally_stock_item_name="Discount Item Tally Two",
            db=db,
        )
        report_page = products_route(signed_request(1), db=db)
        alias_search = products_route(
            signed_request(1, query_string=b"q=Discount+Item+Tally+Two"),
            q="Discount Item Tally Two",
            db=db,
        )
        sales_pdf = product_sales_pdf(signed_request(1, path=f"/products/{product_id}/sales-report.pdf"), product_id, db=db)

    with Session() as db:
        saved = db.scalar(select(Product).where(Product.product_code == "D001"))
        assert saved.default_rate == 525
        assert saved.sales_discount_rate == 12
        assert saved.purchase_qr_print_allowed is True
        assert saved.nickname == "Updated Alias"
        assert saved.hsn == "091099"
        assert saved.gst_rate == 12
        assert saved.tally_stock_item_name == "Discount Item Primary"
        assert saved.alternate_tally_stock_item_name == "Discount Item Tally Two"
        assert saved.shelf_verification_interval == 1
        audits = db.scalars(
            select(ChangeAudit).where(ChangeAudit.entity_type == "product").order_by(ChangeAudit.id)
        ).all()
        assert [row.action for row in audits] == ["create", "update"]
        assert audits[-1].before_json and audits[-1].after_json
    engine.dispose()

    assert page.status_code == 200
    page_text = page.body.decode()
    report_text = report_page.body.decode()
    alias_search_text = alias_search.body.decode()

    assert "Sales discount %" in page_text
    assert "Nickname" in page_text
    assert "Generate label batch" not in page_text
    assert f'action="/products/{product_id}/generate"' not in page_text
    assert f'data-product-summary-hsn="{product_id}"' in report_text
    assert f'data-product-row-gst="{product_id}"' in report_text
    assert "Alternate Tally stock item" in page_text
    assert "Allow purchase QR printing" in page_text
    assert "Purchase QR" in report_text
    assert "Available stock" in report_text
    assert "Missing stock" in report_text
    assert "Restock" in report_text
    assert "<th>Report</th>" in report_text
    assert f'data-product-open="product-report-modal-{product_id}"' in report_text
    assert f'href="/products/{product_id}/sales-report.pdf"' in report_text
    assert sales_pdf.status_code == 200
    assert sales_pdf.headers["content-type"] == "application/pdf"
    assert sales_pdf.body.startswith(b"%PDF")
    assert alias_search.status_code == 200
    assert "Discount Item" in alias_search_text
    assert create.status_code == 303
    assert update.status_code == 303


def test_super_admin_can_delete_unused_product_and_name_edit_is_removed():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        db.add(User(id=1, username="root", password_hash="x", role="super_admin", active=True))
        db.add(
            Product(
                product_code="DEL",
                product_name="Delete Me",
                hsn="0910",
                gst_rate=5,
                unit="Pcs",
                tally_stock_item_name="Delete Me",
            )
        )
        db.commit()
        product_id = db.scalar(select(Product.id).where(Product.product_code == "DEL"))

    with Session() as db:
        page = products_route(signed_request(1), db=db)
        legacy_get = product_name_legacy_redirect(signed_request(1, path=f"/products/{product_id}/name"), product_id, db=db)
        legacy_post = product_name_legacy_redirect(
            signed_request(1, path=f"/products/{product_id}/name", method="POST"),
            product_id,
            db=db,
        )
        product_name = db.scalar(select(Product.product_name).where(Product.id == product_id))
        delete = delete_product(signed_request(1, path=f"/products/{product_id}/delete", method="POST"), product_id, db=db)

    with Session() as db:
        deleted = db.scalar(select(Product).where(Product.product_code == "DEL"))
    engine.dispose()

    assert page.status_code == 200
    page_text = page.body.decode()
    assert f"/products/{product_id}/name" not in page_text
    assert f"/products/{product_id}/delete" in page_text
    assert legacy_get.status_code == 303
    assert legacy_get.headers["location"] == "/products"
    assert legacy_post.status_code == 303
    assert legacy_post.headers["location"] == "/products"
    assert product_name == "Delete Me"
    assert delete.status_code == 303
    assert deleted is None


def test_product_delete_requires_super_admin_and_blocks_used_product():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        db.add(User(id=1, username="admin", password_hash="x", role="admin", active=True))
        db.add(User(id=2, username="root", password_hash="x", role="super_admin", active=True))
        product = Product(
            product_code="USED",
            product_name="Used Product",
            hsn="0910",
            gst_rate=5,
            unit="Pcs",
            tally_stock_item_name="Used Product",
        )
        db.add(product)
        db.commit()
        db.add(Serial(serial_number="USED-000001", product_id=product.id, status=SerialStatus.GENERATED.value))
        db.commit()
        product_id = product.id

    with Session() as db:
        try:
            delete_product(signed_request(1, path=f"/products/{product_id}/delete", method="POST"), product_id, db=db)
        except HTTPException as exc:
            admin_delete_status = exc.status_code
        else:
            admin_delete_status = None
        super_delete = delete_product(
            signed_request(2, path=f"/products/{product_id}/delete", method="POST"),
            product_id,
            db=db,
        )

    with Session() as db:
        still_exists = db.scalar(select(Product).where(Product.product_code == "USED"))
    engine.dispose()

    assert admin_delete_status == 403
    assert super_delete.status_code == 303
    assert super_delete.headers["location"] == "/products?error=product_delete_blocked"
    assert still_exists is not None
