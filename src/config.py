from pathlib import Path
from dataclasses import dataclass, field

import yaml
from pydantic_settings import BaseSettings
from pydantic import Field, AliasChoices

from src.models.market import MarketCode


class Settings(BaseSettings):
    """环境变量配置"""

    # AI
    ai_base_url: str = Field(
        default="https://open.bigmodel.cn/api/paas/v4",
        validation_alias=AliasChoices(
            "AI_BASE_URL",
            "PANWATCH_AI_BASE_URL",
            "OPENAI_BASE_URL",
        ),
    )
    ai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_API_KEY",
            "PANWATCH_AI_API_KEY",
            "OPENAI_API_KEY",
        ),
    )
    ai_model: str = Field(
        default="glm-4",
        validation_alias=AliasChoices(
            "AI_MODEL",
            "PANWATCH_AI_MODEL",
            "OPENAI_MODEL",
        ),
    )

    # Telegram
    notify_telegram_bot_token: str = ""
    notify_telegram_chat_id: str = ""

    # 代理
    http_proxy: str = Field(
        default="",
        validation_alias=AliasChoices(
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "PANWATCH_HTTP_PROXY",
        ),
    )
    tushare_token: str = Field(
        default="",
        validation_alias=AliasChoices("TUSHARE_TOKEN", "PANWATCH_TUSHARE_TOKEN"),
    )
    tushare_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TUSHARE_BASE_URL",
            "PANWATCH_TUSHARE_BASE_URL",
        ),
    )

    # Redis / runtime infra
    redis_url: str = ""
    service_role: str = Field(
        default="all",
        validation_alias=AliasChoices("PANWATCH_SERVICE_ROLE", "SERVICE_ROLE"),
    )
    market_warm_interval_seconds: int = 45
    market_warm_discovery_interval_seconds: int = 300

    # 通知策略（可通过 UI 的“系统设置”覆盖）
    # 静默时间段（本地时区），格式: HH:MM-HH:MM，空为关闭；跨夜示例: 23:00-07:00
    notify_quiet_hours: str = ""
    # 通知失败重试次数（不含首次尝试）
    notify_retry_attempts: int = 2
    # 重试退避秒数（基数），实际会按 1x,2x,... 递增
    notify_retry_backoff_seconds: float = 2.0
    # 幂等窗口覆盖（JSON），示例: {"news_digest":60,"daily_report":720}
    notify_dedupe_ttl_overrides: str = ""

    # SSL 证书（企业环境）
    ca_cert_file: str = ""

    # 调度
    # day_of_week 使用 POSIX cron 语义(1-5=周一到周五)
    daily_report_cron: str = "30 15 * * 1-5"

    # 默认时区（用于调度、时间展示等）。
    # 统一使用一个环境变量控制：TZ（默认 Asia/Shanghai）。
    # 建议使用 IANA 时区名，如 Asia/Shanghai, America/New_York。
    app_timezone: str = Field(
        default="Asia/Shanghai",
        validation_alias=AliasChoices("TZ", "APP_TIMEZONE"),
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@dataclass
class StockConfig:
    """自选股配置"""

    symbol: str
    name: str
    market: MarketCode


@dataclass
class AppConfig:
    """应用完整配置"""

    settings: Settings
    watchlist: list[StockConfig] = field(default_factory=list)


def load_watchlist(path: str | Path = "config/watchlist.yaml") -> list[StockConfig]:
    """从 YAML 加载自选股列表"""
    path = Path(path)
    if not path.exists():
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    stocks = []
    for market_group in data.get("markets", []):
        market_code = MarketCode(market_group["code"])
        for stock in market_group.get("stocks", []):
            stocks.append(
                StockConfig(
                    symbol=stock["symbol"],
                    name=stock["name"],
                    market=market_code,
                )
            )

    return stocks


def load_config() -> AppConfig:
    """加载完整配置"""
    settings = Settings()
    watchlist = load_watchlist()
    return AppConfig(settings=settings, watchlist=watchlist)
