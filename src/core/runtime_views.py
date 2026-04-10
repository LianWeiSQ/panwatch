from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.config import Settings
from src.core.discovery_service import get_hot_boards, get_hot_stocks
from src.core.market_data import market_data
from src.core.portfolio_service import (
    build_portfolio_summary,
    collect_quote_items_from_accounts,
    list_enabled_accounts,
    list_watchlist_stocks,
    serialize_account,
)
from src.core.strategy_engine import list_strategy_signals
from src.core.suggestion_pool import get_latest_suggestions
from src.models.market import MARKETS, MarketCode
from src.web.models import (
    AnalysisHistory,
    MarketScanSnapshot,
    PriceAlertRule,
    StrategySignalRun,
)


def _format_datetime(dt) -> str:
    if not dt:
        return ""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


def build_market_status() -> list[dict]:
    result = []
    for market_code in (MarketCode.CN,):
        market_def = MARKETS[market_code]
        now = datetime.now(market_def.get_tz())
        is_trading = market_def.is_trading_time(now)
        sessions = [
            f"{session.start.strftime('%H:%M')}-{session.end.strftime('%H:%M')}"
            for session in market_def.sessions
        ]
        weekday = now.weekday()
        current_time = now.time()
        if weekday >= 5:
            status = "closed"
            status_text = "Weekend"
        elif is_trading:
            status = "trading"
            status_text = "Trading"
        else:
            first_session = market_def.sessions[0]
            last_session = market_def.sessions[-1]
            if current_time < first_session.start:
                status = "pre_market"
                status_text = "Pre-market"
            elif current_time > last_session.end:
                status = "after_hours"
                status_text = "After-hours"
            else:
                status = "break"
                status_text = "Break"
        result.append(
            {
                "code": market_code.value,
                "name": market_def.name,
                "status": status,
                "status_text": status_text,
                "is_trading": is_trading,
                "sessions": sessions,
                "local_time": now.strftime("%H:%M"),
                "timezone": market_def.timezone,
            }
        )
    return result


def _load_latest_history_record(db: Session, agent_name: str) -> dict | None:
    row = (
        db.query(AnalysisHistory)
        .filter(AnalysisHistory.agent_name == agent_name)
        .order_by(
            AnalysisHistory.analysis_date.desc(),
            AnalysisHistory.updated_at.desc(),
            AnalysisHistory.id.desc(),
        )
        .first()
    )
    if not row:
        return None
    return {
        "id": row.id,
        "agent_name": row.agent_name,
        "stock_symbol": row.stock_symbol,
        "analysis_date": row.analysis_date,
        "title": row.title or "",
        "content": row.content,
        "created_at": _format_datetime(row.created_at),
        "updated_at": _format_datetime(row.updated_at),
    }


def _build_price_alert_summary_map(db: Session) -> dict[str, dict[str, int]]:
    rows = db.query(PriceAlertRule).all()
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        stock = row.stock
        if not stock:
            continue
        key = f"{(stock.market or 'CN').upper()}:{(stock.symbol or '').upper()}"
        bucket = summary.setdefault(key, {"total": 0, "enabled": 0})
        bucket["total"] += 1
        if row.enabled:
            bucket["enabled"] += 1
    return summary


