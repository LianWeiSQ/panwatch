from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.market_data import market_data
from src.models.market import MarketCode

router = APIRouter()


class QuoteItem(BaseModel):
    symbol: str = Field(..., description="stock symbol")
    market: str = Field(..., description="market: CN/CN_FUT/CN_OPT")


class QuoteBatchRequest(BaseModel):
    items: list[QuoteItem]


def _parse_market(market: str) -> MarketCode:
    try:
        market_code = MarketCode(str(market or "CN").upper())
    except ValueError:
        raise HTTPException(400, f"unsupported market: {market}")
    if market_code in {MarketCode.HK, MarketCode.US}:
        raise HTTPException(400, f"unsupported market in CN-only mode: {market_code.value}")
    return market_code


@router.get("/{symbol}")
def get_quote(symbol: str, market: str = "CN"):
    quote = market_data.get_quote(symbol, _parse_market(market))
    if not quote:
        raise HTTPException(404, "quote not found")
    return quote


@router.post("/batch")
def get_quotes_batch(payload: QuoteBatchRequest):
    if not payload.items:
        return []
    items = [
        {"symbol": item.symbol, "market": _parse_market(item.market).value}
        for item in payload.items
    ]
    return market_data.get_quotes_batch(items)
