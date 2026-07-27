from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy.orm import Session

from app.models import Role, has_role, normalize_role_values
from app.services.settings import get_setting, update_settings


SETTING_KEY = "role_access_config"
DENY_VALUES = {"hidden", "no"}
SUPER_ADMIN_ONLY_KEYS = {"page_role_access", "role_access_edit", "database_reset"}
ADMIN_ONLY_KEYS = {"tally_excel_export"}


@dataclass(frozen=True)
class RoleColumn:
    key: str
    label: str


@dataclass(frozen=True)
class AccessOption:
    value: str
    label: str


@dataclass(frozen=True)
class AccessCell:
    value: str
    label: str
    tone: str
    options: list[AccessOption]


@dataclass(frozen=True)
class AccessRowDefinition:
    key: str
    item: str
    context: str
    defaults: dict[str, str]
    options: list[AccessOption]
    note: str = ""


@dataclass(frozen=True)
class AccessRow:
    key: str
    item: str
    context: str
    cells: list[AccessCell]
    note: str = ""


@dataclass(frozen=True)
class AccessSectionDefinition:
    title: str
    context_heading: str
    rows: list[AccessRowDefinition]


@dataclass(frozen=True)
class AccessSection:
    title: str
    context_heading: str
    rows: list[AccessRow]


ROLE_COLUMNS = [
    RoleColumn(Role.SUPER_ADMIN.value, "Super admin"),
    RoleColumn(Role.ADMIN.value, "Admin"),
    RoleColumn(Role.DIRECTORS.value, "Directors"),
    RoleColumn(Role.WAREHOUSE_MANAGER.value, "Warehouse manager"),
    RoleColumn(Role.PURCHASE.value, "Purchase"),
    RoleColumn(Role.SALES.value, "Sales"),
    RoleColumn(Role.AUDITOR.value, "Auditor"),
]

PAGE_OPTIONS = [AccessOption("shown", "Shown"), AccessOption("hidden", "Hidden")]
ACTION_OPTIONS = [AccessOption("edit", "Edit"), AccessOption("yes", "Yes"), AccessOption("no", "No")]
DATA_OPTIONS = [AccessOption("edit", "Edit"), AccessOption("view", "View"), AccessOption("workflow", "Workflow"), AccessOption("no", "No")]

CELL_META = {
    "shown": ("Shown", "synced"),
    "hidden": ("Hidden", "failed"),
    "edit": ("Edit", "synced"),
    "yes": ("Yes", "synced"),
    "view": ("View", "generated"),
    "workflow": ("Workflow", "pending_sync"),
    "no": ("No", "failed"),
}

ACCESS_VALUE_PRIORITY = {
    "edit": 5,
    "yes": 4,
    "workflow": 3,
    "view": 2,
    "shown": 1,
    "no": 0,
    "hidden": 0,
}


def _roles(*roles: Role) -> list[str]:
    return [role.value for role in roles]


def _defaults(options: list[AccessOption], allowed_value: str, allowed_roles: list[str]) -> dict[str, str]:
    denied = "hidden" if options == PAGE_OPTIONS else "no"
    values = {role.key: denied for role in ROLE_COLUMNS}
    values[Role.SUPER_ADMIN.value] = allowed_value
    for role in allowed_roles:
        values[role] = allowed_value
    return values


def _all_operational(options: list[AccessOption], value: str) -> dict[str, str]:
    return _defaults(
        options,
        value,
        _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER, Role.PURCHASE, Role.SALES, Role.AUDITOR),
    )


def _admin(options: list[AccessOption], value: str) -> dict[str, str]:
    return _defaults(options, value, _roles(Role.ADMIN))


