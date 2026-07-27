from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from sqlalchemy import Engine, inspect, text

from app.database import engine


def ensure_runtime_schema(target_engine: Engine | None = None) -> None:
    target = target_engine or engine
    inspector = inspect(target)
    if "batches" not in inspector.get_table_names():
        return
    with target.begin() as connection:
        columns = {column["name"] for column in inspector.get_columns("batches")}
        if "retry_count" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN retry_count INTEGER DEFAULT 0"))
        if "last_retry_at" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN last_retry_at DATETIME"))
        if "reason_code" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN reason_code VARCHAR(80)"))
        if "party_state" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN party_state VARCHAR(80)"))
        if "party_gst_registration_type" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN party_gst_registration_type VARCHAR(40)"))
        if "party_gst_name" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN party_gst_name VARCHAR(180)"))
        if "party_gstin" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN party_gstin VARCHAR(20)"))
        if "gst_treatment" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN gst_treatment VARCHAR(40)"))
        if "gst_cgst_rate" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN gst_cgst_rate FLOAT"))
        if "gst_sgst_rate" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN gst_sgst_rate FLOAT"))
        if "gst_igst_rate" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN gst_igst_rate FLOAT"))
        if "sync_remote_id" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN sync_remote_id VARCHAR(80)"))
            connection.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_batches_sync_remote_id ON batches (sync_remote_id)")
            )
        if "sync_request_xml" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN sync_request_xml TEXT"))
        if "sync_started_at" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN sync_started_at DATETIME"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_batches_sync_started_at ON batches (sync_started_at)")
            )
        if "audit_assignment_id" not in columns:
            connection.execute(text("ALTER TABLE batches ADD COLUMN audit_assignment_id INTEGER"))
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_batches_audit_assignment_id "
                    "ON batches (audit_assignment_id)"
                )
            )

        if "serials" in inspector.get_table_names():
            serial_columns = {column["name"] for column in inspector.get_columns("serials")}
            if "label_printed_at" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN label_printed_at DATETIME"))
            if "label_printed_by_id" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN label_printed_by_id INTEGER"))
            if "product_batch_number" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN product_batch_number VARCHAR(80)"))
            if "mfg_date" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN mfg_date DATE"))
            if "expiry_date" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN expiry_date DATE"))
            if "warehouse" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN warehouse VARCHAR(80)"))
            if "warehouse_level" not in serial_columns:
                connection.execute(
                    text(
                        "ALTER TABLE serials ADD COLUMN warehouse_level "
                        "VARCHAR(40) DEFAULT 'Company Warehouse'"
                    )
                )
                connection.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_serials_warehouse_level ON serials (warehouse_level)")
                )
            if "location_id" not in serial_columns:
                connection.execute(text("ALTER TABLE serials ADD COLUMN location_id INTEGER"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS ix_serials_location_id ON serials (location_id)"))

        if "batch_items" in inspector.get_table_names():
            item_columns = {column["name"] for column in inspector.get_columns("batch_items")}
            if "fefo_picked" not in item_columns:
                connection.execute(text("ALTER TABLE batch_items ADD COLUMN fefo_picked BOOLEAN DEFAULT 0"))
            if "shelf_location_id" not in item_columns:
                connection.execute(text("ALTER TABLE batch_items ADD COLUMN shelf_location_id INTEGER"))
                connection.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_batch_items_shelf_location_id ON batch_items (shelf_location_id)")
                )
            if "shelf_verified_by_id" not in item_columns:
                connection.execute(text("ALTER TABLE batch_items ADD COLUMN shelf_verified_by_id INTEGER"))
            if "shelf_verified_at" not in item_columns:
                connection.execute(text("ALTER TABLE batch_items ADD COLUMN shelf_verified_at DATETIME"))

        if "products" in inspector.get_table_names():
            product_columns = {column["name"] for column in inspector.get_columns("products")}
            if "nickname" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN nickname VARCHAR(120)"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS ix_products_nickname ON products (nickname)"))
            if "sales_discount_rate" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN sales_discount_rate FLOAT DEFAULT 0"))
            if "brand" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN brand VARCHAR(120)"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS ix_products_brand ON products (brand)"))
            if "alternate_tally_stock_item_name" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN alternate_tally_stock_item_name VARCHAR(180)"))
            if "shelf_verification_interval" not in product_columns:
                connection.execute(
                    text("ALTER TABLE products ADD COLUMN shelf_verification_interval INTEGER DEFAULT 1")
                )
            if "purchase_qr_print_allowed" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN purchase_qr_print_allowed BOOLEAN DEFAULT 0"))
            connection.execute(
                text(
                    "UPDATE products SET shelf_verification_interval = 1 "
                    "WHERE shelf_verification_interval IS NULL OR shelf_verification_interval < 1"
                )
            )

        if "users" in inspector.get_table_names():
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "deleted_at" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN deleted_at DATETIME"))
            if "must_change_password" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"))

        if "tally_sales_voucher_cache" in inspector.get_table_names():
            voucher_cache_columns = {
                column["name"]
                for column in inspector.get_columns("tally_sales_voucher_cache")
            }
            if "tally_user" not in voucher_cache_columns:
                connection.execute(
                    text(
                        "ALTER TABLE tally_sales_voucher_cache "
                        "ADD COLUMN tally_user VARCHAR(220) DEFAULT ''"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_tally_sales_voucher_cache_tally_user "
                        "ON tally_sales_voucher_cache (tally_user)"
                    )
                )

        if "storage_locations" in inspector.get_table_names():
            location_columns = {column["name"] for column in inspector.get_columns("storage_locations")}
            if "warehouse_level" not in location_columns:
                connection.execute(
                    text(
                        "ALTER TABLE storage_locations ADD COLUMN warehouse_level "
                        "VARCHAR(40) DEFAULT 'Company Warehouse'"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_storage_locations_warehouse_level "
                        "ON storage_locations (warehouse_level)"
                    )
                )

        if target.dialect.name == "sqlite" and "stock_relocations" in inspector.get_table_names():
            for table_name in ("stock_relocations", "relocation_serials"):
                connection.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS prevent_{table_name}_update
                        BEFORE UPDATE ON {table_name}
                        BEGIN
                            SELECT RAISE(ABORT, 'Relocation history is permanent');
                        END
                        """
                    )
                )
                connection.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS prevent_{table_name}_delete
                        BEFORE DELETE ON {table_name}
                        BEGIN
                            SELECT RAISE(ABORT, 'Relocation history is permanent');
                        END
                        """
                    )
                )

    if target.dialect.name == "sqlite" and _missing_inventory_foreign_keys(target):
        _backup_before_schema_rebuild(target)
        _rebuild_sqlite_inventory_tables(target)


