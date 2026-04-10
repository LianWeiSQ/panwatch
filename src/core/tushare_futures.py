from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from src.config import Settings

logger = logging.getLogger(__name__)

_CONTRACT_SYMBOL_RE = re.compile(r"^(?P<prefix>[A-Z]{1,4})(?P<year>\d{2})(?P<month>\d{2})$")
_MAIN_SYMBOL_RE = re.compile(r"^(?P<prefix>[A-Z]{1,4})0$")

_TS_TO_APP_EXCHANGE = {
    "CFX": "CFFEX",
    "CFFEX": "CFFEX",
    "SHF": "SHFE",
    "SHFE": "SHFE",
    "DCE": "DCE",
    "ZCE": "CZCE",
    "CZCE": "CZCE",
    "INE": "INE",
    "GFE": "GFEX",
    "GFEX": "GFEX",
}
_APP_TO_TS_EXCHANGE = {
    "CFFEX": "CFX",
    "SHFE": "SHF",
    "DCE": "DCE",
    "CZCE": "ZCE",
    "INE": "INE",
    "GFEX": "GFE",
}

_CATALOG_CACHE_TTL_SECONDS = 60 * 60 * 6
_MAPPING_CACHE_TTL_SECONDS = 60 * 5
_DAILY_CACHE_TTL_SECONDS = 60 * 5

_CLIENT_CACHE: dict[str, Any] = {"token": "", "base_url": "", "client": None}
_CLIENT_LOCK = threading.Lock()
_CATALOG_CACHE: dict[str, Any] = {"ts": 0.0, "items": []}
_CATALOG_LOCK = threading.Lock()
_MAPPING_CACHE: dict[str, tuple[float, str]] = {}
_MAPPING_LOCK = threading.Lock()
_DAILY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_DAILY_LOCK = threading.Lock()


class TushareUnavailable(RuntimeError):
    pass


def has_tushare_token() -> bool:
    token, _ = _get_tushare_env()
    return bool(token)


def _normalize_base_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def _get_tushare_env() -> tuple[str, str]:
    settings = Settings()
    token = str(settings.tushare_token or "").strip()
    base_url = _normalize_base_url(settings.tushare_base_url)
    return token, base_url


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _format_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10:
        return raw[:10]
    return raw


def _extract_product_code(symbol: str) -> str:
    match = re.match(r"^[A-Z]{1,4}", _normalize_symbol(symbol))
    return match.group(0) if match else ""


def _ts_code_to_symbol(ts_code: str) -> str:
    return _normalize_symbol(str(ts_code or "").split(".", 1)[0])


def _normalize_exchange(exchange: str | None) -> str:
    value = str(exchange or "").strip().upper()
    if not value:
        return ""
    return _TS_TO_APP_EXCHANGE.get(value, value)


def _to_ts_exchange(exchange: str | None) -> str:
    value = _normalize_exchange(exchange)
    if not value:
        return ""
    return _APP_TO_TS_EXCHANGE.get(value, value)


def _parse_expiry_from_contract(symbol: str) -> str:
    match = _CONTRACT_SYMBOL_RE.match(_normalize_symbol(symbol))
    if not match:
        return ""
    year = 2000 + int(match.group("year"))
    month = int(match.group("month"))
    if month < 1 or month > 12:
        return ""
    return f"{year:04d}-{month:02d}"


def _is_active_payload(item: dict[str, Any]) -> bool:
    expiry = str(item.get("expiry_date") or "").strip()
    if not expiry:
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    return expiry >= today


def _catalog_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    return (
        0 if _is_active_payload(item) else 1,
        str(item.get("expiry_date") or "9999-12"),
        str(item.get("symbol") or ""),
    )


def _get_tushare_client():
    token, base_url = _get_tushare_env()
    if not token:
        raise TushareUnavailable("tushare token not configured")

    with _CLIENT_LOCK:
        if (
            _CLIENT_CACHE["client"] is not None
            and _CLIENT_CACHE["token"] == token
            and _CLIENT_CACHE["base_url"] == base_url
        ):
            return _CLIENT_CACHE["client"]

        try:
            import tushare as ts
        except Exception as exc:
            raise TushareUnavailable(f"tushare package unavailable: {exc}") from exc

        ts.set_token(token)
        client = ts.pro_api(token)
        if base_url:
            client._DataApi__http_url = base_url
        _CLIENT_CACHE["token"] = token
        _CLIENT_CACHE["base_url"] = base_url
        _CLIENT_CACHE["client"] = client
        return client