def access_section_definitions() -> list[AccessSectionDefinition]:
    return [
        AccessSectionDefinition(
            title="Pages shown in navigation",
            context_heading="Where",
            rows=[
                AccessRowDefinition("page_dashboard", "Dashboard", "Top navigation", _all_operational(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition("page_batches", "Batches", "Top navigation menu", _all_operational(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition(
                    "page_warehouse",
                    "Warehouse",
                    "Top navigation menu",
                    _defaults(
                        PAGE_OPTIONS,
                        "shown",
                        _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER, Role.AUDITOR),
                    ),
                    PAGE_OPTIONS,
                ),
                AccessRowDefinition("page_serials", "Serials", "Top navigation", _all_operational(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition(
                    "page_reports",
                    "Reports",
                    "Top navigation",
                    _defaults(PAGE_OPTIONS, "shown", _roles(Role.ADMIN, Role.DIRECTORS)),
                    PAGE_OPTIONS,
                ),
                AccessRowDefinition(
                    "page_stock_movement",
                    "Stock movement",
                    "Top navigation",
                    _defaults(PAGE_OPTIONS, "shown", _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER)),
                    PAGE_OPTIONS,
                ),
                AccessRowDefinition("page_tally_check", "Tally Check", "Admin menu", _admin(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition(
                    "page_barcodes",
                    "Barcodes",
                    "Top navigation menu",
                    _defaults(PAGE_OPTIONS, "shown", _roles(Role.ADMIN, Role.PURCHASE)),
                    PAGE_OPTIONS,
                ),
                AccessRowDefinition("page_admin_menu", "Admin menu", "Top navigation menu", _admin(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition(
                    "page_products",
                    "Products",
                    "Admin menu",
                    _admin(PAGE_OPTIONS, "shown"),
                    PAGE_OPTIONS,
                    "Product search can still be opened directly by signed-in users when Product master data is allowed.",
                ),
                AccessRowDefinition("page_expiry", "Expiry", "Admin menu", _admin(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition("page_settings", "Settings", "Admin menu", _admin(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition(
                    "page_role_access",
                    "Role access",
                    "Admin menu",
                    _defaults(PAGE_OPTIONS, "shown", []),
                    PAGE_OPTIONS,
                    "Locked to super admin so other roles cannot change their own access.",
                ),
                AccessRowDefinition("page_maintenance", "Maintenance", "Admin menu", _admin(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
                AccessRowDefinition("page_users", "Users", "Admin menu", _admin(PAGE_OPTIONS, "shown"), PAGE_OPTIONS),
            ],
        ),
        AccessSectionDefinition(
            title="Actions allowed by role",
            context_heading="Action",
            rows=[
                AccessRowDefinition("batch_purchase", "Purchase batch", "Create, scan, edit draft, submit", _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.PURCHASE)), ACTION_OPTIONS),
                AccessRowDefinition("batch_sale", "Sale batch", "Create, scan, edit draft, submit", _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.SALES)), ACTION_OPTIONS),
                AccessRowDefinition("batch_audit", "Audit batch", "Create, scan, submit, download audit PDF", _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.AUDITOR)), ACTION_OPTIONS),
                AccessRowDefinition(
                    "audit_assignment_manage",
                    "Audit assignments",
                    "Assign timed product audits, extend deadlines; auditors can view their assigned work",
                    {
                        **_defaults(DATA_OPTIONS, "edit", _roles(Role.ADMIN, Role.DIRECTORS)),
                        Role.AUDITOR.value: "view",
                    },
                    DATA_OPTIONS,
                ),
                AccessRowDefinition("batch_sales_return", "Sales return", "Create, scan, edit draft, submit", _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.SALES)), ACTION_OPTIONS),
                AccessRowDefinition("batch_purchase_return", "Purchase return", "Create, scan, edit draft, submit", _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.PURCHASE)), ACTION_OPTIONS),
                AccessRowDefinition("batch_issue", "Stock issue", "Create, scan, edit draft, submit", _admin(ACTION_OPTIONS, "edit"), ACTION_OPTIONS),
                AccessRowDefinition(
                    "stock_relocation",
                    "Move stock",
                    "Search, scan, and confirm warehouse relocations",
                    _defaults(
                        ACTION_OPTIONS,
                        "edit",
                        _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER, Role.AUDITOR),
                    ),
                    ACTION_OPTIONS,
                ),
                AccessRowDefinition(
                    "location_manage",
                    "Storage locations",
                    "Create, activate, and deactivate warehouse locations",
                    _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER)),
                    ACTION_OPTIONS,
                ),
                AccessRowDefinition("manual_serial_entry", "Manual serial entry", "Type serial numbers into batches", _admin(ACTION_OPTIONS, "edit"), ACTION_OPTIONS, "Locked to admin and super admin; non-admin users can scan by camera/photo only."),
                AccessRowDefinition("fefo_pick", "FEFO pick", "Auto-pick sale, issue, purchase return", _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.PURCHASE, Role.SALES)), ACTION_OPTIONS),
                AccessRowDefinition("tally_xml", "Tally XML", "Download purchase/sale/sales-return XML", _admin(ACTION_OPTIONS, "yes"), ACTION_OPTIONS),
                AccessRowDefinition(
                    "tally_excel_export",
                    "Tally Excel export",
                    "Download purchase/sale Tally workbooks",
                    _admin(ACTION_OPTIONS, "yes"),
                    ACTION_OPTIONS,
                    "Locked to admin and super admin.",
                ),
                AccessRowDefinition("tally_sync_retry", "Tally sync retry", "Retry pending or failed sync", _admin(ACTION_OPTIONS, "yes"), ACTION_OPTIONS),
                AccessRowDefinition("product_create", "Products", "Create products and generate serials", _admin(ACTION_OPTIONS, "edit"), ACTION_OPTIONS),
                AccessRowDefinition(
                    "barcode_assignment",
                    "Barcode assignment",
                    "Assign labels to existing stock",
                    _defaults(ACTION_OPTIONS, "edit", _roles(Role.ADMIN, Role.PURCHASE)),
                    ACTION_OPTIONS,
                ),
                AccessRowDefinition("barcode_replacement", "Barcode replacement", "Replace damaged serial labels", _admin(ACTION_OPTIONS, "edit"), ACTION_OPTIONS),
                AccessRowDefinition("reports_export", "Reports", "Filter and export scan/transaction reports", _admin(ACTION_OPTIONS, "yes"), ACTION_OPTIONS),
                AccessRowDefinition(
                    "stock_movement_export",
                    "Stock movement export",
                    "Export Excel and PDF movement reports",
                    _defaults(ACTION_OPTIONS, "yes", _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER)),
                    ACTION_OPTIONS,
                ),
                AccessRowDefinition("tally_check_edit", "Tally Check", "Confirm, refresh, and remove master checks", _admin(ACTION_OPTIONS, "edit"), ACTION_OPTIONS),
                AccessRowDefinition("settings_edit", "Settings", "Edit company settings and enable sync", _admin(ACTION_OPTIONS, "edit"), ACTION_OPTIONS),
                AccessRowDefinition(
                    "role_access_edit",
                    "Role access",
                    "Edit role permission matrix",
                    _defaults(ACTION_OPTIONS, "edit", []),
                    ACTION_OPTIONS,
                    "Locked to super admin.",
                ),
                AccessRowDefinition(
                    "users_manage",
                    "Users",
                    "Create users and enable/disable accounts",
                    _admin(ACTION_OPTIONS, "edit"),
                    ACTION_OPTIONS,
                    "Only super admins can delete user accounts. Accounts with history are hidden and kept for old records.",
                ),
                AccessRowDefinition("backup_download", "Backup", "Download SQLite backup", _admin(ACTION_OPTIONS, "yes"), ACTION_OPTIONS),
                AccessRowDefinition(
                    "database_reset",
                    "Database reset",
                    "Clear database records and application cache",
                    _defaults(ACTION_OPTIONS, "yes", []),
                    ACTION_OPTIONS,
                    "Locked to super admin and requires password verification.",
                ),
            ],
        ),
        AccessSectionDefinition(
            title="Data access and modification",
            context_heading="Data area",
            rows=[
                AccessRowDefinition("dashboard_data", "Dashboard data", "Counts, charts, recent scans and batches", _all_operational(DATA_OPTIONS, "view"), DATA_OPTIONS),
                AccessRowDefinition("product_master", "Product master", "View product list/search", _all_operational(DATA_OPTIONS, "view"), DATA_OPTIONS, "Only users with Product action access can create products or generate serials."),
                AccessRowDefinition("serial_data", "Serial data", "View serial list, details, scan history", _all_operational(DATA_OPTIONS, "view"), DATA_OPTIONS),
                AccessRowDefinition("label_files", "Label files", "Download serial XLSX or admin label PDF", _defaults(DATA_OPTIONS, "view", _roles(Role.ADMIN, Role.PURCHASE, Role.SALES, Role.AUDITOR)), DATA_OPTIONS),
                AccessRowDefinition("batch_list", "Batch list", "View all recent batches", _all_operational(DATA_OPTIONS, "view"), DATA_OPTIONS, "Batch detail pages still follow batch-type permissions."),
                AccessRowDefinition("purchase_data", "Purchase data", "Purchase and purchase-return batches", _defaults(DATA_OPTIONS, "workflow", _roles(Role.ADMIN, Role.PURCHASE)), DATA_OPTIONS),
                AccessRowDefinition("sales_data", "Sales data", "Sale and sales-return batches", _defaults(DATA_OPTIONS, "workflow", _roles(Role.ADMIN, Role.SALES)), DATA_OPTIONS),
                AccessRowDefinition("audit_data", "Audit data", "Audit batches and findings", _defaults(DATA_OPTIONS, "workflow", _roles(Role.ADMIN, Role.AUDITOR)), DATA_OPTIONS),
                AccessRowDefinition("issue_data", "Issue data", "Stock issue batches", _admin(DATA_OPTIONS, "edit"), DATA_OPTIONS),
                AccessRowDefinition(
                    "warehouse_data",
                    "Warehouse data",
                    "Storage map and permanent relocation history",
                    _defaults(
                        DATA_OPTIONS,
                        "view",
                        _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER, Role.AUDITOR),
                    ),
                    DATA_OPTIONS,
                ),
                AccessRowDefinition(
                    "reports_data",
                    "Reports data",
                    "Scan logs, inventory transactions, and Directors reports",
                    _defaults(DATA_OPTIONS, "view", _roles(Role.ADMIN, Role.DIRECTORS)),
                    DATA_OPTIONS,
                ),
                AccessRowDefinition(
                    "stock_movement_data",
                    "Stock movement data",
                    "Movement, stock cover, expiry risk, and suggested actions",
                    _defaults(DATA_OPTIONS, "view", _roles(Role.ADMIN, Role.WAREHOUSE_MANAGER)),
                    DATA_OPTIONS,
                ),
                AccessRowDefinition("expiry_analytics", "Expiry analytics", "Expiry risk and sleeping stock", _admin(DATA_OPTIONS, "view"), DATA_OPTIONS),
                AccessRowDefinition("tally_settings", "Tally settings", "Company profiles, ledgers, sync flag", _admin(DATA_OPTIONS, "edit"), DATA_OPTIONS),
                AccessRowDefinition("tally_attempts", "Tally sync attempts", "Request/response details", _admin(DATA_OPTIONS, "view"), DATA_OPTIONS),
                AccessRowDefinition("user_accounts", "User accounts", "User list, roles, active status", _admin(DATA_OPTIONS, "edit"), DATA_OPTIONS),
                AccessRowDefinition("backup_data", "Backup data", "SQLite database download", _admin(DATA_OPTIONS, "view"), DATA_OPTIONS),
            ],
        ),
    ]