def _build_monitor_stocks(
    *,
    watchlist: list[dict],
    quotes_by_key: dict[str, dict],
    portfolio_summary: dict,
    suggestions: dict[str, dict],
) -> list[dict]:
    position_map: dict[str, dict] = {}
    for account in portfolio_summary.get("accounts") or []:
        for position in account.get("positions") or []:
            position_map[f"{position['market']}:{position['symbol']}"] = position

    out = []
    for stock in watchlist:
        key = f"{stock['market']}:{stock['symbol']}"
        quote = quotes_by_key.get(key) or {}
        position = position_map.get(key)
        current_price = quote.get("current_price")
        cost_price = position.get("cost_price") if position else None
        pnl_pct = None
        if current_price is not None and cost_price:
            try:
                pnl_pct = (float(current_price) - float(cost_price)) / float(cost_price) * 100
            except Exception:
                pnl_pct = None
        change_pct = quote.get("change_pct")
        alert_type = None
        if change_pct is not None and abs(float(change_pct)) >= 3.0:
            alert_type = "surge" if float(change_pct) > 0 else "drop"
        suggestion = suggestions.get(key)
        out.append(
            {
                "symbol": stock["symbol"],
                "name": stock["name"],
                "market": stock["market"],
                "current_price": current_price,
                "change_pct": change_pct,
                "open_price": quote.get("open_price"),
                "high_price": quote.get("high_price"),
                "low_price": quote.get("low_price"),
                "volume": quote.get("volume"),
                "turnover": quote.get("turnover"),
                "alert_type": alert_type,
                "has_position": bool(position),
                "cost_price": cost_price,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                "trading_style": position.get("trading_style") if position else None,
                "suggestion": suggestion,
            }
        )
    return out


def _build_action_center() -> dict:
    try:
        opportunities = list_strategy_signals(
            market="",
            status="active",
            min_score=55,
            limit=6,
            snapshot_date="",
            source_pool="all",
            holding="unheld",
            strategy_code="",
            risk_level="all",
            include_payload=False,
        ).get("items", [])
    except Exception:
        opportunities = []
    try:
        risk_items = list_strategy_signals(
            market="",
            status="all",
            min_score=0,
            limit=6,
            snapshot_date="",
            source_pool="all",
            holding="held",
            strategy_code="",
            risk_level="high",
            include_payload=False,
        ).get("items", [])
    except Exception:
        risk_items = []
    return {
        "opportunities": opportunities,
        "risk_items": risk_items,
    }


def _build_data_freshness(db: Session) -> dict:
    latest_strategy_snapshot = (
        db.query(func.max(StrategySignalRun.snapshot_date)).scalar() or ""
    )
    latest_market_scan = (
        db.query(func.max(MarketScanSnapshot.snapshot_date)).scalar() or ""
    )
    latest_history = (
        db.query(func.max(AnalysisHistory.updated_at)).scalar()
    )
    return {
        "strategy_snapshot_date": latest_strategy_snapshot,
        "entry_snapshot_date": latest_strategy_snapshot,
        "market_scan_snapshot_date": latest_market_scan,
        "latest_history_updated_at": _format_datetime(latest_history) if latest_history else "",
    }


def build_dashboard_runtime(
    db: Session,
    *,
    discover_market: str = "CN",
    boards_mode: str = "gainers",
    stocks_mode: str = "turnover",
) -> dict:
    discover_market_value = str(discover_market or "CN").strip().upper() or "CN"
    if discover_market_value != "CN":
        raise ValueError(f"dashboard discovery supports CN only in current mode: {discover_market_value}")
    watchlist_rows = list_watchlist_stocks(db)
    watchlist = [
        {
            "id": row.id,
            "symbol": row.symbol,
            "name": row.name,
            "market": row.market,
            "sort_order": row.sort_order or 0,
            "instrument_id": row.instrument_id,
            "instrument_type": getattr(row.instrument, "instrument_type", "equity"),
            "exchange": getattr(row.instrument, "exchange", "") or "",
            "underlying_symbol": getattr(row.instrument, "underlying_symbol", "") or "",
            "underlying_name": getattr(row.instrument, "underlying_name", "") or "",
            "contract_multiplier": float(
                getattr(row.instrument, "contract_multiplier", None) or 1.0
            ),
            "expiry_date": getattr(row.instrument, "expiry_date", "") or "",
            "is_main_contract": bool(getattr(row.instrument, "is_main_contract", False)),
        }
        for row in watchlist_rows
    ]
    portfolio = build_portfolio_summary(db, include_quotes=True)
    quote_items = collect_quote_items_from_accounts(list_enabled_accounts(db)) + [
        {"symbol": row["symbol"], "market": row["market"]} for row in watchlist
    ]
    quotes = market_data.get_quotes_batch(quote_items)
    quotes_by_key = {
        f"{str(row.get('market') or 'CN').upper()}:{str(row.get('symbol') or '').upper()}": row
        for row in quotes
    }
    stock_keys = [(row["symbol"], row["market"]) for row in watchlist]
    suggestions = get_latest_suggestions(stock_keys=stock_keys, include_expired=True)
    monitor_stocks = _build_monitor_stocks(
        watchlist=watchlist,
        quotes_by_key=quotes_by_key,
        portfolio_summary=portfolio,
        suggestions=suggestions,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_status": build_market_status(),
        "indices": market_data.get_market_indices(),
        "portfolio": portfolio,
        "watchlist": watchlist,
        "quotes": quotes_by_key,
        "monitor_stocks": monitor_stocks,
        "insights": {
            "daily_report": _load_latest_history_record(db, "daily_report"),
            "premarket_outlook": _load_latest_history_record(db, "premarket_outlook"),
            "news_digest": _load_latest_history_record(db, "news_digest"),
        },
        "discovery": {
            "market": "CN",
            "boards_mode": boards_mode,
            "stocks_mode": stocks_mode,
            "boards": get_hot_boards(db, market="CN", mode=boards_mode, limit=12),
            "stocks": get_hot_stocks(
                db,
                market="CN",
                mode="turnover" if stocks_mode == "for_you" else stocks_mode,
                limit=20,
            ),
        },
        "action_center": _build_action_center(),
        "data_freshness": _build_data_freshness(db),
    }