def _call_tushare(method_name: str, call_variants: list[dict[str, Any]]):
    client = _get_tushare_client()
    method = getattr(client, method_name, None)
    if method is None:
        raise TushareUnavailable(f"tushare client missing method: {method_name}")

    last_error: Exception | None = None
    for kwargs in call_variants:
        try:
            return method(**kwargs)
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            break
    if last_error is not None:
        raise last_error
    return None


def _payload_from_catalog_row(
    row: dict[str, Any],
    *,
    requested_symbol: str | None = None,
    main_display_symbol: str | None = None,
    continuous_ts_code: str = "",
) -> dict[str, Any]:
    ts_code = str(row.get("ts_code") or "").strip().upper()
    actual_symbol = _ts_code_to_symbol(ts_code) or _normalize_symbol(row.get("symbol") or "")
    symbol = _normalize_symbol(requested_symbol or actual_symbol)
    exchange = _normalize_exchange(row.get("exchange") or ts_code.split(".")[-1])
    product_code = _normalize_symbol(row.get("fut_code") or _extract_product_code(actual_symbol))
    multiplier = (
        _safe_float(row.get("multiplier"))
        or _safe_float(row.get("per_unit"))
        or 1.0
    )
    tick_size = (
        _safe_float(row.get("quote_unit"))
        or _safe_float(row.get("price_tick"))
        or None
    )
    expiry_date = (
        _format_trade_date(row.get("delist_date"))
        or _parse_expiry_from_contract(actual_symbol)
    )
    last_sync_at = datetime.now().isoformat(timespec="seconds")
    return {
        "instrument_type": "future",
        "market": "CN_FUT",
        "symbol": symbol,
        "display_symbol": main_display_symbol or actual_symbol,
        "name": str(row.get("name") or actual_symbol or symbol),
        "exchange": exchange,
        "exchange_name": exchange,
        "currency": "CNY",
        "underlying_symbol": product_code,
        "underlying_name": str(row.get("name") or product_code or symbol),
        "product_name": str(row.get("name") or product_code or symbol),
        "product_key": f"{exchange}:{product_code}" if exchange and product_code else product_code,
        "contract_multiplier": float(multiplier),
        "tick_size": tick_size,
        "expiry_date": expiry_date,
        "is_main_contract": bool(requested_symbol and requested_symbol.endswith("0")),
        "tushare_ts_code": ts_code,
        "tushare_exchange": _to_ts_exchange(exchange),
        "tushare_fut_code": product_code,
        "tushare_last_sync_at": last_sync_at,
        "tushare_continuous_ts_code": continuous_ts_code,
    }


