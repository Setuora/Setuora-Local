from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import threading

from app.config import PROJECT_ROOT, get_settings


ENV_FILE = PROJECT_ROOT / ".env"
BACKUP_ENV_KEYS = (
    "AUTOMATIC_BACKUPS_ENABLED",
    "BACKUP_DIRECTORY",
    "BACKUP_OFFSITE_DIRECTORY",
    "BACKUP_INTERVAL_HOURS",
    "BACKUP_RETENTION_COUNT",
)
BACKUP_FILE_PATTERNS = ("setuora-backup-*.db", "setu-backup-*.db")
_SQLITE_FILE_MAINTENANCE_LOCK = threading.RLock()


@dataclass(frozen=True)
class BackupInfo:
    filename: str
    data: bytes


@dataclass(frozen=True)
class BackupFileInfo:
    filename: str
    path: Path
    size_bytes: int
    verified_at: datetime
    offsite_path: Path | None = None


@dataclass(frozen=True)
class BackupStatus:
    enabled: bool
    directory: Path
    interval_hours: int
    retention_count: int
    latest_backup: Path | None
    latest_backup_size_bytes: int | None
    offsite_directory: Path | None
    offsite_latest_backup: Path | None
    backup_files: tuple[Path, ...]


@dataclass(frozen=True)
class RestoreInfo:
    restored_path: Path
    safety_backup_path: Path
    source_path: Path
    restored_at: datetime


def sqlite_database_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise RuntimeError("Backup download is only available for SQLite deployments")
    return Path(url.replace("sqlite:///", "", 1)).resolve()


def resolve_configured_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def verify_sqlite_backup(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"Backup file does not exist: {path}")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for backup: {integrity}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"SQLite foreign-key check failed for backup: {violations[:5]}")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"SQLite backup verification failed for {path}: {exc}") from exc
    finally:
        connection.close()


def verify_setuora_backup(path: Path) -> None:
    verify_sqlite_backup(path)
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    missing = {"users", "settings"} - tables
    if missing:
        raise RuntimeError("Backup is a valid SQLite file, but it is not a Setuora database backup.")


