from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

import akshare as ak
import httpx

from src.models.market import MarketCode

logger = logging.getLogger(__name__)

OPTIONS_MARKET = MarketCode.CN_OPT.value

_OPTION_SYMBOL_RE = re.compile(
    r"^(?P<underlying>[A-Z]{2}\d{4})(?:-)?(?P<option_type>[CP])(?:-)?(?P<strike>\d+(?:\.\d+)?)$"
)

_CATALOG_CACHE_TTL_SECONDS = 60 * 60 * 6
_QUOTE_CACHE_TTL_SECONDS = 12

_CATALOG_CACHE: dict[str, Any] = {"ts": 0.0, "items": []}
_CATALOG_LOCK = threading.Lock()
_QUOTE_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_QUOTE_LOCK = threading.Lock()

_SUPPORTED_PRODUCTS: dict[str, dict[str, Any]] = {
    "AG": {
        "product_key": "ag_o",
        "quote_product": "ag_o",
        "exchange": "SHFE",
        "underlying_label": "沪银",
        "aliases": {"ag", "ag_o", "沪银", "白银", "白银期权"},
    },
    "FU": {
        "product_key": "fu_o",
        "quote_product": "fu_o",
        "exchange": "SHFE",
        "underlying_label": "燃料油",
        "aliases": {"fu", "fu_o", "燃料油", "燃油", "燃料油期权", "燃油期权"},
    },
    "HO": {
        "product_key": "HO",
        "quote_product": "ho",
        "exchange": "CFFEX",
        "underlying_label": "上证50",
        "aliases": {"ho", "上证50", "上证50指数", "50指数", "sz50"},
    },
}

_EXCHANGE_ALIAS = {
    "SHFE": "SHFE",
    "CFFEX": "CFFEX",
}


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "-", "--", "None"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalize_option_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        return ""
    value = value.replace("_", "").replace(" ", "")
    match = _OPTION_SYMBOL_RE.match(value)
    if not match:
        return value.replace("-", "")
    strike = match.group("strike")
    if "." in strike:
        strike = strike.rstrip("0").rstrip(".")
    return f"{match.group('underlying')}{match.group('option_type')}{strike}"


def _format_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10:
        return raw[:10]
    return raw


def _option_type_label(option_type: str) -> str:
    return "看涨" if option_type == "call" else "看跌"


def _option_type_from_payload(value: Any, symbol: str) -> str:
    raw = str(value or "").strip()
    if raw == "1":
        return "call"
    if raw == "2":
        return "put"
    match = _OPTION_SYMBOL_RE.match(_normalize_option_symbol(symbol))
    if match and match.group("option_type") == "C":
        return "call"
    if match and match.group("option_type") == "P":
        return "put"
    return ""