def _definitions_by_key() -> dict[str, AccessRowDefinition]:
    return {row.key: row for section in access_section_definitions() for row in section.rows}


def default_role_access_config() -> dict[str, dict[str, str]]:
    return {key: row.defaults.copy() for key, row in _definitions_by_key().items()}


def _valid_option(row: AccessRowDefinition, value: str) -> bool:
    return value in {option.value for option in row.options}


def normalize_role_access_config(raw_config: dict | None) -> dict[str, dict[str, str]]:
    definitions = _definitions_by_key()
    config = default_role_access_config()
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    for row_key, row_values in raw_config.items():
        row = definitions.get(row_key)
        if not row or not isinstance(row_values, dict):
            continue
        for role in ROLE_COLUMNS:
            if role.key == Role.SUPER_ADMIN.value:
                continue
            value = str(row_values.get(role.key, "")).strip()
            if _valid_option(row, value):
                config[row_key][role.key] = value
    for row_key, row in definitions.items():
        config[row_key][Role.SUPER_ADMIN.value] = row.defaults[Role.SUPER_ADMIN.value]
        if row_key in SUPER_ADMIN_ONLY_KEYS:
            denied = "hidden" if row.options == PAGE_OPTIONS else "no"
            for role in ROLE_COLUMNS:
                if role.key != Role.SUPER_ADMIN.value:
                    config[row_key][role.key] = denied
        if row_key in ADMIN_ONLY_KEYS:
            for role in ROLE_COLUMNS:
                config[row_key][role.key] = row.defaults[role.key]
    return config