def build_stocks_workspace(db: Session) -> dict:
    accounts = list_enabled_accounts(db)
    watchlist_rows = list_watchlist_stocks(db)
    watchlist = [
        {
            "id": row.id,
            "symbol": row.symbol,
            "name": row.name,
            "market": row.market,
            "sort_order": row.sort_order or 0,
            "instrument_id": row.instrument_id,
            "instrument_type": getattr(row.instrument, "instrument_type", "equity"),
            "exchange": getattr(row.instrument, "exchange", "") or "",
            "underlying_symbol": getattr(row.instrument, "underlying_symbol", "") or "",
            "underlying_name": getattr(row.instrument, "underlying_name", "") or "",
            "contract_multiplier": float(
                getattr(row.instrument, "contract_multiplier", None) or 1.0
            ),
            "expiry_date": getattr(row.instrument, "expiry_date", "") or "",
            "is_main_contract": bool(getattr(row.instrument, "is_main_contract", False)),
            "agents": [
                {
                    "agent_name": agent.agent_name,
                    "schedule": agent.schedule or "",
                    "ai_model_id": agent.ai_model_id,
                    "notify_channel_ids": agent.notify_channel_ids or [],
                }
                for agent in row.agents or []
            ],
        }
        for row in watchlist_rows
    ]
    portfolio = build_portfolio_summary(db, include_quotes=True)
    quote_items = collect_quote_items_from_accounts(accounts) + [
        {"symbol": row["symbol"], "market": row["market"]} for row in watchlist
    ]
    quotes = market_data.get_quotes_batch(quote_items)
    quotes_by_key = {
        f"{str(row.get('market') or 'CN').upper()}:{str(row.get('symbol') or '').upper()}": {
            "current_price": row.get("current_price"),
            "change_pct": row.get("change_pct"),
        }
        for row in quotes
    }
    kline_rows = market_data.get_kline_summary_batch(quote_items)
    kline_summaries = {
        f"{str(row.get('market') or 'CN').upper()}:{str(row.get('symbol') or '').upper()}": row.get("summary") or {}
        for row in kline_rows
    }
    stock_keys = [(row["symbol"], row["market"]) for row in watchlist]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_status": build_market_status(),
        "accounts": [serialize_account(account) for account in accounts],
        "stocks": watchlist,
        "portfolio": portfolio,
        "quotes": quotes_by_key,
        "kline_summaries": kline_summaries,
        "pool_suggestions": get_latest_suggestions(stock_keys=stock_keys, include_expired=True),
        "price_alert_summaries": _build_price_alert_summary_map(db),
    }