def _format_strike(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return str(value).rstrip("0").rstrip(".")


def _search_key(value: str) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def _option_query_aliases(query: str) -> list[str]:
    value = _search_key(query)
    if not value:
        return []
    aliases = {value}
    replacements = (
        ("认购", "看涨"),
        ("购", "看涨"),
        ("call", "看涨"),
        ("认沽", "看跌"),
        ("沽", "看跌"),
        ("put", "看跌"),
    )
    for src, dst in replacements:
        if src in value:
            aliases.add(value.replace(src, dst))
    return [item for item in aliases if item]


def _underlying_display_name(prefix: str, underlying_contract: str) -> str:
    config = _SUPPORTED_PRODUCTS.get(prefix) or {}
    base = str(config.get("underlying_label") or prefix)
    suffix = underlying_contract[-4:]
    if prefix == "HO":
        return f"{base} {suffix}"
    return f"{base}{suffix}"


def _extract_symbol_parts(symbol: str) -> dict[str, str] | None:
    match = _OPTION_SYMBOL_RE.match(_normalize_option_symbol(symbol))
    if not match:
        return None
    return {
        "underlying_contract": match.group("underlying"),
        "underlying_prefix": match.group("underlying")[:2],
        "option_code": match.group("option_type"),
        "strike": match.group("strike"),
    }


def _option_source_symbol(symbol: str) -> str:
    parts = _extract_symbol_parts(symbol)
    if not parts:
        return str(symbol or "")
    return f"{parts['underlying_prefix'].lower()}{parts['underlying_contract'][2:]}{parts['option_code']}{parts['strike']}"


def _is_supported_row(row: dict[str, Any]) -> bool:
    product_key = str(row.get("品种ID") or "").strip()
    exchange = str(row.get("交易所ID") or "").strip().upper()
    symbol = _normalize_option_symbol(row.get("合约ID") or "")
    parts = _extract_symbol_parts(symbol)
    if not parts:
        return False
    prefix = parts["underlying_prefix"]
    config = _SUPPORTED_PRODUCTS.get(prefix)
    if not config:
        return False
    return (
        product_key == str(config.get("product_key"))
        and exchange == str(config.get("exchange"))
    )


def _row_to_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_supported_row(row):
        return None

    symbol = _normalize_option_symbol(row.get("合约ID") or "")
    parts = _extract_symbol_parts(symbol)
    if not parts:
        return None
    prefix = parts["underlying_prefix"]
    config = _SUPPORTED_PRODUCTS[prefix]
    option_type = _option_type_from_payload(row.get("期权类型"), symbol)
    strike_price = _safe_float(row.get("行权价"))
    exchange = _EXCHANGE_ALIAS.get(str(row.get("交易所ID") or "").strip().upper(), "")
    expiry_date = _format_trade_date(row.get("最后交易日"))
    underlying_contract = str(row.get("标的合约ID") or parts["underlying_contract"]).strip().upper().replace("-", "")
    if not underlying_contract:
        underlying_contract = parts["underlying_contract"]
    underlying_name = _underlying_display_name(prefix, underlying_contract)
    strike_label = _format_strike(strike_price)
    status = str(row.get("合约状态") or "").strip()
    is_active = status in {"", "1"} and (not expiry_date or expiry_date >= datetime.now().strftime("%Y-%m-%d"))

    return {
        "instrument_type": "option",
        "market": OPTIONS_MARKET,
        "symbol": symbol,
        "display_symbol": symbol,
        "name": f"{underlying_name} {_option_type_label(option_type)} {strike_label}".strip(),
        "exchange": exchange,
        "exchange_name": exchange,
        "currency": "CNY",
        "underlying_symbol": underlying_contract,
        "underlying_name": underlying_name,
        "contract_multiplier": float(_safe_float(row.get("合约乘数")) or 1.0),
        "tick_size": _safe_float(row.get("最小变动价位")),
        "expiry_date": expiry_date,
        "is_main_contract": False,
        "option_type": option_type,
        "strike_price": strike_price,
        "exercise_style": "",
        "status": "active" if is_active else "inactive",
        "meta": {
            "product_key": config["product_key"],
            "quote_product": config["quote_product"],
            "exchange_name": exchange,
            "underlying_prefix": prefix,
        },
    }


def get_supported_option_catalog(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE["items"]
        if (
            not force_refresh
            and cached
            and now - float(_CATALOG_CACHE["ts"] or 0) < _CATALOG_CACHE_TTL_SECONDS
        ):
            return list(cached)

    df = ak.option_contract_info_ctp()
    rows: list[dict[str, Any]] = []
    for raw in df.to_dict(orient="records"):
        payload = _row_to_payload(raw)
        if payload:
            rows.append(payload)

    rows.sort(
        key=lambda item: (
            0 if item.get("status") == "active" else 1,
            str(item.get("expiry_date") or ""),
            str(item.get("symbol") or ""),
        )
    )

    with _CATALOG_LOCK:
        _CATALOG_CACHE["ts"] = now
        _CATALOG_CACHE["items"] = list(rows)
    return rows


def resolve_option_contract(symbol: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    normalized = _normalize_option_symbol(symbol)
    if not normalized:
        return None
    for item in get_supported_option_catalog(force_refresh=force_refresh):
        if str(item.get("symbol") or "").upper() == normalized:
            return dict(item)
    return None


def _option_search_tokens(item: dict[str, Any]) -> list[str]:
    symbol = str(item.get("symbol") or "")
    parts = _extract_symbol_parts(symbol) or {}
    prefix = parts.get("underlying_prefix", "")
    aliases = _SUPPORTED_PRODUCTS.get(prefix, {}).get("aliases", set())
    option_type = str(item.get("option_type") or "")
    strike = _format_strike(_safe_float(item.get("strike_price")))
    tokens = [
        symbol,
        str(item.get("name") or ""),
        str(item.get("underlying_symbol") or ""),
        str(item.get("underlying_name") or ""),
        strike,
        option_type,
        "看涨" if option_type == "call" else "看跌" if option_type == "put" else "",
        "购" if option_type == "call" else "沽" if option_type == "put" else "",
        "c" if option_type == "call" else "p" if option_type == "put" else "",
        *aliases,
    ]
    return [_search_key(token) for token in tokens if str(token or "").strip()]


def search_option_instruments(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return []
    aliases = _option_query_aliases(q)
    results: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    for item in get_supported_option_catalog():
        if item.get("status") != "active":
            continue
        haystack = _option_search_tokens(item)
        if not haystack:
            continue
        joined = "".join(haystack)
        tokenized_query = [token for token in re.split(r"[\s/|]+", q) if token]
        token_aliases = [_option_query_aliases(token) for token in tokenized_query]
        matched = any(alias in joined for alias in aliases)
        if not matched and token_aliases:
            matched = all(any(alias in joined for alias in token_set) for token_set in token_aliases)
        if not matched:
            continue
        results[str(item.get("symbol") or "")] = dict(item)
        if len(results) >= max(1, limit):
            break
    return list(results.values())[: max(1, limit)]


def _quote_cache_key(meta: dict[str, Any]) -> str:
    exchange = str(meta.get("exchange") or "").strip().lower()
    product = str(((meta.get("meta") or {}).get("quote_product")) or "").strip().lower()
    underlying = str(meta.get("underlying_symbol") or "").strip().lower()
    return f"{exchange}:{product}:{underlying}"


def _fetch_option_chain_quotes(meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cache_key = _quote_cache_key(meta)
    now = time.time()
    with _QUOTE_LOCK:
        cached = _QUOTE_CACHE.get(cache_key)
        if cached and now - cached[0] < _QUOTE_CACHE_TTL_SECONDS:
            return {key: dict(value) for key, value in cached[1].items()}

    meta_info = meta.get("meta") or {}
    product = str(meta_info.get("quote_product") or "").strip().lower()
    exchange = str(meta.get("exchange") or "").strip().lower()
    underlying = str(meta.get("underlying_symbol") or "").strip().lower()
    if not product or not exchange or not underlying:
        return {}

    response = httpx.get(
        "https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData",
        params={
            "type": "futures",
            "product": product,
            "exchange": exchange,
            "pinzhong": underlying,
        },
        headers={"User-Agent": "PanWatch/1.0"},
        timeout=20.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    data = (((payload.get("result") or {}).get("data") or {}))
    rows: dict[str, dict[str, Any]] = {}

    for side, option_type in (("up", "call"), ("down", "put")):
        for row in list(data.get(side) or []):
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            has_embedded_strike = len(row) >= 9
            code_index = 8 if has_embedded_strike else 7
            symbol = _normalize_option_symbol(row[code_index])
            current_price = _safe_float(row[2])
            change_amount = _safe_float(row[6])
            if (
                change_amount is not None
                and current_price is not None
                and abs(change_amount) > max(abs(current_price) * 5, 1000)
            ):
                change_amount = None
            prev_close = None
            if current_price is not None and change_amount is not None:
                prev_close = current_price - change_amount
            change_pct = None
            if change_amount is not None and prev_close not in (None, 0):
                change_pct = change_amount / float(prev_close) * 100
            symbol_parts = _extract_symbol_parts(symbol) or {}
            strike_price = _safe_float(row[7]) if has_embedded_strike else _safe_float(symbol_parts.get("strike"))
            rows[symbol] = {
                "instrument_type": "option",
                "market": OPTIONS_MARKET,
                "symbol": symbol,
                "display_symbol": symbol,
                "current_price": current_price,
                "change_amount": change_amount,
                "change_pct": change_pct,
                "prev_close": prev_close,
                "open_price": None,
                "high_price": None,
                "low_price": None,
                "volume": None,
                "turnover": None,
                "position": _safe_float(row[5]),
                "bid_price": _safe_float(row[1]),
                "ask_price": _safe_float(row[3]),
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "tick_time": "",
                "option_type": option_type,
                "strike_price": strike_price,
            }

    with _QUOTE_LOCK:
        _QUOTE_CACHE[cache_key] = (now, rows)
    return {key: dict(value) for key, value in rows.items()}


def get_option_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized = []
    for symbol in symbols or []:
        value = _normalize_option_symbol(symbol)
        if value and value not in normalized:
            normalized.append(value)
    if not normalized:
        return {}

    resolved = {
        symbol: resolve_option_contract(symbol)
        for symbol in normalized
    }
    grouped: dict[str, list[str]] = {}
    for symbol, meta in resolved.items():
        if not meta:
            continue
        grouped.setdefault(_quote_cache_key(meta), []).append(symbol)

    results: dict[str, dict[str, Any]] = {}
    for cache_key, group_symbols in grouped.items():
        meta = resolved.get(group_symbols[0]) or {}
        try:
            chain = _fetch_option_chain_quotes(meta)
        except Exception as exc:
            logger.warning("option quote fetch failed for %s: %s", cache_key, exc)
            continue
        for symbol in group_symbols:
            quote = chain.get(symbol)
            meta_payload = resolved.get(symbol) or {}
            if quote:
                merged = dict(meta_payload)
                merged.update({k: v for k, v in quote.items() if v not in (None, "", [])})
                if quote.get("option_type"):
                    merged["option_type"] = quote["option_type"]
                if quote.get("strike_price") is not None:
                    merged["strike_price"] = quote["strike_price"]
                results[symbol] = merged

    for symbol in normalized:
        if symbol not in results and resolved.get(symbol):
            meta = dict(resolved[symbol] or {})
            meta.update(
                {
                    "current_price": None,
                    "change_pct": None,
                    "change_amount": None,
                    "prev_close": None,
                    "open_price": None,
                    "high_price": None,
                    "low_price": None,
                    "volume": None,
                    "turnover": None,
                    "position": None,
                    "bid_price": None,
                    "ask_price": None,
                    "trade_date": "",
                    "tick_time": "",
                }
            )
            results[symbol] = meta

    return results


def get_option_daily_bars(symbol: str, *, days: int = 60) -> list[dict[str, Any]]:
    normalized = _normalize_option_symbol(symbol)
    meta = resolve_option_contract(normalized)
    if not meta:
        return []
    source_symbol = _option_source_symbol(normalized)
    prefix = str((meta.get("meta") or {}).get("underlying_prefix") or "").upper()
    if prefix == "HO":
        df = ak.option_cffex_sz50_daily_sina(symbol=source_symbol)
    else:
        df = ak.option_commodity_hist_sina(symbol=source_symbol)
    rows: list[dict[str, Any]] = []
    for row in df.tail(max(1, int(days or 1))).to_dict(orient="records"):
        open_price = _safe_float(row.get("open"))
        close_price = _safe_float(row.get("close"))
        high_price = _safe_float(row.get("high"))
        low_price = _safe_float(row.get("low"))
        if None in (open_price, close_price, high_price, low_price):
            continue
        rows.append(
            {
                "date": _format_trade_date(row.get("date")),
                "open": float(open_price),
                "close": float(close_price),
                "high": float(high_price),
                "low": float(low_price),
                "volume": float(_safe_float(row.get("volume")) or 0.0),
            }
        )
    return rows
