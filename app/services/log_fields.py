from app.models import BatchType, InventoryTransaction, TransactionType


INVOICE_BATCH_TYPES = {BatchType.PURCHASE.value, BatchType.RECEIVE.value, BatchType.SALE.value}


def invoice_created_by(txn: InventoryTransaction) -> str:
    batch = txn.batch
    if not batch or batch.batch_type not in INVOICE_BATCH_TYPES:
        return ""
    return batch.user.username if batch.user else ""


def barcode_sold_by(txn: InventoryTransaction) -> str:
    if txn.transaction_type != TransactionType.SALE.value:
        return ""
    return txn.user.username if txn.user else ""


def product_audited_by(txn: InventoryTransaction) -> str:
    if txn.transaction_type != TransactionType.AUDIT.value:
        return ""
    return txn.user.username if txn.user else ""
