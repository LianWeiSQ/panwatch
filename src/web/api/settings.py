import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import Settings
from src.core.runtime_settings import get_runtime_setting
from src.core.update_checker import check_update
from src.web.database import get_db
from src.web.models import AppSettings

router = APIRouter()


def get_app_version() -> str:
    """获取应用版本号。"""
    version = os.getenv("APP_VERSION")
    if version:
        return version

    possible_paths = [
        "VERSION",
        os.path.join(os.path.dirname(__file__), "../../../VERSION"),
    ]
    for path in possible_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "dev"


class SettingUpdate(BaseModel):
    value: str


class SettingResponse(BaseModel):
    key: str
    value: str
    description: str

    class Config:
        from_attributes = True


SETTING_DESCRIPTIONS = {
    "http_proxy": "HTTP 代理地址",
    "notify_quiet_hours": "通知静默时间段（HH:MM-HH:MM，空为关闭）",
    "notify_retry_attempts": "通知失败重试次数（不含首次）",
    "notify_retry_backoff_seconds": "通知重试退避秒数（基础值）",
    "notify_dedupe_ttl_overrides": "通知幂等窗口覆盖（JSON，可留空）",
}

SETTING_KEYS = list(SETTING_DESCRIPTIONS.keys())


def _get_env_defaults() -> dict[str, str]:
    """从 .env / 环境变量读取当前值作为默认。"""
    s = Settings()
    return {
        "http_proxy": s.http_proxy,
        "notify_quiet_hours": s.notify_quiet_hours,
        "notify_retry_attempts": str(s.notify_retry_attempts),
        "notify_retry_backoff_seconds": str(s.notify_retry_backoff_seconds),
        "notify_dedupe_ttl_overrides": s.notify_dedupe_ttl_overrides,
    }


@router.get("", response_model=list[SettingResponse])
def list_settings(db: Session = Depends(get_db)):
    settings = db.query(AppSettings).all()
    existing_map = {s.key: s for s in settings}

    env_defaults = _get_env_defaults()
    result = []
    for key in SETTING_KEYS:
        desc = SETTING_DESCRIPTIONS.get(key, "")
        env_val = env_defaults.get(key, "")

        if key not in existing_map:
            setting = AppSettings(key=key, value=env_val, description=desc)
            db.add(setting)
            result.append(setting)
        else:
            setting = existing_map[key]
            if not setting.description:
                setting.description = desc
            result.append(setting)

    db.commit()
    return result


@router.put("/{key}", response_model=SettingResponse)
def update_setting(key: str, update: SettingUpdate, db: Session = Depends(get_db)):
    setting = db.query(AppSettings).filter(AppSettings.key == key).first()
    if not setting:
        setting = AppSettings(
            key=key,
            value=update.value,
            description=SETTING_DESCRIPTIONS.get(key, ""),
        )
        db.add(setting)
    else:
        setting.value = update.value

    db.commit()
    db.refresh(setting)
    return setting


@router.get("/version")
def get_version():
    """获取应用版本号。"""
    return {"version": get_app_version()}


@router.get("/update-check")
def get_update_check(db: Session = Depends(get_db)):
    """检查是否有可用新版本（带服务端缓存）。"""
    current = get_app_version()
    proxy = get_runtime_setting("http_proxy", default=Settings().http_proxy or "")
    result = check_update(current, proxy=proxy)
    err = str(result.get("error") or "").strip()
    if err:
        return {
            "success": False,
            "code": 10061,
            "message": err,
        }
    return result