def get_tushare_futures_catalog(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _CATALOG_LOCK:
        cached_items = _CATALOG_CACHE["items"]
        if (
            not force_refresh
            and cached_items
            and now - float(_CATALOG_CACHE["ts"] or 0) < _CATALOG_CACHE_TTL_SECONDS
        ):
            return list(cached_items)

    df = _call_tushare(
        "fut_basic",
        [
            {
                "fields": "ts_code,symbol,exchange,name,fut_code,multiplier,quote_unit,list_date,delist_date",
            },
            {},
        ],
    )
    rows = df.to_dict(orient="records") if df is not None else []
    items = [_payload_from_catalog_row(row) for row in rows if str(row.get("ts_code") or "").strip()]
    items.sort(key=_catalog_sort_key)

    with _CATALOG_LOCK:
        _CATALOG_CACHE["ts"] = now
        _CATALOG_CACHE["items"] = list(items)
    return items


def _find_catalog_row_by_ts_code(ts_code: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    normalized_ts_code = str(ts_code or "").strip().upper()
    if not normalized_ts_code:
        return None
    for item in get_tushare_futures_catalog(force_refresh=force_refresh):
        if str(item.get("tushare_ts_code") or "").upper() == normalized_ts_code:
            return dict(item)
    return None


def _find_catalog_row_by_symbol(symbol: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        return None
    for item in get_tushare_futures_catalog(force_refresh=force_refresh):
        if str(item.get("symbol") or "").upper() == normalized_symbol:
            return dict(item)
    return None


def _resolve_main_mapping_ts_code(continuous_ts_code: str, *, force_refresh: bool = False) -> str:
    normalized_ts_code = str(continuous_ts_code or "").strip().upper()
    if not normalized_ts_code:
        return ""
    now = time.time()
    with _MAPPING_LOCK:
        cached = _MAPPING_CACHE.get(normalized_ts_code)
        if cached and not force_refresh and (now - cached[0]) < _MAPPING_CACHE_TTL_SECONDS:
            return cached[1]

    df = _call_tushare("fut_mapping", [{"ts_code": normalized_ts_code}])
    rows = df.to_dict(orient="records") if df is not None else []
    if not rows:
        return ""
    rows.sort(key=lambda row: str(row.get("trade_date") or ""))
    mapped_ts_code = str(rows[-1].get("mapping_ts_code") or "").strip().upper()

    with _MAPPING_LOCK:
        _MAPPING_CACHE[normalized_ts_code] = (now, mapped_ts_code)
    return mapped_ts_code


def resolve_tushare_future_contract(symbol: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        return None

    direct = _find_catalog_row_by_symbol(normalized_symbol, force_refresh=force_refresh)
    if direct:
        return direct

    main_match = _MAIN_SYMBOL_RE.match(normalized_symbol)
    if not main_match:
        return None

    prefix = main_match.group("prefix")
    catalog = get_tushare_futures_catalog(force_refresh=force_refresh)
    candidates = [item for item in catalog if str(item.get("underlying_symbol") or "") == prefix]
    if not candidates:
        return None

    seen_suffixes: set[str] = set()
    for item in candidates:
        suffix = str(item.get("tushare_exchange") or "").strip().upper()
        if not suffix or suffix in seen_suffixes:
            continue
        seen_suffixes.add(suffix)
        continuous_ts_code = f"{prefix}00.{suffix}"
        mapped_ts_code = _resolve_main_mapping_ts_code(
            continuous_ts_code,
            force_refresh=force_refresh,
        )
        if not mapped_ts_code:
            continue
        mapped = _find_catalog_row_by_ts_code(mapped_ts_code, force_refresh=force_refresh)
        if not mapped:
            continue
        payload = dict(mapped)
        payload["symbol"] = normalized_symbol
        payload["display_symbol"] = mapped.get("symbol") or normalized_symbol
        payload["is_main_contract"] = True
        payload["tushare_continuous_ts_code"] = continuous_ts_code
        return payload

    return None


def search_tushare_future_instruments(
    query: str,
    *,
    exchange: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return []

    normalized_q = _normalize_symbol(q)
    lower_q = q.lower()
    exchange_filter = _normalize_exchange(exchange)
    results: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    if _MAIN_SYMBOL_RE.match(normalized_q):
        resolved = resolve_tushare_future_contract(normalized_q)
        if resolved and (
            not exchange_filter or str(resolved.get("exchange") or "") == exchange_filter
        ):
            results[normalized_q] = resolved
            if len(results) >= limit:
                return list(results.values())[:limit]

    if _CONTRACT_SYMBOL_RE.match(normalized_q):
        resolved = resolve_tushare_future_contract(normalized_q)
        if resolved and (
            not exchange_filter or str(resolved.get("exchange") or "") == exchange_filter
        ):
            results[normalized_q] = resolved
            if len(results) >= limit:
                return list(results.values())[:limit]

    catalog = get_tushare_futures_catalog()
    if re.match(r"^[A-Z]{1,4}$", normalized_q):
        main_alias = resolve_tushare_future_contract(f"{normalized_q}0")
        if main_alias and (
            not exchange_filter or str(main_alias.get("exchange") or "") == exchange_filter
        ):
            results.setdefault(str(main_alias.get("symbol") or ""), main_alias)

    for item in catalog:
        if exchange_filter and str(item.get("exchange") or "") != exchange_filter:
            continue
        haystack = [
            str(item.get("symbol") or "").lower(),
            str(item.get("display_symbol") or "").lower(),
            str(item.get("name") or "").lower(),
            str(item.get("underlying_symbol") or "").lower(),
            str(item.get("exchange") or "").lower(),
            str(item.get("tushare_ts_code") or "").lower(),
        ]
        if any(lower_q in text for text in haystack if text):
            results.setdefault(str(item.get("symbol") or ""), dict(item))
            if len(results) >= limit:
                break

    return list(results.values())[:limit]


def _get_daily_rows_for_ts_code(
    ts_code: str,
    *,
    days: int = 10,
) -> list[dict[str, Any]]:
    normalized_ts_code = str(ts_code or "").strip().upper()
    if not normalized_ts_code:
        return []
    need_days = max(2, int(days or 2))
    cache_key = f"{normalized_ts_code}:{need_days}"
    now = time.time()
    with _DAILY_LOCK:
        cached = _DAILY_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _DAILY_CACHE_TTL_SECONDS:
            return list(cached[1])

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=max(need_days * 6, 14))).strftime("%Y%m%d")
    df = _call_tushare(
        "fut_daily",
        [
            {
                "ts_code": normalized_ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": "ts_code,trade_date,open,high,low,close,pre_close,settle,pre_settle,vol,amount,oi",
            },
            {
                "ts_code": normalized_ts_code,
                "start_date": start_date,
                "end_date": end_date,
            },
        ],
    )
    rows = df.to_dict(orient="records") if df is not None else []
    rows.sort(key=lambda row: str(row.get("trade_date") or ""))

    with _DAILY_LOCK:
        _DAILY_CACHE[cache_key] = (now, list(rows))
    return rows


def get_tushare_futures_daily_bars(symbol: str, *, days: int = 60) -> list[dict[str, Any]]:
    resolved = resolve_tushare_future_contract(symbol)
    if not resolved:
        return []
    ts_code = str(resolved.get("tushare_ts_code") or "").strip().upper()
    if not ts_code:
        return []

    rows = _get_daily_rows_for_ts_code(ts_code, days=max(2, days))
    if not rows:
        return []

    bars: list[dict[str, Any]] = []
    for row in rows[-max(1, int(days or 1)):]:
        open_price = _safe_float(row.get("open"))
        close_price = _safe_float(row.get("close"))
        high_price = _safe_float(row.get("high"))
        low_price = _safe_float(row.get("low"))
        if None in (open_price, close_price, high_price, low_price):
            continue
        bars.append(
            {
                "date": _format_trade_date(row.get("trade_date")),
                "open": float(open_price),
                "close": float(close_price),
                "high": float(high_price),
                "low": float(low_price),
                "volume": float(_safe_float(row.get("vol")) or 0.0),
            }
        )
    return bars


def _latest_minute_row_for_ts_code(ts_code: str) -> dict[str, Any] | None:
    normalized_ts_code = str(ts_code or "").strip().upper()
    if not normalized_ts_code:
        return None

    df = _call_tushare(
        "rt_fut_min",
        [
            {"ts_code": normalized_ts_code, "freq": "1MIN"},
            {"ts_code": normalized_ts_code, "freq": "1min"},
            {"ts_code": normalized_ts_code},
        ],
    )
    rows = df.to_dict(orient="records") if df is not None else []
    if not rows:
        return None

    def _sort_key(row: dict[str, Any]) -> str:
        return (
            str(row.get("trade_time") or "")
            or str(row.get("datetime") or "")
            or str(row.get("trade_date") or "")
        )

    rows.sort(key=_sort_key)
    return rows[-1]


def _prev_close_from_daily(ts_code: str) -> tuple[float | None, str]:
    rows = _get_daily_rows_for_ts_code(ts_code, days=3)
    if not rows:
        return None, ""

    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    prev_close = (
        _safe_float(latest.get("pre_close"))
        or _safe_float(latest.get("pre_settle"))
        or _safe_float(latest.get("pre_close_price"))
        or (_safe_float(prev.get("settle")) if prev else None)
        or (_safe_float(prev.get("close")) if prev else None)
    )
    return prev_close, _format_trade_date(latest.get("trade_date"))


def _minute_row_to_quote(
    requested_symbol: str,
    meta: dict[str, Any],
    minute_row: dict[str, Any],
    prev_close: float | None,
    trade_date: str,
) -> dict[str, Any]:
    current_price = (
        _safe_float(minute_row.get("close"))
        or _safe_float(minute_row.get("price"))
        or _safe_float(minute_row.get("last"))
    )
    open_price = _safe_float(minute_row.get("open"))
    high_price = _safe_float(minute_row.get("high"))
    low_price = _safe_float(minute_row.get("low"))
    volume = _safe_float(minute_row.get("vol")) or _safe_float(minute_row.get("volume"))
    turnover = _safe_float(minute_row.get("amount"))
    position = _safe_float(minute_row.get("oi")) or _safe_float(minute_row.get("position"))
    change_amount = None
    change_pct = None
    if current_price is not None and prev_close not in (None, 0):
        change_amount = float(current_price) - float(prev_close)
        change_pct = change_amount / float(prev_close) * 100

    tick_time = str(
        minute_row.get("trade_time")
        or minute_row.get("datetime")
        or minute_row.get("time")
        or ""
    )
    return {
        "instrument_type": "future",
        "market": "CN_FUT",
        "symbol": requested_symbol,
        "display_symbol": meta.get("display_symbol") or requested_symbol,
        "name": meta.get("name") or requested_symbol,
        "exchange": meta.get("exchange") or "",
        "exchange_name": meta.get("exchange_name") or meta.get("exchange") or "",
        "currency": "CNY",
        "underlying_symbol": meta.get("underlying_symbol") or _extract_product_code(requested_symbol),
        "underlying_name": meta.get("underlying_name") or meta.get("name") or requested_symbol,
        "product_name": meta.get("product_name") or meta.get("underlying_name") or meta.get("name") or requested_symbol,
        "product_key": meta.get("product_key") or "",
        "contract_multiplier": float(meta.get("contract_multiplier") or 1.0),
        "tick_size": meta.get("tick_size"),
        "expiry_date": meta.get("expiry_date") or _parse_expiry_from_contract(meta.get("display_symbol") or requested_symbol),
        "is_main_contract": bool(meta.get("is_main_contract")),
        "current_price": current_price,
        "change_pct": change_pct,
        "change_amount": change_amount,
        "prev_close": prev_close,
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "volume": volume,
        "turnover": turnover,
        "position": position,
        "bid_price": None,
        "ask_price": None,
        "trade_date": trade_date,
        "tick_time": tick_time,
        "tushare_ts_code": meta.get("tushare_ts_code") or "",
        "tushare_exchange": meta.get("tushare_exchange") or "",
        "tushare_fut_code": meta.get("tushare_fut_code") or "",
        "tushare_last_sync_at": datetime.now().isoformat(timespec="seconds"),
        "tushare_continuous_ts_code": meta.get("tushare_continuous_ts_code") or "",
    }


def get_tushare_futures_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized = []
    for symbol in symbols or []:
        value = _normalize_symbol(symbol)
        if value and value not in normalized:
            normalized.append(value)
    if not normalized:
        return {}

    resolved: dict[str, dict[str, Any]] = {}
    for symbol in normalized:
        item = resolve_tushare_future_contract(symbol)
        if item:
            resolved[symbol] = item

    actual_grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for requested_symbol, item in resolved.items():
        ts_code = str(item.get("tushare_ts_code") or "").strip().upper()
        if ts_code:
            actual_grouped.setdefault(ts_code, []).append((requested_symbol, item))

    results: dict[str, dict[str, Any]] = {}
    for ts_code, entries in actual_grouped.items():
        minute_row = _latest_minute_row_for_ts_code(ts_code)
        if not minute_row:
            continue
        prev_close, trade_date = _prev_close_from_daily(ts_code)
        for requested_symbol, meta in entries:
            results[requested_symbol] = _minute_row_to_quote(
                requested_symbol,
                meta,
                minute_row,
                prev_close,
                trade_date,
            )

    return results