def get_role_access_config(db: Session) -> dict[str, dict[str, str]]:
    raw = get_setting(db, SETTING_KEY, "")
    if not raw:
        return default_role_access_config()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return default_role_access_config()
    return normalize_role_access_config(parsed)


def save_role_access_config(db: Session, submitted: dict[str, dict[str, str]], *, commit: bool = True) -> None:
    config = get_role_access_config(db)
    for row_key, row_values in submitted.items():
        config.setdefault(row_key, {}).update(row_values)
    config = normalize_role_access_config(config)
    update_settings(db, {SETTING_KEY: json.dumps(config, sort_keys=True)}, commit=commit)


def config_from_form(form_items) -> dict[str, dict[str, str]]:
    definitions = _definitions_by_key()
    config: dict[str, dict[str, str]] = {}
    for key, value in form_items:
        if not key.startswith("access__"):
            continue
        parts = key.split("__", 2)
        if len(parts) != 3:
            continue
        _, row_key, role_key = parts
        row = definitions.get(row_key)
        if not row or role_key == Role.SUPER_ADMIN.value:
            continue
        value = str(value).strip()
        if _valid_option(row, value):
            config.setdefault(row_key, {})[role_key] = value
    return config


def _cell(value: str, options: list[AccessOption]) -> AccessCell:
    label, tone = CELL_META.get(value, (value.title(), "generated"))
    return AccessCell(value=value, label=label, tone=tone, options=options)


