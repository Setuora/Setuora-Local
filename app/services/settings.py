import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Company, Setting


COMPANY_SETTING_KEYS = [
    "company_name",
    "tally_host",
    "tally_port",
    "sales_voucher_type",
    "purchase_voucher_type",
    "sales_ledger_name",
    "purchase_ledger_name",
    "cgst_ledger_name",
    "sgst_ledger_name",
    "sales_gst_ledger_mappings",
    "round_off_ledger_name",
]


DEFAULT_SETTINGS = {
    "company_name": "",
    "tally_enabled": "false",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "",
    "purchase_voucher_type": "",
    "sales_ledger_name": "",
    "purchase_ledger_name": "",
    "cgst_ledger_name": "",
    "sgst_ledger_name": "",
    "sales_gst_ledger_mappings": "",
    "round_off_ledger_name": "",
    "retry_interval_seconds": "180",
    "movement_analysis_days": "90",
    "movement_dead_below_pct": "10",
    "movement_slow_below_pct": "40",
    "movement_medium_up_to_pct": "80",
}

LEGACY_PLACEHOLDER_SETTINGS = {
    "company_name": "SWARNAGOWRI",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales @ 5%",
    "purchase_ledger_name": "Purchase @ 5%",
    "cgst_ledger_name": "Input CGST @  2.5 %",
    "sgst_ledger_name": "Input SGST@2.5%",
    "sales_gst_ledger_mappings": "",
    "round_off_ledger_name": "ROUND OFF",
}


def gst_rate_key(value: object) -> str:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid GST rate '{value}'.") from exc
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError(f"GST rate '{value}' must be between 0 and 100.")
    return format(rate.normalize(), "f")


