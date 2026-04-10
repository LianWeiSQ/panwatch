from __future__ import annotations

from typing import Callable

from src.config import Settings


_ENV_READERS: dict[str, Callable[[Settings], str]] = {
    "http_proxy": lambda settings: settings.http_proxy,
    "notify_quiet_hours": lambda settings: settings.notify_quiet_hours,
    "notify_retry_attempts": lambda settings: str(settings.notify_retry_attempts),
    "notify_retry_backoff_seconds": lambda settings: str(settings.notify_retry_backoff_seconds),
    "notify_dedupe_ttl_overrides": lambda settings: settings.notify_dedupe_ttl_overrides,
}


def get_env_default(key: str, default: str = "") -> str:
    reader = _ENV_READERS.get(str(key or "").strip())
    if reader is None:
        return default
    try:
        return str(reader(Settings()) or default)
    except Exception:
        return default


def get_runtime_setting(key: str, default: str = "") -> str:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return default

    try:
        from src.web.database import SessionLocal
        from src.web.models import AppSettings

        db = SessionLocal()
        try:
            setting = (
                db.query(AppSettings)
                .filter(AppSettings.key == normalized_key)
                .first()
            )
            value = str(setting.value or "").strip() if setting else ""
            if value:
                return value
        finally:
            db.close()
    except Exception:
        pass

    return get_env_default(normalized_key, default=default)
