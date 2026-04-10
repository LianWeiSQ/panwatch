from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.config import Settings
from src.core.market_data import market_data
from src.core.notifier import get_global_proxy
from src.web.models import MarketScanSnapshot, Stock

logger = logging.getLogger(__name__)


def resolve_proxy() -> str:
    try:
        return (get_global_proxy() or "").strip() or (Settings().http_proxy or "").strip()
    except Exception:
        return ""


def normalize_market(market: str) -> str:
    value = (market or "CN").strip().upper()
    if value != "CN":
        raise ValueError(f"discovery supports CN only in current mode: {value or 'UNKNOWN'}")
    return "CN"


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
        if parsed != parsed:
            return None
        return parsed
    except Exception:
        return None


def _pick_num(mapping: dict, keys: list[str]) -> float | None:
    for key in keys:
        if key in mapping:
            number = _to_number(mapping.get(key))
            if number is not None:
                return number
    return None


def latest_snapshot_stocks(db: Session, market: str, limit: int = 120) -> list[dict]:
    market_code = normalize_market(market)
    latest = (
        db.query(MarketScanSnapshot.snapshot_date)
        .filter(MarketScanSnapshot.stock_market == market_code)
        .order_by(MarketScanSnapshot.snapshot_date.desc())
        .first()
    )
    if not latest:
        return []
    rows = (
        db.query(MarketScanSnapshot)
        .filter(
            MarketScanSnapshot.stock_market == market_code,
            MarketScanSnapshot.snapshot_date == latest[0],
        )
        .order_by(MarketScanSnapshot.score_seed.desc(), MarketScanSnapshot.updated_at.desc())
        .limit(max(20, min(int(limit), 300)))
        .all()
    )
    out: list[dict] = []
    for row in rows:
        quote = row.quote if isinstance(row.quote, dict) else {}
        out.append(
            {
                "symbol": row.stock_symbol,
                "market": row.stock_market,
                "name": row.stock_name or row.stock_symbol,
                "price": _pick_num(quote, ["price", "current_price", "last", "close"]),
                "change_pct": _pick_num(quote, ["change_pct", "pct_change", "chg_pct"]),
                "turnover": _pick_num(quote, ["turnover", "amount", "turnover_value"]),
                "volume": _pick_num(quote, ["volume", "vol"]),
                "source": "snapshot",
            }
        )
    return out


def watchlist_symbols(db: Session, market: str) -> set[str]:
    market_code = normalize_market(market)
    rows = db.query(Stock.symbol).filter(Stock.market == market_code).all()
    return {str(row[0]).strip().upper() for row in rows if row and row[0]}


def _avg(values: list[float]) -> float | None:
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _sum(values: list[float]) -> float | None:
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return float(sum(valid))


def build_synthetic_boards(
    *,
    market: str,
    stocks: list[dict],
    watchlist: set[str],
    limit: int,
) -> list[dict]:
    market_code = normalize_market(market)
    if not stocks:
        return []
    universe = stocks[: max(30, min(len(stocks), 120))]
    gainers = sorted(universe, key=lambda item: _to_number(item.get("change_pct")) or -999.0, reverse=True)
    turnover = sorted(universe, key=lambda item: _to_number(item.get("turnover")) or 0.0, reverse=True)
    volatility = sorted(universe, key=lambda item: abs(_to_number(item.get("change_pct")) or 0.0), reverse=True)
    watch_related = [item for item in universe if str(item.get("symbol") or "").upper() in watchlist]

    def build_bucket(code: str, name: str, items: list[dict]) -> dict | None:
        if not items:
            return None
        top = items[: min(12, len(items))]
        return {
            "code": f"{market_code}_{code}",
            "name": name,
            "change_pct": _avg([_to_number(item.get("change_pct")) for item in top]),
            "change_amount": None,
            "turnover": _sum([_to_number(item.get("turnover")) for item in top]),
        }

    market_name = {"CN": "A-share"}.get(market_code, market_code)
    buckets = [
        build_bucket("GAINERS", f"{market_name} Gainers", gainers),
        build_bucket("TURNOVER", f"{market_name} Turnover", turnover),
        build_bucket("VOLATILITY", f"{market_name} Volatility", volatility),
        build_bucket("WATCHLIST", f"{market_name} Watchlist", watch_related),
    ]
    return [bucket for bucket in buckets if bucket][: max(1, min(int(limit), 20))]


