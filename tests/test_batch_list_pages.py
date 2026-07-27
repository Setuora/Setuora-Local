from types import SimpleNamespace

from app.models import BatchType, User
from app.routers.batches import BATCH_LIST_SCOPES, batch_list_rows
from app.services.access_control import default_role_access_config
from app.services.inventory import create_batch
from app.templates import templates


def test_purchase_and_sales_batch_pages_filter_stock_views(db_session):
    user = User(username="admin", password_hash="x", role="admin", active=True)
    db_session.add(user)
    db_session.commit()

    purchase = create_batch(db_session, user, BatchType.PURCHASE, "Supplier", "")
    receive = create_batch(db_session, user, BatchType.RECEIVE, "Supplier", "")
    sale = create_batch(db_session, user, BatchType.SALE, "Customer", "")
    audit = create_batch(db_session, user, BatchType.AUDIT, "Rack A", "")

    purchase_rows = batch_list_rows(db_session, BATCH_LIST_SCOPES["purchase"]["types"])
    sales_rows = batch_list_rows(db_session, BATCH_LIST_SCOPES["sales"]["types"])

    assert {row.batch_number for row in purchase_rows} == {purchase.batch_number, receive.batch_number}
    assert {row.batch_number for row in sales_rows} == {sale.batch_number}
    assert audit.batch_number not in {row.batch_number for row in purchase_rows + sales_rows}

    template_user = SimpleNamespace(
        username="admin",
        role="admin",
        _access_config=default_role_access_config(),
    )
    request = SimpleNamespace(url=SimpleNamespace(path="/batches/purchase"), query_params={})
    html = templates.env.get_template("batches.html").render(
        request=request,
        user=template_user,
        batches=purchase_rows,
        batch_scope="purchase",
        page_title="Purchase batches",
        page_eyebrow="Incoming stock",
        empty_message="No purchase batches yet",
    )

    assert "Purchase batches" in html
    assert 'href="/batches/purchase">Purchase batches</a>' in html
    assert 'href="/batches/sales">Sales batches</a>' in html
    assert purchase.batch_number in html
    assert receive.batch_number in html
    assert sale.batch_number not in html