def role_access_sections(db: Session | None = None) -> list[AccessSection]:
    config = get_role_access_config(db) if db is not None else default_role_access_config()
    sections: list[AccessSection] = []
    for section in access_section_definitions():
        rows = []
        for row in section.rows:
            row_config = config.get(row.key, row.defaults)
            rows.append(
                AccessRow(
                    key=row.key,
                    item=row.item,
                    context=row.context,
                    note=row.note,
                    cells=[_cell(row_config.get(role.key, row.defaults[role.key]), row.options) for role in ROLE_COLUMNS],
                )
            )
        sections.append(AccessSection(section.title, section.context_heading, rows))
    return sections


def _configured_access_values(config: dict[str, dict[str, str]], role: Role | str, access_key: str) -> list[str]:
    values = []
    for role_value in normalize_role_values(role):
        if role_value == Role.SUPER_ADMIN.value:
            values.append("edit")
        else:
            values.append(config.get(access_key, {}).get(role_value, "no"))
    return values


def role_access_value(db: Session, role: Role | str, access_key: str) -> str:
    config = get_role_access_config(db)
    values = _configured_access_values(config, role, access_key)
    if not values:
        return "no"
    return max(values, key=lambda value: ACCESS_VALUE_PRIORITY.get(value, 0))


def _role_has_access_value(
    config: dict[str, dict[str, str]],
    role: Role | str,
    access_key: str,
    allowed_values: set[str] | None = None,
) -> bool:
    values = _configured_access_values(config, role, access_key)
    if allowed_values is not None:
        return any(value not in DENY_VALUES and value in allowed_values for value in values)
    return any(value not in DENY_VALUES for value in values)


def _role_can_open_access_key(config: dict[str, dict[str, str]], role: Role | str, access_key: str) -> bool:
    values = _configured_access_values(config, role, access_key)
    return any(value not in DENY_VALUES for value in values)


def _role_is_super_admin(role: Role | str) -> bool:
    return has_role(role, Role.SUPER_ADMIN)


def configured_role_has_access(
    config: dict[str, dict[str, str]],
    role: Role | str,
    access_key: str,
    allowed_values: set[str] | None = None,
) -> bool:
    return _role_has_access_value(config, role, access_key, allowed_values)


def landing_path_for(config: dict[str, dict[str, str]], role: Role | str) -> str:
    def can(key: str) -> bool:
        return _role_can_open_access_key(config, role, key)

    destinations = [
        ("page_dashboard", "dashboard_data", "/"),
        ("page_batches", "batch_list", "/batches"),
        ("page_batches", "batch_purchase", "/batches/new?batch_type=PURCHASE"),
        ("page_batches", "batch_sale", "/batches/new?batch_type=SALE"),
        ("page_batches", "batch_audit", "/batches/new?batch_type=AUDIT"),
        ("page_warehouse", "stock_relocation", "/warehouse/move"),
        ("page_warehouse", "warehouse_data", "/warehouse/history"),
        ("page_serials", "serial_data", "/serials"),
        ("page_reports", "reports_data", "/reports"),
        ("page_stock_movement", "stock_movement_data", "/stock-movement"),
        ("page_tally_check", "tally_check_edit", "/tally-check"),
        ("page_barcodes", "barcode_assignment", "/barcode-assignment"),
        ("page_barcodes", "barcode_replacement", "/barcode-replacement"),
        ("page_products", "product_master", "/products"),
        ("page_expiry", "expiry_analytics", "/expiry"),
        ("page_settings", "settings_edit", "/settings"),
        ("page_maintenance", "backup_data", "/maintenance"),
        ("page_users", "users_manage", "/users"),
    ]
    for page_key, permission_key, path in destinations:
        if can(page_key) and can(permission_key):
            return path
    if _role_is_super_admin(role):
        return "/settings/access"
    return "/account/password"


def role_has_access(db: Session, role: Role | str, access_key: str, allowed_values: set[str] | None = None) -> bool:
    return configured_role_has_access(get_role_access_config(db), role, access_key, allowed_values)
