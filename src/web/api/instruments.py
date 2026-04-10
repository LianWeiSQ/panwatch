from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.instrument_service import (
    FUTURES_MARKET,
    OPTIONS_MARKET,
    ensure_equity_instrument,
    ensure_future_instrument,
    ensure_option_instrument,
    get_or_create_compat_stock_for_instrument,
    search_future_instruments,
)
from src.web.database import get_db
from src.web.models import Instrument
from src.web.stock_list import search_stocks

router = APIRouter()


class InstrumentEnsureRequest(BaseModel):
    instrument_type: str = ""
    market: str
    symbol: str
    name: str = ""
    exchange: str = ""
    underlying_symbol: str = ""
    underlying_name: str = ""
    contract_multiplier: float | None = None
    tick_size: float | None = None
    expiry_date: str = ""
    is_main_contract: bool | None = None
    option_type: str = ""
    strike_price: float | None = None
    exercise_style: str = ""
    add_to_watchlist: bool = True


def _resolve_instrument_type(instrument_type: str, market: str) -> str:
    type_value = str(instrument_type or "").strip().lower()
    market_value = str(market or "").strip().upper()
    if type_value:
        return type_value
    if market_value == FUTURES_MARKET:
        return "future"
    if market_value == OPTIONS_MARKET:
        return "option"
    return "equity"


def _instrument_to_response(instrument: Instrument) -> dict:
    stock = instrument.stock
    return {
        "id": instrument.id,
        "instrument_type": instrument.instrument_type,
        "market": instrument.market,
        "symbol": instrument.symbol,
        "display_symbol": instrument.display_symbol or instrument.symbol,
        "name": instrument.name,
        "exchange": instrument.exchange or "",
        "currency": instrument.currency or "",
        "underlying_symbol": instrument.underlying_symbol or "",
        "underlying_name": instrument.underlying_name or "",
        "contract_multiplier": instrument.contract_multiplier,
        "tick_size": instrument.tick_size,
        "expiry_date": instrument.expiry_date or "",
        "is_main_contract": bool(instrument.is_main_contract),
        "option_type": instrument.option_type or "",
        "strike_price": instrument.strike_price,
        "exercise_style": instrument.exercise_style or "",
        "status": instrument.status or "",
        "meta": instrument.meta or {},
        "stock": (
            {
                "id": stock.id,
                "symbol": stock.symbol,
                "name": stock.name,
                "market": stock.market,
                "sort_order": stock.sort_order or 0,
            }
            if stock
            else None
        ),
        "runtime_supported": instrument.instrument_type in {"equity", "future"},
    }


@router.get("/search")
def search_instruments(
    q: str = Query("", min_length=1),
    market: str = Query(""),
    instrument_type: str = Query(""),
    limit: int = Query(20, ge=1, le=50),
):
    market_value = str(market or "").strip().upper()
    type_value = _resolve_instrument_type(instrument_type, market_value)

    results: list[dict] = []
    if type_value == "equity":
        if market_value not in {"", "CN"}:
            raise HTTPException(400, f"unsupported market in CN-only mode: {market_value}")
        for item in search_stocks(q, "CN")[:limit]:
            results.append(
                {
                    "instrument_type": "equity",
                    "market": item.get("market"),
                    "symbol": item.get("symbol"),
                    "display_symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "exchange": "",
                    "currency": "CNY" if item.get("market") == "CN" else "HKD" if item.get("market") == "HK" else "USD",
                    "underlying_symbol": "",
                    "underlying_name": "",
                    "contract_multiplier": 1.0,
                    "tick_size": None,
                    "expiry_date": "",
                    "is_main_contract": False,
                    "option_type": "",
                    "strike_price": None,
                    "exercise_style": "",
                }
            )
        return results[:limit]

    if type_value == "future":
        return search_future_instruments(q, exchange="", limit=limit)

    return []


@router.post("/ensure")
def ensure_instrument(payload: InstrumentEnsureRequest, db: Session = Depends(get_db)):
    market_value = str(payload.market or "").strip().upper()
    if not market_value:
        raise HTTPException(400, "market is required")
    if market_value in {"HK", "US"}:
        raise HTTPException(400, f"unsupported market in CN-only mode: {market_value}")
    symbol = str(payload.symbol or "").strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")

    type_value = _resolve_instrument_type(payload.instrument_type, market_value)
    if type_value == "equity":
        instrument = ensure_equity_instrument(
            db,
            symbol=symbol,
            name=payload.name or symbol,
            market=market_value,
        )
    elif type_value == "future":
        instrument = ensure_future_instrument(
            db,
            symbol=symbol,
            name=payload.name,
            exchange=payload.exchange,
            underlying_symbol=payload.underlying_symbol,
            underlying_name=payload.underlying_name,
            contract_multiplier=payload.contract_multiplier,
            tick_size=payload.tick_size,
            expiry_date=payload.expiry_date,
            is_main_contract=payload.is_main_contract,
        )
    elif type_value == "option":
        instrument = ensure_option_instrument(
            db,
            symbol=symbol,
            name=payload.name,
            exchange=payload.exchange,
            underlying_symbol=payload.underlying_symbol,
            underlying_name=payload.underlying_name,
            contract_multiplier=payload.contract_multiplier,
            tick_size=payload.tick_size,
            expiry_date=payload.expiry_date,
            option_type=payload.option_type,
            strike_price=payload.strike_price,
            exercise_style=payload.exercise_style,
        )
    else:
        raise HTTPException(400, f"unsupported instrument_type: {type_value}")

    if payload.add_to_watchlist:
        get_or_create_compat_stock_for_instrument(db, instrument)

    db.commit()
    db.refresh(instrument)
    return _instrument_to_response(instrument)


@router.get("/{instrument_id}")
def get_instrument(instrument_id: int, db: Session = Depends(get_db)):
    instrument = db.query(Instrument).filter(Instrument.id == instrument_id).first()
    if not instrument:
        raise HTTPException(404, "instrument not found")
    return _instrument_to_response(instrument)