def _missing_inventory_foreign_keys(target: Engine) -> bool:
    inspector = inspect(target)
    expected = {
        "serials": {"label_printed_by_id", "location_id"},
        "batch_items": {"shelf_location_id", "shelf_verified_by_id"},
    }
    required_for_rebuild = {
        "serials": {
            "id",
            "serial_number",
            "product_id",
            "status",
            "active",
            "created_at",
            "replaced_by_id",
            "label_printed_at",
            "label_printed_by_id",
            "product_batch_number",
            "mfg_date",
            "expiry_date",
            "warehouse",
            "warehouse_level",
            "location_id",
        },
        "batch_items": {
            "id",
            "batch_id",
            "serial_id",
            "quantity",
            "rate",
            "remarks",
            "fefo_picked",
            "shelf_location_id",
            "shelf_verified_by_id",
            "shelf_verified_at",
            "created_at",
        },
    }
    for table_name, columns in expected.items():
        if table_name not in inspector.get_table_names():
            continue
        table_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if not required_for_rebuild[table_name].issubset(table_columns):
            continue
        constrained = {
            column
            for foreign_key in inspector.get_foreign_keys(table_name)
            for column in foreign_key["constrained_columns"]
        }
        if not columns.issubset(constrained):
            return True
    return False


def _backup_before_schema_rebuild(target: Engine) -> Path | None:
    database = target.url.database
    if not database or database == ":memory:":
        return None
    source_path = Path(database).resolve()
    if not source_path.exists():
        return None
    backup_dir = source_path.parent / "schema-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"{source_path.stem}-before-inventory-fk-{stamp}.db"
    source = sqlite3.connect(source_path)
    try:
        backup = sqlite3.connect(destination)
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()
    return destination


