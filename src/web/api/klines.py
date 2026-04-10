from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.market_data import market_data
from src.models.market import MarketCode

router = APIRouter()


class KlineItem(BaseModel):
    symbol: str = Field(..., description="stock symbol")
    market: str = Field(..., description="market: CN/CN_FUT/CN_OPT")
    days: int | None = Field(default=60, description="day count")
    interval: str | None = Field(default="1d", description="1d/1w/1m")


class KlineBatchRequest(BaseModel):
    items: list[KlineItem]


class KlineSummaryItem(BaseModel):
    symbol: str = Field(..., description="stock symbol")
    market: str = Field(..., description="market: CN/CN_FUT/CN_OPT")


class KlineSummaryBatchRequest(BaseModel):
    items: list[KlineSummaryItem]


def _parse_market(market: str) -> MarketCode:
    try:
        market_code = MarketCode(str(market or "CN").upper())
    except ValueError:
        raise HTTPException(400, f"unsupported market: {market}")
    if market_code in {MarketCode.HK, MarketCode.US}:
        raise HTTPException(400, f"unsupported market in CN-only mode: {market_code.value}")
    return market_code


def _serialize_klines(klines) -> list[dict]:
    return [
        {
            "date": k.date,
            "open": k.open,
            "close": k.close,
            "high": k.high,
            "low": k.low,
            "volume": k.volume,
        }
        for k in klines
    ]


def _aggregate_klines(klines, interval: str) -> list:
    value = (interval or "1d").lower()
    if value in {"1d", "day", "d"}:
        return klines
    if value not in {"1w", "1m", "week", "month", "w", "m"}:
        return klines

    parsed = []
    for row in klines or []:
        try:
            dt = datetime.strptime(row.date, "%Y-%m-%d")
        except Exception:
            continue
        parsed.append((dt, row))

    parsed.sort(key=lambda item: item[0])
    buckets: dict[str, list] = {}
    for dt, row in parsed:
        if value in {"1w", "week", "w"}:
            year, week, _ = dt.isocalendar()
            key = f"{year:04d}-W{week:02d}"
        else:
            key = f"{dt.year:04d}-{dt.month:02d}"
        buckets.setdefault(key, []).append((dt, row))

    out = []
    for _, items in buckets.items():
        items.sort(key=lambda item: item[0])
        first = items[0][1]
        last = items[-1][1]
        out.append(
            type(first)(
                date=items[-1][0].strftime("%Y-%m-%d"),
                open=first.open,
                close=last.close,
                high=max(item[1].high for item in items),
                low=min(item[1].low for item in items),
                volume=sum(item[1].volume for item in items),
            )
        )
    out.sort(key=lambda row: row.date)
    return out


@router.get("/{symbol}")
def get_klines(symbol: str, market: str = "CN", days: int = 60, interval: str = "1d"):
    market_code = _parse_market(market)
    klines = market_data.get_klines(symbol, market_code, days=days)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "days": days,
        "interval": interval,
        "klines": _serialize_klines(_aggregate_klines(klines, interval)),
    }


@router.post("/batch")
def get_klines_batch(payload: KlineBatchRequest):
    if not payload.items:
        return []
    results = []
    for item in payload.items:
        market_code = _parse_market(item.market)
        days = item.days or 60
        interval = item.interval or "1d"
        klines = market_data.get_klines(item.symbol, market_code, days=days)
        results.append(
            {
                "symbol": item.symbol,
                "market": market_code.value,
                "days": days,
                "interval": interval,
                "klines": _serialize_klines(_aggregate_klines(klines, interval)),
            }
        )
    return results


@router.get("/{symbol}/summary")
def get_kline_summary(symbol: str, market: str = "CN"):
    market_code = _parse_market(market)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "summary": market_data.get_kline_summary(symbol, market_code),
    }


@router.post("/summary/batch")
def get_kline_summary_batch(payload: KlineSummaryBatchRequest):
    if not payload.items:
        return []
    items = [
        {"symbol": item.symbol, "market": _parse_market(item.market).value}
        for item in payload.items
    ]
    return market_data.get_kline_summary_batch(items)