def stocks_by_synthetic_board(
    *,
    code: str,
    market: str,
    stocks: list[dict],
    watchlist: set[str],
    limit: int,
) -> list[dict]:
    market_code = normalize_market(market)
    suffix = code.replace(f"{market_code}_", "", 1)
    universe = stocks[: max(30, min(len(stocks), 160))]
    if suffix == "GAINERS":
        ranked = sorted(universe, key=lambda item: _to_number(item.get("change_pct")) or -999.0, reverse=True)
    elif suffix == "TURNOVER":
        ranked = sorted(universe, key=lambda item: _to_number(item.get("turnover")) or 0.0, reverse=True)
    elif suffix == "VOLATILITY":
        ranked = sorted(universe, key=lambda item: abs(_to_number(item.get("change_pct")) or 0.0), reverse=True)
    elif suffix == "WATCHLIST":
        ranked = [item for item in universe if str(item.get("symbol") or "").upper() in watchlist]
        ranked = sorted(ranked, key=lambda item: _to_number(item.get("turnover")) or 0.0, reverse=True)
    else:
        ranked = []
    return ranked[: max(1, min(int(limit), 100))]


def get_hot_stocks(db: Session, market: str = "CN", mode: str = "turnover", limit: int = 20) -> list[dict]:
    market_code = normalize_market(market)
    mode_value = (mode or "turnover").lower()
    if mode_value not in {"turnover", "gainers"}:
        raise ValueError(f"unsupported stock mode: {mode_value}")
    proxy = resolve_proxy() or None
    try:
        items = market_data.get_live_hot_stocks(market_code, mode_value, limit, proxy=proxy)
        if items:
            return [{**item, "source": item.get("source") or "live"} for item in items]
    except Exception as exc:
        logger.warning("discovery hot stocks live failed %s/%s: %s", market_code, mode_value, exc)
    return latest_snapshot_stocks(db, market_code, limit=max(limit, 40))


def get_hot_boards(db: Session, market: str = "CN", mode: str = "gainers", limit: int = 12) -> list[dict]:
    market_code = normalize_market(market)
    mode_value = (mode or "gainers").lower()
    if mode_value not in {"gainers", "turnover", "hot"}:
        raise ValueError(f"unsupported board mode: {mode_value}")
    proxy = resolve_proxy() or None

    if market_code == "CN":
        try:
            items = market_data.get_live_hot_boards(market_code, mode_value, limit, proxy=proxy)
            if items:
                return items
        except Exception as exc:
            logger.warning("discovery hot boards live failed %s/%s: %s", market_code, mode_value, exc)

    stocks = get_hot_stocks(
        db,
        market=market_code,
        mode="turnover" if mode_value == "turnover" else "gainers",
        limit=max(50, int(limit) * 10),
    )
    return build_synthetic_boards(
        market=market_code,
        stocks=stocks,
        watchlist=watchlist_symbols(db, market_code),
        limit=limit,
    )


def get_board_stocks(
    db: Session,
    board_code: str,
    mode: str = "gainers",
    limit: int = 20,
    market: str = "CN",
) -> list[dict]:
    code = (board_code or "").strip()
    if not code:
        raise ValueError("missing board code")
    market_code = normalize_market(market)
    mode_value = (mode or "gainers").lower()
    if mode_value not in {"gainers", "turnover", "hot"}:
        raise ValueError(f"unsupported board stock mode: {mode_value}")

    if code.startswith("CN_"):
        synthetic_market = code.split("_", 1)[0]
        stocks = get_hot_stocks(
            db,
            market=synthetic_market,
            mode="turnover" if mode_value == "turnover" else "gainers",
            limit=max(80, int(limit) * 8),
        )
        return stocks_by_synthetic_board(
            code=code,
            market=synthetic_market,
            stocks=stocks,
            watchlist=watchlist_symbols(db, synthetic_market),
            limit=limit,
        )
    if code.startswith(("HK_", "US_")):
        raise ValueError(f"discovery board unsupported in CN-only mode: {code}")

    proxy = resolve_proxy() or None
    try:
        return market_data.get_live_board_stocks(code, mode_value, limit, proxy=proxy)
    except Exception as exc:
        logger.warning("discovery board stocks live failed %s/%s: %s", code, mode_value, exc)
        return []