def _rebuild_sqlite_inventory_tables(target: Engine) -> None:
    connection = target.raw_connection()
    cursor = connection.cursor()
    try:
        connection.commit()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE serials__setuora_new (
                id INTEGER NOT NULL PRIMARY KEY,
                serial_number VARCHAR(140) NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id),
                status VARCHAR(40) NOT NULL,
                active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                replaced_by_id INTEGER REFERENCES serials__setuora_new(id),
                label_printed_at DATETIME,
                label_printed_by_id INTEGER REFERENCES users(id),
                product_batch_number VARCHAR(80),
                mfg_date DATE,
                expiry_date DATE,
                warehouse VARCHAR(80),
                warehouse_level VARCHAR(40) NOT NULL DEFAULT 'Company Warehouse',
                location_id INTEGER REFERENCES storage_locations(id)
            );

            CREATE TABLE batch_items__setuora_new (
                id INTEGER NOT NULL PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                serial_id INTEGER NOT NULL REFERENCES serials__setuora_new(id),
                quantity INTEGER NOT NULL,
                rate FLOAT,
                remarks TEXT,
                fefo_picked BOOLEAN NOT NULL DEFAULT 0,
                shelf_location_id INTEGER REFERENCES storage_locations(id),
                shelf_verified_by_id INTEGER REFERENCES users(id),
                shelf_verified_at DATETIME,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_batch_serial UNIQUE (batch_id, serial_id)
            );

            INSERT INTO serials__setuora_new (
                id, serial_number, product_id, status, active, created_at, replaced_by_id,
                label_printed_at, label_printed_by_id, product_batch_number, mfg_date,
                expiry_date, warehouse, warehouse_level, location_id
            )
            SELECT
                id, serial_number, product_id, status, active, created_at, replaced_by_id,
                label_printed_at, label_printed_by_id, product_batch_number, mfg_date,
                expiry_date, warehouse, COALESCE(warehouse_level, 'Company Warehouse'), location_id
            FROM serials;

            INSERT INTO batch_items__setuora_new (
                id, batch_id, serial_id, quantity, rate, remarks, fefo_picked,
                shelf_location_id, shelf_verified_by_id, shelf_verified_at, created_at
            )
            SELECT
                id, batch_id, serial_id, quantity, rate, remarks, COALESCE(fefo_picked, 0),
                shelf_location_id, shelf_verified_by_id, shelf_verified_at, created_at
            FROM batch_items;

            DROP TABLE batch_items;
            DROP TABLE serials;
            ALTER TABLE serials__setuora_new RENAME TO serials;
            ALTER TABLE batch_items__setuora_new RENAME TO batch_items;

            CREATE UNIQUE INDEX ix_serials_serial_number ON serials (serial_number);
            CREATE INDEX ix_serials_status ON serials (status);
            CREATE INDEX ix_serials_product_batch_number ON serials (product_batch_number);
            CREATE INDEX ix_serials_expiry_date ON serials (expiry_date);
            CREATE INDEX ix_serials_warehouse ON serials (warehouse);
            CREATE INDEX ix_serials_warehouse_level ON serials (warehouse_level);
            CREATE INDEX ix_serials_location_id ON serials (location_id);
            CREATE INDEX ix_batch_items_shelf_location_id ON batch_items (shelf_location_id);
            """
        )
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Foreign-key violations after schema migration: {violations[:5]}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        connection.close()
