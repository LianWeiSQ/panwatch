from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import akshare as ak
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.market import MarketCode
from src.core.option_service import resolve_option_contract
from src.core.tushare_futures import (
    TushareUnavailable,
    resolve_tushare_future_contract,
    search_tushare_future_instruments,
)
from src.web.models import Instrument, Stock

logger = logging.getLogger(__name__)

FUTURES_MARKET = MarketCode.CN_FUT.value
OPTIONS_MARKET = MarketCode.CN_OPT.value

_PRODUCT_CACHE_TTL_SECONDS = 60 * 60 * 6
_CONTRACT_CACHE_TTL_SECONDS = 60 * 5
_CONTRACT_INDEX_TTL_SECONDS = 60 * 5

_PRODUCT_CACHE: dict[str, Any] = {"ts": 0.0, "items": []}
_PRODUCT_LOCK = threading.Lock()
_CONTRACT_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CONTRACT_LOCK = threading.Lock()
_CONTRACT_INDEX_CACHE: dict[str, Any] = {"ts": 0.0, "items": {}}
_CONTRACT_INDEX_LOCK = threading.Lock()

_CONTRACT_SYMBOL_RE = re.compile(r"^(?P<prefix>[A-Z]{1,4})(?P<year>\d{2})(?P<month>\d{2})$")

_EXCHANGE_CODE_MAP = {
    "上海期货交易所": "SHFE",
    "大连商品交易所": "DCE",
    "郑州商品交易所": "CZCE",
    "上海国际能源交易中心": "INE",
    "广州期货交易所": "GFEX",
    "中国金融期货交易所": "CFFEX",
    "shfe": "SHFE",
    "dce": "DCE",
    "czce": "CZCE",
    "ine": "INE",
    "gfex": "GFEX",
    "cffex": "CFFEX",
}

_EXCHANGE_ALIASES = {
    "SHFE": {"shfe", "上期所", "上海期货", "上海期货交易所"},
    "DCE": {"dce", "大商所", "大连商品", "大连商品交易所"},
    "CZCE": {"czce", "郑商所", "郑州商品", "郑州商品交易所"},
    "INE": {"ine", "上期能源", "能源中心", "上海国际能源交易中心"},
    "GFEX": {"gfex", "广期所", "广州期货", "广州期货交易所"},
    "CFFEX": {"cffex", "中金所", "金融期货", "中国金融期货交易所"},
}


@dataclass(frozen=True)
class EnsureInstrumentResult:
    instrument: Instrument
    stock: Stock | None


def _normalize_exchange(exchange: str | None) -> str:
    value = str(exchange or "").strip()
    if not value:
        return ""
    return _EXCHANGE_CODE_MAP.get(value, value.upper())


def _default_currency_for_market(market: str) -> str:
    return "CNY"


def _normalize_symbol(symbol: str, market: str) -> str:
    value = str(symbol or "").strip()
    market_value = str(market or MarketCode.CN.value).upper()
    if market_value in {FUTURES_MARKET, OPTIONS_MARKET}:
        return value.upper()
    return value


