from datetime import date, datetime, timezone
from enum import Enum
import json

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    DIRECTORS = "directors"
    WAREHOUSE_MANAGER = "warehouse_manager"
    PURCHASE = "purchase"
    SALES = "sales"
    AUDITOR = "auditor"


def normalize_role_values(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Role):
        items = [value]
    elif isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [str(value)]

    role_values = {role.value for role in Role}
    role_names = {role.name: role.value for role in Role}
    selected: list[str] = []
    for item in items:
        parts = [item.value] if isinstance(item, Role) else str(item).split(",")
        for part in parts:
            token = part.strip()
            if token.startswith("Role."):
                token = token.split(".", 1)[1]
            if token in role_names:
                token = role_names[token]
            if token in role_values and token not in selected:
                selected.append(token)
    return tuple(selected)


def serialize_role_values(value) -> str:
    selected = set(normalize_role_values(value))
    return ",".join(role.value for role in Role if role.value in selected)


def role_label(value) -> str:
    roles = normalize_role_values(value)
    return ", ".join(roles)


def has_role(value, role: Role | str) -> bool:
    roles = normalize_role_values(value)
    target = normalize_role_values(role)
    return bool(target and target[0] in roles)


def has_any_role(value, roles) -> bool:
    values = set(normalize_role_values(value))
    targets = set(normalize_role_values(roles))
    return bool(values & targets)


class SerialStatus(str, Enum):
    GENERATED = "GENERATED"
    PURCHASED = "PURCHASED"
    RECEIVED = "RECEIVED"
    IN_STOCK = "IN_STOCK"
    SOLD = "SOLD"
    RETURNED = "RETURNED"
    PURCHASE_RETURN = "PURCHASE_RETURN"
    ISSUED = "ISSUED"
    AUDITED = "AUDITED"
    DAMAGED = "DAMAGED"
    MISSING = "MISSING"
    INVALID = "INVALID"
    REPLACED = "REPLACED"


class BatchType(str, Enum):
    PURCHASE = "PURCHASE"
    RECEIVE = "RECEIVE"
    SALE = "SALE"
    AUDIT = "AUDIT"
    PURCHASE_RETURN = "PURCHASE_RETURN"
    SALES_RETURN = "SALES_RETURN"
    ISSUE = "ISSUE"
    QR_ASSIGNMENT = "QR_ASSIGNMENT"


class GstTreatment(str, Enum):
    INTRA_STATE = "INTRA_STATE"
    INTER_STATE = "INTER_STATE"


class GstRegistrationType(str, Enum):
    UNKNOWN = "Unknown"
    COMPOSITION = "Composition"
    REGULAR = "Regular"
    UNREGISTERED_CONSUMER = "Unregistered/Consumer"


class TransactionType(str, Enum):
    PURCHASE = "PURCHASE"
    SALE = "SALE"
    SALES_RETURN = "SALES_RETURN"
    PURCHASE_RETURN = "PURCHASE_RETURN"
    ISSUE = "ISSUE"
    AUDIT = "AUDIT"
    QR_ASSIGNMENT = "QR_ASSIGNMENT"
    QR_REPLACEMENT = "QR_REPLACEMENT"
    RELOCATION = "RELOCATION"


class WarehouseLevel(str, Enum):
    COMPANY_WAREHOUSE = "Company Warehouse"
    C_AND_F = "C&F"
    MASTER_FRANCHISE = "Master Franchise"
    TALUK_FRANCHISE = "Taluk Franchise"
    HOME_FRANCHISE = "Home Franchise"


class BatchStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"
    PENDING_SYNC = "PENDING_SYNC"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(255), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batches: Mapped[list["Batch"]] = relationship(back_populates="user")
    inventory_transactions: Mapped[list["InventoryTransaction"]] = relationship(back_populates="user")
    tally_access_assignments: Mapped[list["UserTallyAccess"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    @property
    def role_values(self) -> tuple[str, ...]:
        return normalize_role_values(self.role)

    @property
    def role_label(self) -> str:
        return role_label(self.role)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    product_name: Mapped[str] = mapped_column(String(180), index=True)
    nickname: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    hsn: Mapped[str] = mapped_column(String(40))
    gst_rate: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(40), default="Pcs")
    default_rate: Mapped[float] = mapped_column(Float, default=0)
    sales_discount_rate: Mapped[float] = mapped_column(Float, default=0)
    shelf_verification_interval: Mapped[int] = mapped_column(Integer, default=1)
    purchase_qr_print_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    tally_stock_item_name: Mapped[str] = mapped_column(String(180))
    alternate_tally_stock_item_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    serials: Mapped[list["Serial"]] = relationship(back_populates="product")
    inventory_transactions: Mapped[list["InventoryTransaction"]] = relationship(back_populates="product")
    audit_assignments: Mapped[list["AuditAssignment"]] = relationship(back_populates="product")


class StorageLocation(Base):
    __tablename__ = "storage_locations"
    __table_args__ = (
        UniqueConstraint(
            "warehouse",
            "zone",
            "section",
            "rack",
            "shelf",
            "bin",
            name="uq_storage_location_path",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    warehouse: Mapped[str] = mapped_column(String(80), index=True)
    warehouse_level: Mapped[str] = mapped_column(
        String(40),
        default=WarehouseLevel.COMPANY_WAREHOUSE.value,
        index=True,
    )
    zone: Mapped[str] = mapped_column(String(80), index=True)
    section: Mapped[str] = mapped_column(String(80), index=True)
    rack: Mapped[str] = mapped_column(String(80), index=True)
    shelf: Mapped[str] = mapped_column(String(80), index=True)
    bin: Mapped[str] = mapped_column(String(80), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    serials: Mapped[list["Serial"]] = relationship(back_populates="location")

    @property
    def full_path(self) -> str:
        return " / ".join((self.warehouse, self.zone, self.section, self.rack, self.shelf, self.bin))


class Serial(Base):
    __tablename__ = "serials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    status: Mapped[str] = mapped_column(String(40), default=SerialStatus.GENERATED.value, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    replaced_by_id: Mapped[int | None] = mapped_column(ForeignKey("serials.id"), nullable=True)
    label_printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label_printed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    product_batch_number: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    mfg_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    warehouse: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    warehouse_level: Mapped[str] = mapped_column(
        String(40),
        default=WarehouseLevel.COMPANY_WAREHOUSE.value,
        index=True,
    )
    location_id: Mapped[int | None] = mapped_column(ForeignKey("storage_locations.id"), nullable=True, index=True)

    product: Mapped[Product] = relationship(back_populates="serials")
    location: Mapped[StorageLocation | None] = relationship(back_populates="serials")
    batch_items: Mapped[list["BatchItem"]] = relationship(back_populates="serial")
    inventory_transactions: Mapped[list["InventoryTransaction"]] = relationship(back_populates="serial")
    audit_assignment_items: Mapped[list["AuditAssignmentItem"]] = relationship(back_populates="serial")

    @property
    def display_status(self) -> str:
        """Use the business-facing name for a QR that is not stock yet."""
        if self.status == SerialStatus.GENERATED.value:
            return "UNASSIGNED"
        return self.status


class StockRelocation(Base):
    __tablename__ = "stock_relocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference_number: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    product_batch_number: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    previous_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    new_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id"), index=True)
    previous_location_snapshot: Mapped[str] = mapped_column(String(520))
    new_location_snapshot: Mapped[str] = mapped_column(String(520))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_used: Mapped[str] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    product: Mapped[Product] = relationship()
    previous_location: Mapped[StorageLocation | None] = relationship(foreign_keys=[previous_location_id])
    new_location: Mapped[StorageLocation] = relationship(foreign_keys=[new_location_id])
    user: Mapped[User] = relationship()
    serial_links: Mapped[list["RelocationSerial"]] = relationship(back_populates="relocation")


class RelocationSerial(Base):
    __tablename__ = "relocation_serials"
    __table_args__ = (UniqueConstraint("relocation_id", "serial_id", name="uq_relocation_serial"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    relocation_id: Mapped[int] = mapped_column(ForeignKey("stock_relocations.id"), index=True)
    serial_id: Mapped[int] = mapped_column(ForeignKey("serials.id"), index=True)

    relocation: Mapped[StockRelocation] = relationship(back_populates="serial_links")
    serial: Mapped[Serial] = relationship()


class AuditAssignment(Base):
    __tablename__ = "audit_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    auditor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    product: Mapped[Product] = relationship(back_populates="audit_assignments")
    auditor: Mapped[User] = relationship(foreign_keys=[auditor_id])
    assigned_by: Mapped[User] = relationship(foreign_keys=[assigned_by_id])
    expected_items: Mapped[list["AuditAssignmentItem"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
    )
    batches: Mapped[list["Batch"]] = relationship(back_populates="audit_assignment")


class AuditAssignmentItem(Base):
    __tablename__ = "audit_assignment_items"
    __table_args__ = (
        UniqueConstraint("assignment_id", "serial_id", name="uq_audit_assignment_serial"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("audit_assignments.id"), index=True)
    serial_id: Mapped[int] = mapped_column(ForeignKey("serials.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    assignment: Mapped[AuditAssignment] = relationship(back_populates="expected_items")
    serial: Mapped[Serial] = relationship(back_populates="audit_assignment_items")


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    batch_type: Mapped[str] = mapped_column(String(40), index=True)
    party_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    party_state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    party_gst_registration_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    party_gst_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    party_gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gst_treatment: Mapped[str | None] = mapped_column(String(40), nullable=True)
    gst_cgst_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    gst_sgst_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    gst_igst_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    audit_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_assignments.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(40), default=BatchStatus.DRAFT.value, index=True)
    tally_voucher_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tally_voucher_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tally_reference: Mapped[str | None] = mapped_column(String(180), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_remote_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    sync_request_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="batches")
    audit_assignment: Mapped[AuditAssignment | None] = relationship(back_populates="batches")
    items: Mapped[list["BatchItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    scan_logs: Mapped[list["ScanLog"]] = relationship(back_populates="batch")
    sync_attempts: Mapped[list["SyncAttempt"]] = relationship(back_populates="batch")
    audit_findings: Mapped[list["AuditFinding"]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    inventory_transactions: Mapped[list["InventoryTransaction"]] = relationship(back_populates="batch")


class BatchItem(Base):
    __tablename__ = "batch_items"
    __table_args__ = (UniqueConstraint("batch_id", "serial_id", name="uq_batch_serial"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    serial_id: Mapped[int] = mapped_column(ForeignKey("serials.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    fefo_picked: Mapped[bool] = mapped_column(Boolean, default=False)
    shelf_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    shelf_verified_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    shelf_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    batch: Mapped[Batch] = relationship(back_populates="items")
    serial: Mapped[Serial] = relationship(back_populates="batch_items")
    shelf_location: Mapped[StorageLocation | None] = relationship()
    shelf_verified_by: Mapped[User | None] = relationship()


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    serial_id: Mapped[int | None] = mapped_column(ForeignKey("serials.id"), nullable=True)
    serial_number_raw: Mapped[str] = mapped_column(String(140), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tally_reference: Mapped[str | None] = mapped_column(String(180), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    batch: Mapped[Batch | None] = relationship(back_populates="scan_logs")
    serial: Mapped[Serial | None] = relationship()
    user: Mapped[User] = relationship()


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_type: Mapped[str] = mapped_column(String(40), index=True)
    serial_id: Mapped[int | None] = mapped_column(ForeignKey("serials.id"), nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    serial_number: Mapped[str | None] = mapped_column(String(140), nullable=True, index=True)
    status_from: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status_to: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    tally_reference: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    reference_number: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    serial: Mapped[Serial | None] = relationship(back_populates="inventory_transactions")
    product: Mapped[Product | None] = relationship(back_populates="inventory_transactions")
    batch: Mapped[Batch | None] = relationship(back_populates="inventory_transactions")
    user: Mapped[User] = relationship(back_populates="inventory_transactions")


class SyncAttempt(Base):
    __tablename__ = "sync_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    status: Mapped[str] = mapped_column(String(40), index=True)
    request_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    batch: Mapped[Batch] = relationship(back_populates="sync_attempts")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ChangeAudit(Base):
    __tablename__ = "change_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_username: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    actor: Mapped[User | None] = relationship()


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    config: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def tally_company_name(self) -> str:
        try:
            return json.loads(self.config).get("company_name", "")
        except (ValueError, TypeError):
            return ""


class UserTallyAccess(Base):
    __tablename__ = "user_tally_access"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "company_id",
            "resource_type",
            "resource_key",
            name="uq_user_tally_access_resource",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(24), index=True)
    resource_key: Mapped[str] = mapped_column(String(220))
    resource_label: Mapped[str] = mapped_column(String(220))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates="tally_access_assignments")
    company: Mapped[Company] = relationship()


class TallyLedgerCache(Base):
    __tablename__ = "tally_ledger_cache"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "tally_company_key",
            "ledger_key",
            name="uq_tally_ledger_cache_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    tally_company: Mapped[str] = mapped_column(String(220))
    tally_company_key: Mapped[str] = mapped_column(String(220), index=True)
    ledger_key: Mapped[str] = mapped_column(String(220))
    name: Mapped[str] = mapped_column(String(220), index=True)
    parent: Mapped[str] = mapped_column(String(220), default="")
    closing_balance: Mapped[str] = mapped_column(String(80), default="")
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class TallySalesVoucherCache(Base):
    __tablename__ = "tally_sales_voucher_cache"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "tally_company_key",
            "remote_id",
            name="uq_tally_sales_voucher_cache_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    tally_company: Mapped[str] = mapped_column(String(220))
    tally_company_key: Mapped[str] = mapped_column(String(220), index=True)
    remote_id: Mapped[str] = mapped_column(String(500))
    voucher_date: Mapped[str] = mapped_column(String(40), index=True)
    voucher_number: Mapped[str] = mapped_column(String(120), default="")
    voucher_type: Mapped[str] = mapped_column(String(120), default="")
    party_ledger: Mapped[str] = mapped_column(String(220), default="")
    amount: Mapped[str] = mapped_column(String(80), default="")
    narration: Mapped[str] = mapped_column(Text, default="")
    tally_user: Mapped[str] = mapped_column(String(220), default="", index=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class LoginAudit(Base):
    __tablename__ = "login_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_address: Mapped[str | None] = mapped_column(String(80), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class TallyMasterConfirmation(Base):
    __tablename__ = "tally_master_confirmations"
    __table_args__ = (UniqueConstraint("master_type", "master_name", name="uq_tally_master_confirmation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_type: Mapped[str] = mapped_column(String(80), index=True)
    master_name: Mapped[str] = mapped_column(String(220), index=True)
    source: Mapped[str] = mapped_column(String(220))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    confirmed_by: Mapped[User] = relationship()


class AuditFinding(Base):
    __tablename__ = "audit_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    serial_id: Mapped[int | None] = mapped_column(ForeignKey("serials.id"), nullable=True)
    serial_number: Mapped[str] = mapped_column(String(140), index=True)
    product_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    finding_type: Mapped[str] = mapped_column(String(40), index=True)
    expected_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    scanned_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    batch: Mapped[Batch] = relationship(back_populates="audit_findings")
    serial: Mapped[Serial | None] = relationship()