def parse_sales_gst_ledger_mappings(raw: str | None) -> dict[str, dict[str, str]]:
    mappings: dict[str, dict[str, str]] = {}
    for line_number, raw_line in enumerate((raw or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in {4, 5}:
            raise ValueError(
                f"Sales GST ledger mapping line {line_number} must contain: "
                "GST rate | Sales ledger | CGST ledger | SGST ledger | IGST ledger."
            )
        rate, sales_ledger, cgst_ledger, sgst_ledger = parts[:4]
        igst_ledger = parts[4] if len(parts) == 5 else ""
        key = gst_rate_key(rate)
        required_ledgers = (sales_ledger, cgst_ledger, sgst_ledger)
        if not all(required_ledgers) or (len(parts) == 5 and not igst_ledger):
            raise ValueError(f"Sales GST ledger mapping line {line_number} has an empty ledger name.")
        if key in mappings:
            raise ValueError(f"GST rate {key}% is listed more than once.")
        mappings[key] = {
            "sales": sales_ledger,
            "cgst": cgst_ledger,
            "sgst": sgst_ledger,
            "igst": igst_ledger,
        }
    return mappings


def ensure_default_settings(db: Session) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        if not db.get(Setting, key):
            db.add(Setting(key=key, value=value))
    db.commit()


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    if row:
        return row.value
    return DEFAULT_SETTINGS.get(key, default)


def get_all_settings(db: Session) -> dict[str, str]:
    values = DEFAULT_SETTINGS.copy()
    for row in db.query(Setting).all():
        values[row.key] = row.value
    return values


def _apply_settings(db: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        row = db.get(Setting, key)
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))


def update_settings(db: Session, values: dict[str, str], *, commit: bool = True) -> None:
    _apply_settings(db, values)
    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise


def clear_legacy_placeholder_settings(db: Session) -> None:
    if get_setting(db, "tally_enabled", "false").lower() == "true":
        return
    if any(get_setting(db, key, "") != value for key, value in LEGACY_PLACEHOLDER_SETTINGS.items()):
        return

    for key in COMPANY_SETTING_KEYS:
        row = db.get(Setting, key)
        if row:
            row.value = DEFAULT_SETTINGS[key]

    legacy_companies = db.scalars(select(Company).where(Company.name == LEGACY_PLACEHOLDER_SETTINGS["company_name"])).all()
    for company in legacy_companies:
        try:
            config = json.loads(company.config)
        except (TypeError, ValueError):
            config = {}
        if all(config.get(key) == LEGACY_PLACEHOLDER_SETTINGS[key] for key in COMPANY_SETTING_KEYS):
            db.delete(company)
    db.commit()


def is_tally_enabled(db: Session) -> bool:
    return get_setting(db, "tally_enabled", "false").lower() == "true"


def current_company_config(db: Session) -> dict[str, str]:
    return {key: get_setting(db, key) for key in COMPANY_SETTING_KEYS}


def list_companies(db: Session) -> list[Company]:
    return list(db.scalars(select(Company).order_by(Company.name)).all())


def get_active_company(db: Session) -> Company | None:
    return db.scalar(select(Company).where(Company.is_active.is_(True)))


def company_config(company: Company) -> dict[str, str]:
    try:
        stored = json.loads(company.config)
    except (TypeError, ValueError):
        stored = {}
    return {
        key: str(stored.get(key, DEFAULT_SETTINGS[key]) or "")
        for key in COMPANY_SETTING_KEYS
    }


def ensure_company_records(db: Session) -> None:
    if db.scalar(select(Company.id).limit(1)):
        if not get_active_company(db):
            first = db.scalar(select(Company).order_by(Company.id))
            first.is_active = True
            db.commit()
        return
    config = current_company_config(db)
    name = (config.get("company_name") or "").strip()
    if not name:
        return
    db.add(Company(name=name, config=json.dumps(config), is_active=True))
    db.commit()


def validate_company_fields(config: dict[str, str]) -> str | None:
    if not config.get("company_name", "").strip():
        return "Company name is required."
    if not config.get("tally_host", "").strip():
        return "Tally host is required."
    port = config.get("tally_port", "").strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        return "Tally port must be a whole number between 1 and 65535."
    try:
        parse_sales_gst_ledger_mappings(config.get("sales_gst_ledger_mappings"))
    except ValueError as exc:
        return str(exc)
    return None


def add_company(db: Session, name: str, config: dict[str, str], *, commit: bool = True) -> Company:
    clean = {key: (config.get(key, "") or "").strip() for key in COMPANY_SETTING_KEYS}
    label = (name or clean["company_name"]).strip()
    if not label:
        return _raise("A company label is required.")
    if db.scalar(select(Company).where(Company.name == label)):
        return _raise(f"A company named '{label}' already exists.")
    error = validate_company_fields(clean)
    if error:
        return _raise(error)
    is_first = db.scalar(select(Company.id).limit(1)) is None
    company = Company(name=label, config=json.dumps(clean), is_active=is_first)
    db.add(company)
    if is_first:
        _apply_settings(db, clean)
    try:
        db.flush()
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise
    if commit:
        db.refresh(company)
    return company


def update_company(db: Session, company_id: int, name: str, config: dict[str, str], *, commit: bool = True) -> Company:
    company = db.get(Company, company_id)
    if not company:
        return _raise("Company not found.")
    clean = {key: (config.get(key, "") or "").strip() for key in COMPANY_SETTING_KEYS}
    label = (name or clean["company_name"]).strip()
    if not label:
        return _raise("A company label is required.")
    duplicate = db.scalar(
        select(Company).where(Company.name == label, Company.id != company.id)
    )
    if duplicate:
        return _raise(f"A company named '{label}' already exists.")
    error = validate_company_fields(clean)
    if error:
        return _raise(error)
    company.name = label
    company.config = json.dumps(clean)
    if company.is_active:
        _apply_settings(db, clean)
    try:
        db.flush()
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise
    if commit:
        db.refresh(company)
    return company


def activate_company(db: Session, company_id: int, *, commit: bool = True) -> None:
    company = db.get(Company, company_id)
    if not company:
        return _raise("Company not found.")
    for other in list_companies(db):
        other.is_active = other.id == company.id
    config = company_config(company)
    _apply_settings(db, {**config, "tally_enabled": "false"})
    try:
        db.flush()
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise


def delete_company(db: Session, company_id: int, *, commit: bool = True) -> None:
    company = db.get(Company, company_id)
    if not company:
        return _raise("Company not found.")
    if (db.scalar(select(func.count(Company.id))) or 0) <= 1:
        return _raise("At least one company is required.")
    if company.is_active:
        return _raise("Activate another company before deleting this one.")
    db.delete(company)
    try:
        db.flush()
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise


def save_active_company_config(db: Session, config: dict[str, str], *, commit: bool = True) -> None:
    company = get_active_company(db)
    if not company:
        return
    clean = {key: (config.get(key, "") or "").strip() for key in COMPANY_SETTING_KEYS}
    company.config = json.dumps(clean)
    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise


def persist_settings_and_active_company(db: Session, values: dict[str, str], *, commit: bool = True) -> None:
    _apply_settings(db, values)
    save_active_company_config(db, values, commit=False)
    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise


def _raise(message: str):
    raise ValueError(message)