def _parse_expiry_from_contract(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    match = _CONTRACT_SYMBOL_RE.match(text)
    if not match:
        return ""
    year = 2000 + int(match.group("year"))
    month = int(match.group("month"))
    if month < 1 or month > 12:
        return ""
    return f"{year:04d}-{month:02d}"


def _extract_contract_prefix(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    match = re.match(r"^[A-Z]{1,4}", text)
    return match.group(0) if match else ""


def _merge_tushare_meta(meta: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(meta or {})
    for key in (
        "tushare_ts_code",
        "tushare_exchange",
        "tushare_fut_code",
        "tushare_last_sync_at",
        "tushare_continuous_ts_code",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            merged[key] = value
    return merged


def _merge_quote_payload(meta: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    merged = dict(meta or {})
    for key, value in (quote or {}).items():
        if value not in (None, "", []):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    return merged


def _normalize_future_quote_payload(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload or {})
    contract_symbol = _normalize_symbol(symbol, FUTURES_MARKET)
    merged["symbol"] = contract_symbol
    if not merged.get("display_symbol"):
        merged["display_symbol"] = contract_symbol
    merged.setdefault("instrument_type", "future")
    merged.setdefault("market", FUTURES_MARKET)
    if not merged.get("name"):
        merged["name"] = contract_symbol
    if not merged.get("currency"):
        merged["currency"] = "CNY"
    if not merged.get("contract_multiplier"):
        merged["contract_multiplier"] = 1.0
    if not merged.get("expiry_date"):
        merged["expiry_date"] = _parse_expiry_from_contract(contract_symbol)
    if "is_main_contract" not in merged or merged.get("is_main_contract") is None:
        merged["is_main_contract"] = contract_symbol.endswith("0")
    if not merged.get("underlying_symbol"):
        merged["underlying_symbol"] = _extract_contract_prefix(contract_symbol)
    if not merged.get("product_name"):
        merged["product_name"] = (
            str(merged.get("underlying_name") or "")
            or str(merged.get("name") or "")
            or contract_symbol
        )
    if not merged.get("underlying_name"):
        merged["underlying_name"] = (
            str(merged.get("product_name") or "")
            or str(merged.get("name") or "")
            or contract_symbol
        )
    return merged


def _future_contract_to_payload(row: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    symbol = _normalize_symbol(row.get("symbol") or "", FUTURES_MARKET)
    raw_change_pct = row.get("changepercent")
    change_pct = None
    if raw_change_pct is not None:
        try:
            change_pct = float(raw_change_pct)
            if abs(change_pct) <= 1.5:
                change_pct *= 100
        except Exception:
            change_pct = None
    trade = row.get("trade")
    prev_close = row.get("preclose")
    if prev_close in (None, "", 0):
        prev_close = row.get("presettlement") or row.get("prevsettlement")
    change_amount = None
    try:
        if trade not in (None, "") and prev_close not in (None, ""):
            change_amount = float(trade) - float(prev_close)
    except Exception:
        change_amount = None
    return {
        "instrument_type": "future",
        "market": FUTURES_MARKET,
        "symbol": symbol,
        "display_symbol": symbol,
        "name": str(row.get("name") or symbol),
        "exchange": _normalize_exchange(row.get("exchange") or product.get("exchange_code")),
        "exchange_name": str(product.get("exchange_name") or ""),
        "currency": "CNY",
        "underlying_symbol": str(product.get("product_code") or ""),
        "underlying_name": str(product.get("product_name") or ""),
        "product_name": str(product.get("product_name") or ""),
        "product_key": str(product.get("product_key") or ""),
        "contract_multiplier": 1.0,
        "tick_size": None,
        "expiry_date": _parse_expiry_from_contract(symbol),
        "is_main_contract": symbol.endswith("0"),
        "current_price": float(trade) if trade not in (None, "") else None,
        "change_pct": change_pct,
        "change_amount": change_amount,
        "prev_close": float(prev_close) if prev_close not in (None, "") else None,
        "open_price": float(row.get("open")) if row.get("open") not in (None, "") else None,
        "high_price": float(row.get("high")) if row.get("high") not in (None, "") else None,
        "low_price": float(row.get("low")) if row.get("low") not in (None, "") else None,
        "volume": float(row.get("volume")) if row.get("volume") not in (None, "") else None,
        "turnover": None,
        "position": float(row.get("position")) if row.get("position") not in (None, "") else None,
        "bid_price": float(row.get("bidprice1")) if row.get("bidprice1") not in (None, "") else None,
        "ask_price": float(row.get("askprice1")) if row.get("askprice1") not in (None, "") else None,
        "trade_date": str(row.get("tradedate") or ""),
        "tick_time": str(row.get("ticktime") or ""),
    }


def get_futures_products(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _PRODUCT_LOCK:
        if (
            not force_refresh
            and _PRODUCT_CACHE["items"]
            and now - float(_PRODUCT_CACHE["ts"] or 0) < _PRODUCT_CACHE_TTL_SECONDS
        ):
            return list(_PRODUCT_CACHE["items"])

    df = ak.futures_symbol_mark()
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        product_name = str(row.get("symbol") or "").strip()
        exchange_name = str(row.get("exchange") or "").strip()
        mark = str(row.get("mark") or "").strip()
        if not product_name:
            continue
        product_code = ""
        if product_name.isascii():
            product_code = product_name.upper()
        items.append(
            {
                "product_name": product_name,
                "product_code": product_code,
                "exchange_name": exchange_name,
                "exchange_code": _normalize_exchange(exchange_name),
                "mark": mark,
                "product_key": f"{_normalize_exchange(exchange_name)}:{product_name}",
            }
        )

    with _PRODUCT_LOCK:
        _PRODUCT_CACHE["ts"] = now
        _PRODUCT_CACHE["items"] = list(items)
    return items


def get_futures_contracts_for_product(product: dict[str, Any], *, force_refresh: bool = False) -> list[dict[str, Any]]:
    product_key = str(product.get("product_key") or "")
    now = time.time()
    with _CONTRACT_LOCK:
        cached = _CONTRACT_CACHE.get(product_key)
        if cached and not force_refresh and (now - cached[0]) < _CONTRACT_CACHE_TTL_SECONDS:
            return list(cached[1])

    df = ak.futures_zh_realtime(symbol=str(product.get("product_name") or ""))
    rows = [
        _future_contract_to_payload(raw, product)
        for raw in df.to_dict(orient="records")
        if str(raw.get("symbol") or "").strip()
    ]

    with _CONTRACT_LOCK:
        _CONTRACT_CACHE[product_key] = (now, list(rows))
    return rows


def _build_contract_index(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    now = time.time()
    with _CONTRACT_INDEX_LOCK:
        if (
            not force_refresh
            and _CONTRACT_INDEX_CACHE["items"]
            and now - float(_CONTRACT_INDEX_CACHE["ts"] or 0) < _CONTRACT_INDEX_TTL_SECONDS
        ):
            return dict(_CONTRACT_INDEX_CACHE["items"])

    products = get_futures_products(force_refresh=force_refresh)
    index: dict[str, dict[str, Any]] = {}
    max_workers = min(8, max(1, len(products)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(get_futures_contracts_for_product, product, force_refresh=force_refresh): product
            for product in products
        }
        for future in as_completed(future_map):
            try:
                for item in future.result():
                    index[str(item.get("symbol") or "").upper()] = item
            except Exception:
                logger.warning("failed to load futures contracts", exc_info=True)

    with _CONTRACT_INDEX_LOCK:
        _CONTRACT_INDEX_CACHE["ts"] = now
        _CONTRACT_INDEX_CACHE["items"] = dict(index)
    return index


def resolve_future_contract(symbol: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    contract_symbol = _normalize_symbol(symbol, FUTURES_MARKET)
    if not contract_symbol:
        return None
    try:
        resolved = resolve_tushare_future_contract(contract_symbol, force_refresh=force_refresh)
        if resolved:
            logger.info(
                "cn_fut resolve provider=tushare fallback=false symbol=%s ts_code=%s",
                contract_symbol,
                resolved.get("tushare_ts_code"),
            )
            return resolved
    except TushareUnavailable as exc:
        logger.info(
            "cn_fut resolve provider=tushare fallback=true symbol=%s reason=%s",
            contract_symbol,
            exc,
        )
    except Exception as exc:
        logger.warning(
            "cn_fut resolve provider=tushare fallback=true symbol=%s reason=%s",
            contract_symbol,
            exc,
        )
    try:
        index = _build_contract_index(force_refresh=force_refresh)
        item = index.get(contract_symbol)
        if item:
            logger.info(
                "cn_fut resolve provider=akshare fallback=true symbol=%s",
                contract_symbol,
            )
            return dict(item)
    except Exception:
        logger.warning("failed to resolve futures contract index for %s", contract_symbol, exc_info=True)
    try:
        df = ak.futures_zh_daily_sina(symbol=contract_symbol)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    logger.info(
        "cn_fut resolve provider=akshare fallback=true symbol=%s source=daily_sina",
        contract_symbol,
    )
    return {
        "instrument_type": "future",
        "market": FUTURES_MARKET,
        "symbol": contract_symbol,
        "display_symbol": contract_symbol,
        "name": contract_symbol,
        "exchange": "",
        "exchange_name": "",
        "currency": "CNY",
        "underlying_symbol": "",
        "underlying_name": "",
        "product_name": "",
        "product_key": "",
        "contract_multiplier": 1.0,
        "tick_size": None,
        "expiry_date": _parse_expiry_from_contract(contract_symbol),
        "is_main_contract": contract_symbol.endswith("0"),
    }


def search_future_instruments(
    query: str,
    *,
    exchange: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return []
    try:
        results = search_tushare_future_instruments(q, exchange=exchange, limit=limit)
        if results:
            logger.info(
                "cn_fut search provider=tushare fallback=false query=%s count=%s",
                q,
                len(results),
            )
            return results[: max(1, limit)]
    except TushareUnavailable as exc:
        logger.info(
            "cn_fut search provider=tushare fallback=true query=%s reason=%s",
            q,
            exc,
        )
    except Exception as exc:
        logger.warning(
            "cn_fut search provider=tushare fallback=true query=%s reason=%s",
            q,
            exc,
        )
    normalized_q = q.upper()
    lower_q = q.lower()
    exchange_filter = _normalize_exchange(exchange)

    results: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    contract_match = _CONTRACT_SYMBOL_RE.match(normalized_q)
    prefix_match = re.match(r"^[A-Z]{1,4}$", normalized_q)
    if contract_match:
        resolved = resolve_future_contract(normalized_q)
        if resolved:
            results[resolved["symbol"]] = resolved
    elif prefix_match:
        try:
            index = _build_contract_index()
            for symbol, item in index.items():
                if not symbol.startswith(normalized_q):
                    continue
                if exchange_filter and str(item.get("exchange") or "") != exchange_filter:
                    continue
                results.setdefault(symbol, item)
                if len(results) >= limit:
                    return list(results.values())[: max(1, limit)]
        except Exception:
            logger.warning("failed to build futures prefix index for %s", normalized_q, exc_info=True)

    try:
        products = get_futures_products()
    except Exception:
        logger.warning("failed to load futures product catalog", exc_info=True)
        return list(results.values())[: max(1, limit)]
    matched_products: list[dict[str, Any]] = []
    for product in products:
        product_exchange = str(product.get("exchange_code") or "")
        if exchange_filter and product_exchange != exchange_filter:
            continue
        product_name = str(product.get("product_name") or "")
        product_code = str(product.get("product_code") or "")
        mark = str(product.get("mark") or "")
        haystack = [product_name.lower(), product_code.lower(), mark.lower(), product_exchange.lower()]
        aliases = _EXCHANGE_ALIASES.get(product_exchange, set())
        if any(lower_q in text for text in haystack if text) or lower_q in aliases:
            matched_products.append(product)

    if not matched_products and contract_match:
        return list(results.values())[: max(1, limit)]

    for product in matched_products[: max(1, min(len(matched_products), 8))]:
        try:
            for item in get_futures_contracts_for_product(product):
                symbol = str(item.get("symbol") or "")
                name = str(item.get("name") or "")
                if exchange_filter and str(item.get("exchange") or "") != exchange_filter:
                    continue
                if (
                    lower_q in symbol.lower()
                    or lower_q in name.lower()
                    or lower_q in str(item.get("underlying_name") or "").lower()
                ):
                    results.setdefault(symbol, item)
                if len(results) >= limit:
                    return list(results.values())[: max(1, limit)]
        except Exception:
            logger.warning("future search failed for %s", product.get("product_name"), exc_info=True)

    final = list(results.values())[: max(1, limit)]
    if final:
        logger.info(
            "cn_fut search provider=akshare fallback=true query=%s count=%s",
            q,
            len(final),
        )
    return final


def get_futures_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized = []
    for symbol in symbols or []:
        value = _normalize_symbol(symbol, FUTURES_MARKET)
        if value and value not in normalized:
            normalized.append(value)
    if not normalized:
        return {}

    results: dict[str, dict[str, Any]] = {}
    try:
        contract_index = _build_contract_index()
    except Exception:
        contract_index = {}
        logger.warning("future quote contract index fetch failed", exc_info=True)

    for symbol in normalized:
        row = contract_index.get(symbol)
        if not row:
            continue
        results[symbol] = _normalize_future_quote_payload(symbol, row)

    unresolved_symbols = [symbol for symbol in normalized if symbol not in results]

    resolved: dict[str, dict[str, Any]] = {}
    for symbol in unresolved_symbols:
        item = resolve_future_contract(symbol)
        if item:
            resolved[symbol] = item

    grouped: dict[str, list[str]] = {}
    for symbol in unresolved_symbols:
        item = resolved.get(symbol) or {}
        product_name = str(item.get("product_name") or "")
        if product_name:
            grouped.setdefault(product_name, []).append(symbol)

    for product_name, contract_symbols in grouped.items():
        try:
            df = ak.futures_zh_realtime(symbol=product_name)
            rows = {
                _normalize_symbol(str(row.get("symbol") or ""), FUTURES_MARKET): row
                for row in df.to_dict(orient="records")
            }
            for symbol in contract_symbols:
                meta = resolved.get(symbol) or {}
                row = rows.get(symbol)
                if not row:
                    continue
                results[symbol] = _normalize_future_quote_payload(
                    symbol,
                    _merge_quote_payload(
                        meta,
                        _future_contract_to_payload(row, meta),
                    ),
                )
        except Exception:
            logger.warning("future quote fetch failed for %s", product_name, exc_info=True)

    logger.info(
        "cn_fut quotes provider=akshare requested=%s resolved=%s",
        len(normalized),
        len([symbol for symbol in normalized if results.get(symbol, {}).get("current_price") is not None]),
    )

    for symbol in normalized:
        results.setdefault(
            symbol,
            _normalize_future_quote_payload(
                symbol,
                resolved.get(symbol) or {
                    "current_price": None,
                    "change_pct": None,
                    "change_amount": None,
                    "prev_close": None,
                    "open_price": None,
                    "high_price": None,
                    "low_price": None,
                    "volume": None,
                    "turnover": None,
                },
            ),
        )
    return results


def get_instrument_by_key(
    db: Session,
    *,
    instrument_type: str,
    market: str,
    symbol: str,
) -> Instrument | None:
    return (
        db.query(Instrument)
        .filter(
            Instrument.instrument_type == str(instrument_type or "equity").strip().lower(),
            Instrument.market == str(market or MarketCode.CN.value).upper(),
            Instrument.symbol == _normalize_symbol(symbol, market),
        )
        .first()
    )


def ensure_equity_instrument(
    db: Session,
    *,
    symbol: str,
    name: str,
    market: str,
    stock: Stock | None = None,
) -> Instrument:
    market_value = str(market or MarketCode.CN.value).upper()
    if market_value != MarketCode.CN.value:
        raise ValueError(f"equity supports CN only in current mode: {market_value}")
    normalized_symbol = _normalize_symbol(symbol, market_value)
    instrument = get_instrument_by_key(
        db,
        instrument_type="equity",
        market=market_value,
        symbol=normalized_symbol,
    )
    if instrument is None:
        instrument = Instrument(
            instrument_type="equity",
            market=market_value,
            symbol=normalized_symbol,
            display_symbol=normalized_symbol,
            name=str(name or normalized_symbol),
            currency=_default_currency_for_market(market_value),
            contract_multiplier=1.0,
        )
        db.add(instrument)
        db.flush()
    else:
        instrument.name = str(name or instrument.name or normalized_symbol)
        instrument.display_symbol = instrument.display_symbol or normalized_symbol
        instrument.currency = instrument.currency or _default_currency_for_market(market_value)
        if not instrument.contract_multiplier:
            instrument.contract_multiplier = 1.0
    if stock is not None:
        stock.instrument_id = instrument.id
    return instrument


def ensure_future_instrument(
    db: Session,
    *,
    symbol: str,
    name: str = "",
    exchange: str = "",
    underlying_symbol: str = "",
    underlying_name: str = "",
    contract_multiplier: float | None = None,
    tick_size: float | None = None,
    expiry_date: str = "",
    is_main_contract: bool | None = None,
) -> Instrument:
    normalized_symbol = _normalize_symbol(symbol, FUTURES_MARKET)
    resolved = resolve_future_contract(normalized_symbol) or {}
    instrument = get_instrument_by_key(
        db,
        instrument_type="future",
        market=FUTURES_MARKET,
        symbol=normalized_symbol,
    )
    if instrument is None:
        instrument = Instrument(
            instrument_type="future",
            market=FUTURES_MARKET,
            symbol=normalized_symbol,
            display_symbol=str(resolved.get("display_symbol") or normalized_symbol),
            name=str(name or resolved.get("name") or normalized_symbol),
            exchange=_normalize_exchange(exchange or resolved.get("exchange")),
            currency="CNY",
            underlying_symbol=str(underlying_symbol or resolved.get("underlying_symbol") or ""),
            underlying_name=str(underlying_name or resolved.get("underlying_name") or ""),
            contract_multiplier=float(contract_multiplier or resolved.get("contract_multiplier") or 1.0),
            tick_size=tick_size if tick_size is not None else resolved.get("tick_size"),
            expiry_date=str(expiry_date or resolved.get("expiry_date") or ""),
            is_main_contract=bool(
                resolved.get("is_main_contract") if is_main_contract is None else is_main_contract
            ),
            meta={
                "exchange_name": resolved.get("exchange_name") or "",
                "product_name": resolved.get("product_name") or "",
                "product_key": resolved.get("product_key") or "",
                **_merge_tushare_meta({}, resolved),
            },
        )
        db.add(instrument)
        db.flush()
        return instrument

    instrument.name = str(name or resolved.get("name") or instrument.name or normalized_symbol)
    instrument.display_symbol = str(
        resolved.get("display_symbol") or instrument.display_symbol or normalized_symbol
    )
    instrument.exchange = _normalize_exchange(exchange or instrument.exchange or resolved.get("exchange"))
    instrument.currency = instrument.currency or "CNY"
    instrument.underlying_symbol = str(underlying_symbol or instrument.underlying_symbol or resolved.get("underlying_symbol") or "")
    instrument.underlying_name = str(underlying_name or instrument.underlying_name or resolved.get("underlying_name") or "")
    instrument.contract_multiplier = float(contract_multiplier or instrument.contract_multiplier or resolved.get("contract_multiplier") or 1.0)
    if tick_size is not None:
        instrument.tick_size = tick_size
    elif instrument.tick_size is None and resolved.get("tick_size") is not None:
        instrument.tick_size = resolved.get("tick_size")
    instrument.expiry_date = str(expiry_date or instrument.expiry_date or resolved.get("expiry_date") or "")
    if is_main_contract is not None:
        instrument.is_main_contract = bool(is_main_contract)
    elif instrument.is_main_contract is None:
        instrument.is_main_contract = bool(resolved.get("is_main_contract"))
    meta = dict(instrument.meta or {})
    for key in ("exchange_name", "product_name", "product_key"):
        if resolved.get(key):
            meta[key] = resolved[key]
    instrument.meta = _merge_tushare_meta(meta, resolved)
    return instrument


def ensure_option_instrument(
    db: Session,
    *,
    symbol: str,
    name: str = "",
    exchange: str = "",
    underlying_symbol: str = "",
    underlying_name: str = "",
    contract_multiplier: float | None = None,
    tick_size: float | None = None,
    expiry_date: str = "",
    option_type: str = "",
    strike_price: float | None = None,
    exercise_style: str = "",
) -> Instrument:
    normalized_symbol = _normalize_symbol(symbol, OPTIONS_MARKET)
    resolved = resolve_option_contract(normalized_symbol) or {}
    if not resolved and normalized_symbol:
        raise ValueError(f"unsupported option symbol: {normalized_symbol}")
    normalized_symbol = str(resolved.get("symbol") or normalized_symbol)
    instrument = get_instrument_by_key(
        db,
        instrument_type="option",
        market=OPTIONS_MARKET,
        symbol=normalized_symbol,
    )
    if instrument is None:
        instrument = Instrument(
            instrument_type="option",
            market=OPTIONS_MARKET,
            symbol=normalized_symbol,
            display_symbol=str(resolved.get("display_symbol") or normalized_symbol),
            name=str(name or resolved.get("name") or normalized_symbol),
            exchange=_normalize_exchange(exchange or resolved.get("exchange")),
            currency="CNY",
            underlying_symbol=str(
                underlying_symbol or resolved.get("underlying_symbol") or ""
            ),
            underlying_name=str(
                underlying_name or resolved.get("underlying_name") or ""
            ),
            contract_multiplier=float(
                contract_multiplier
                or resolved.get("contract_multiplier")
                or 1.0
            ),
            tick_size=tick_size if tick_size is not None else resolved.get("tick_size"),
            expiry_date=str(expiry_date or resolved.get("expiry_date") or ""),
            option_type=str(option_type or resolved.get("option_type") or "").lower(),
            strike_price=(
                strike_price
                if strike_price is not None
                else resolved.get("strike_price")
            ),
            exercise_style=str(exercise_style or resolved.get("exercise_style") or ""),
        )
        db.add(instrument)
        db.flush()
        return instrument

    instrument.name = str(name or resolved.get("name") or instrument.name or normalized_symbol)
    instrument.display_symbol = str(
        resolved.get("display_symbol") or instrument.display_symbol or normalized_symbol
    )
    instrument.exchange = _normalize_exchange(exchange or instrument.exchange or resolved.get("exchange"))
    instrument.currency = instrument.currency or "CNY"
    instrument.underlying_symbol = str(
        underlying_symbol
        or instrument.underlying_symbol
        or resolved.get("underlying_symbol")
        or ""
    )
    instrument.underlying_name = str(
        underlying_name
        or instrument.underlying_name
        or resolved.get("underlying_name")
        or ""
    )
    instrument.contract_multiplier = float(
        contract_multiplier
        or instrument.contract_multiplier
        or resolved.get("contract_multiplier")
        or 1.0
    )
    if tick_size is not None:
        instrument.tick_size = tick_size
    elif instrument.tick_size is None and resolved.get("tick_size") is not None:
        instrument.tick_size = resolved.get("tick_size")
    instrument.expiry_date = str(expiry_date or instrument.expiry_date or "")
    if not instrument.expiry_date and resolved.get("expiry_date"):
        instrument.expiry_date = str(resolved.get("expiry_date") or "")
    instrument.option_type = str(
        option_type or resolved.get("option_type") or instrument.option_type or ""
    ).lower()
    if strike_price is not None:
        instrument.strike_price = strike_price
    elif instrument.strike_price is None and resolved.get("strike_price") is not None:
        instrument.strike_price = resolved.get("strike_price")
    instrument.exercise_style = str(exercise_style or instrument.exercise_style or "")
    if not instrument.exercise_style and resolved.get("exercise_style"):
        instrument.exercise_style = str(resolved.get("exercise_style") or "")
    return instrument


def get_or_create_compat_stock_for_instrument(
    db: Session,
    instrument: Instrument,
    *,
    sort_order: int | None = None,
) -> Stock:
    existing = (
        db.query(Stock)
        .filter(Stock.instrument_id == instrument.id)
        .first()
    )
    if existing:
        existing.symbol = instrument.symbol
        existing.name = instrument.name
        existing.market = instrument.market
        return existing

    next_order = sort_order
    if next_order is None:
        next_order = int(db.query(func.max(Stock.sort_order)).scalar() or 0) + 1

    stock = Stock(
        symbol=instrument.symbol,
        name=instrument.name,
        market=instrument.market,
        instrument_id=instrument.id,
        sort_order=next_order,
    )
    db.add(stock)
    db.flush()
    return stock


def ensure_stock_compatibility(
    db: Session,
    *,
    symbol: str,
    name: str,
    market: str,
) -> EnsureInstrumentResult:
    market_value = str(market or MarketCode.CN.value).upper()
    if market_value == FUTURES_MARKET:
        instrument = ensure_future_instrument(
            db,
            symbol=symbol,
            name=name,
        )
        stock = get_or_create_compat_stock_for_instrument(db, instrument)
        return EnsureInstrumentResult(instrument=instrument, stock=stock)
    if market_value == OPTIONS_MARKET:
        instrument = ensure_option_instrument(
            db,
            symbol=symbol,
            name=name,
        )
        stock = get_or_create_compat_stock_for_instrument(db, instrument)
        return EnsureInstrumentResult(instrument=instrument, stock=stock)

    instrument = ensure_equity_instrument(
        db,
        symbol=symbol,
        name=name,
        market=market_value,
    )
    stock = get_or_create_compat_stock_for_instrument(db, instrument)
    return EnsureInstrumentResult(instrument=instrument, stock=stock)