def _copy_sqlite_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(source_path)
    try:
        target = sqlite3.connect(destination_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def create_sqlite_backup() -> BackupInfo:
    with _SQLITE_FILE_MAINTENANCE_LOCK:
        source_path = sqlite_database_path()
        if not source_path.exists():
            raise RuntimeError("SQLite database file does not exist yet")

        # NamedTemporaryFile keeps an open Windows handle that sqlite3 cannot reuse.
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "setuora-backup.db"
            _copy_sqlite_database(source_path, temp_path)
            verify_sqlite_backup(temp_path)
            data = temp_path.read_bytes()

        return BackupInfo(filename="setuora-backup.db", data=data)


def create_scheduled_backup() -> BackupFileInfo:
    with _SQLITE_FILE_MAINTENANCE_LOCK:
        source_path = sqlite_database_path()
        if not source_path.exists():
            raise RuntimeError("SQLite database file does not exist yet")

        settings = get_settings()
        backup_dir = resolve_configured_path(getattr(settings, "backup_directory", "./data/backups"))
        backup_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        filename = f"setuora-backup-{stamp}.db"
        destination = backup_dir / filename
        temp_destination = backup_dir / f".{filename}.tmp"
        if temp_destination.exists():
            temp_destination.unlink()

        _copy_sqlite_database(source_path, temp_destination)
        verify_sqlite_backup(temp_destination)
        temp_destination.replace(destination)

        offsite_path = _copy_to_offsite(destination, filename, settings)
        _prune_backups(backup_dir, getattr(settings, "backup_retention_count", 14))
        offsite_dir = getattr(settings, "backup_offsite_directory", "").strip()
        if offsite_dir:
            _prune_backups(resolve_configured_path(offsite_dir), getattr(settings, "backup_retention_count", 14))

        return BackupFileInfo(
            filename=filename,
            path=destination,
            size_bytes=destination.stat().st_size,
            verified_at=datetime.now(timezone.utc),
            offsite_path=offsite_path,
        )


def update_backup_settings(
    *,
    enabled: bool,
    backup_directory: str,
    interval_hours: str,
    retention_count: str,
    offsite_directory: str,
) -> BackupStatus:
    directory = backup_directory.strip()
    offsite = offsite_directory.strip()
    if not directory:
        raise ValueError("Backup folder is required.")

    interval = _positive_int(interval_hours, "Schedule", minimum=1, maximum=168)
    retention = _positive_int(retention_count, "Retention", minimum=1, maximum=365)

    backup_dir = resolve_configured_path(directory)
    if backup_dir.exists() and not backup_dir.is_dir():
        raise ValueError("Backup folder must be a directory.")

    if offsite:
        offsite_dir = resolve_configured_path(offsite)
        if offsite_dir.exists() and not offsite_dir.is_dir():
            raise ValueError("Off-machine copy path must be a directory.")
        if offsite_dir == backup_dir:
            raise ValueError("Off-machine copy path must be different from the backup folder.")
    else:
        offsite_dir = None

    _persist_env_values(
        {
            "AUTOMATIC_BACKUPS_ENABLED": "true" if enabled else "false",
            "BACKUP_DIRECTORY": directory,
            "BACKUP_OFFSITE_DIRECTORY": offsite,
            "BACKUP_INTERVAL_HOURS": str(interval),
            "BACKUP_RETENTION_COUNT": str(retention),
        }
    )

    backup_dir.mkdir(parents=True, exist_ok=True)
    if offsite_dir:
        offsite_dir.mkdir(parents=True, exist_ok=True)
    return backup_status()


def backup_status() -> BackupStatus:
    settings = get_settings()
    backup_dir = resolve_configured_path(getattr(settings, "backup_directory", "./data/backups"))
    offsite_value = getattr(settings, "backup_offsite_directory", "").strip()
    offsite_dir = resolve_configured_path(offsite_value) if offsite_value else None
    backup_files = tuple(list_backup_files())
    latest = _latest_backup(backup_dir)
    offsite_latest = _latest_backup(offsite_dir) if offsite_dir else None
    return BackupStatus(
        enabled=getattr(settings, "automatic_backups_enabled", True),
        directory=backup_dir,
        interval_hours=max(1, int(getattr(settings, "backup_interval_hours", 24))),
        retention_count=max(1, int(getattr(settings, "backup_retention_count", 14))),
        latest_backup=latest,
        latest_backup_size_bytes=latest.stat().st_size if latest and latest.exists() else None,
        offsite_directory=offsite_dir,
        offsite_latest_backup=offsite_latest,
        backup_files=backup_files,
    )


def list_backup_files() -> list[Path]:
    settings = get_settings()
    directories = [resolve_configured_path(getattr(settings, "backup_directory", "./data/backups"))]
    offsite_value = getattr(settings, "backup_offsite_directory", "").strip()
    if offsite_value:
        directories.append(resolve_configured_path(offsite_value))

    files: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        for backup in _backup_files(directory):
            resolved = backup.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(backup)
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def backup_choice_path(value: str) -> Path:
    requested = Path(value).expanduser().resolve()
    choices = {path.resolve(): path for path in list_backup_files()}
    if requested not in choices:
        raise RuntimeError("Choose a backup from the configured backup folders.")
    return choices[requested]


def restore_sqlite_backup_file(source_path: Path, *, reload_runtime: bool = True) -> RestoreInfo:
    with _SQLITE_FILE_MAINTENANCE_LOCK:
        source = source_path.expanduser().resolve()
        database_path = sqlite_database_path()
        if not database_path.exists():
            raise RuntimeError("SQLite database file does not exist yet")
        verify_setuora_backup(source)

        with TemporaryDirectory() as temp_dir:
            staged_source = Path(temp_dir) / "setuora-restore-source.db"
            shutil.copy2(source, staged_source)
            verify_setuora_backup(staged_source)

            safety_backup = create_scheduled_backup()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            temp_destination = database_path.with_name(f".{database_path.name}.restore-{stamp}.tmp")
            if temp_destination.exists():
                temp_destination.unlink()
            shutil.copy2(staged_source, temp_destination)
            verify_setuora_backup(temp_destination)

            if reload_runtime:
                from app.database import engine

                engine.dispose()

            _remove_sqlite_sidecars(database_path)
            temp_destination.replace(database_path)
            _remove_sqlite_sidecars(database_path)

            if reload_runtime:
                _reload_runtime_database()

        return RestoreInfo(
            restored_path=database_path,
            safety_backup_path=safety_backup.path,
            source_path=source,
            restored_at=datetime.now(timezone.utc),
        )


def _copy_to_offsite(source: Path, filename: str, settings: object) -> Path | None:
    offsite_value = getattr(settings, "backup_offsite_directory", "").strip()
    if not offsite_value:
        return None

    offsite_dir = resolve_configured_path(offsite_value)
    offsite_dir.mkdir(parents=True, exist_ok=True)
    destination = offsite_dir / filename
    temp_destination = offsite_dir / f".{filename}.tmp"
    if temp_destination.exists():
        temp_destination.unlink()
    shutil.copy2(source, temp_destination)
    verify_sqlite_backup(temp_destination)
    temp_destination.replace(destination)
    return destination


def _backup_files(directory: Path | None) -> list[Path]:
    if not directory or not directory.exists():
        return []
    files = {
        backup
        for pattern in BACKUP_FILE_PATTERNS
        for backup in directory.glob(pattern)
        if backup.is_file()
    }
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _latest_backup(directory: Path | None) -> Path | None:
    files = _backup_files(directory)
    return files[0] if files else None


def _prune_backups(directory: Path, retention_count: int) -> None:
    keep = max(1, int(retention_count))
    for backup in _backup_files(directory)[keep:]:
        backup.unlink(missing_ok=True)


def _positive_int(raw: str, label: str, *, minimum: int, maximum: int) -> int:
    value = raw.strip()
    if not value.isdigit():
        raise ValueError(f"{label} must be a whole number.")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _persist_env_values(values: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for raw_line in lines:
        key = _env_line_key(raw_line)
        if key in values:
            output.append(f"{key}={_format_env_value(values[key])}")
            seen.add(key)
        else:
            output.append(raw_line)

    if output and output[-1].strip():
        output.append("")
    for key in BACKUP_ENV_KEYS:
        if key not in seen:
            output.append(f"{key}={_format_env_value(values[key])}")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    for key, value in values.items():
        os.environ[key] = value
    get_settings.cache_clear()


def _env_line_key(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key = line.split("=", 1)[0].strip().lstrip("\ufeff")
    if key.startswith("export "):
        key = key.removeprefix("export ").strip()
    return key or None


def _format_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Backup settings cannot contain line breaks.")
    if '"' in value:
        raise ValueError('Backup settings cannot contain double quotes.')
    if not value:
        return ""
    if value.strip() != value or any(character.isspace() for character in value):
        return f'"{value}"'
    return value


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        database_path.with_name(f"{database_path.name}{suffix}").unlink(missing_ok=True)


def _reload_runtime_database() -> None:
    from app.database import Base, SessionLocal, engine
    from app.services.bootstrap import bootstrap
    from app.services.schema import ensure_runtime_schema

    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema(engine)
    with SessionLocal() as db:
        bootstrap(db)
