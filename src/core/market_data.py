from __future__ import annotations

import asyncio
import json
import logging
import math
from numbers import Real
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from src.collectors.akshare_collector import (
    _fetch_sina_global_futures_quotes,
    _fetch_tencent_quotes,
    _tencent_symbol,
)
from src.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.collectors.kline_collector import KlineCollector, KlineData
from src.config import Settings
from src.core.instrument_service import get_futures_quotes
from src.models.market import IndexData, MarketCode, StockData

logger = logging.getLogger(__name__)

SUPPORTED_RUNTIME_MARKETS = {
    MarketCode.CN,
    MarketCode.CN_FUT,
    MarketCode.CN_OPT,
}
SUPPORTED_DISCOVERY_MARKETS = {MarketCode.CN}

MARKET_INDICES = [
    {
        "symbol": "000001",
        "name": "SSE Composite",
        "market": "CN",
        "tencent_symbol": "sh000001",
        "response_symbol": "000001",
    },
    {
        "symbol": "399001",
        "name": "SZSE Component",
        "market": "CN",
        "tencent_symbol": "sz399001",
        "response_symbol": "399001",
    },
    {
        "symbol": "399006",
        "name": "ChiNext",
        "market": "CN",
        "tencent_symbol": "sz399006",
        "response_symbol": "399006",
    },
]

MARKET_REFERENCE_QUOTES = [
    {
        "symbol": "hf_XAU",
        "name": "现货黄金",
        "market": "CN_FUT",
    },
    {
        "symbol": "hf_CL",
        "name": "WTI原油",
        "market": "CN_FUT",
    },
]


@dataclass
class CacheEnvelope:
    value: Any
    fresh_until: float
    stale_until: float


def _now_ts() -> float:
    return time.time()


def _normalize_market(market: MarketCode | str) -> MarketCode:
    if isinstance(market, MarketCode):
        return market
    try:
        return MarketCode(str(market or "CN").upper())
    except Exception:
        return MarketCode.CN


def _ensure_supported_runtime_market(market: MarketCode) -> MarketCode:
    if market not in SUPPORTED_RUNTIME_MARKETS:
        raise ValueError(f"unsupported market in CN-only mode: {market.value}")
    return market


def _ensure_supported_discovery_market(market: MarketCode | str) -> MarketCode:
    market_code = _normalize_market(market)
    if market_code not in SUPPORTED_DISCOVERY_MARKETS:
        raise ValueError(f"discovery supports CN only in current mode: {market_code.value}")
    return market_code


def _copy_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    return value


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_json_safe_value(item) for item in value)
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            if not math.isfinite(float(value)):
                return None
        except Exception:
            return value
    return value


class RedisBridge:
    def __init__(self, url: str):
        self.url = (url or "").strip()
        self._client = None
        self._available = False
        if not self.url:
            return
        try:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
            self._client.ping()
            self._available = True
        except Exception as exc:
            logger.warning("redis unavailable, fallback to in-memory cache only: %s", exc)
            self._client = None
            self._available = False

    def get(self, key: str) -> str | None:
        if not self._available or self._client is None:
            return None
        try:
            return self._client.get(key)
        except Exception:
            return None

    def set(self, key: str, value: str, ex: int) -> None:
        if not self._available or self._client is None:
            return
        try:
            self._client.set(key, value, ex=ex)
        except Exception:
            logger.debug("redis set failed for %s", key, exc_info=True)

    def setnx_with_expiry(self, key: str, value: str, ex: int) -> bool:
        if not self._available or self._client is None:
            return False
        try:
            return bool(self._client.set(key, value, nx=True, ex=ex))
        except Exception:
            return False

    def delete_if_value(self, key: str, expected: str) -> None:
        if not self._available or self._client is None:
            return
        try:
            current = self._client.get(key)
            if current == expected:
                self._client.delete(key)
        except Exception:
            logger.debug("redis delete failed for %s", key, exc_info=True)


class MarketDataFacade:
    def __init__(self):
        settings = Settings()
        self._memory_cache: dict[str, CacheEnvelope] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._cache_lock = threading.Lock()
        self._redis = RedisBridge(settings.redis_url)

    def _cache_key(self, namespace: str, suffix: str) -> str:
        return f"panwatch:{namespace}:{suffix}"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._cache_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _read_cache(self, key: str) -> tuple[str, Any]:
        now = _now_ts()
        with self._cache_lock:
            entry = self._memory_cache.get(key)
        if entry and now <= entry.stale_until:
            state = "fresh" if now <= entry.fresh_until else "stale"
            return state, _copy_value(entry.value)

        raw = self._redis.get(key)
        if not raw:
            return "miss", None
        try:
            payload = json.loads(raw)
            envelope = CacheEnvelope(
                value=payload.get("value"),
                fresh_until=float(payload.get("fresh_until") or 0),
                stale_until=float(payload.get("stale_until") or 0),
            )
        except Exception:
            return "miss", None
        if now > envelope.stale_until:
            return "miss", None
        with self._cache_lock:
            self._memory_cache[key] = envelope
        state = "fresh" if now <= envelope.fresh_until else "stale"
        return state, _copy_value(envelope.value)

    def _write_cache(self, key: str, value: Any, *, ttl: int, stale_ttl: int) -> Any:
        now = _now_ts()
        envelope = CacheEnvelope(
            value=_copy_value(value),
            fresh_until=now + max(1, ttl),
            stale_until=now + max(ttl + 1, stale_ttl),
        )
        with self._cache_lock:
            self._memory_cache[key] = envelope
        payload = {
            "value": value,
            "fresh_until": envelope.fresh_until,
            "stale_until": envelope.stale_until,
        }
        self._redis.set(key, json.dumps(payload, ensure_ascii=False), ex=max(1, stale_ttl))
        return _copy_value(value)

    def _distributed_lock_key(self, cache_key: str) -> str:
        return f"{cache_key}:lock"

    def _acquire_distributed_lock(self, cache_key: str, seconds: int = 20) -> str | None:
        token = uuid.uuid4().hex
        lock_key = self._distributed_lock_key(cache_key)
        if self._redis.setnx_with_expiry(lock_key, token, ex=max(5, seconds)):
            return token
        return None

    def _release_distributed_lock(self, cache_key: str, token: str | None) -> None:
        if not token:
            return
        self._redis.delete_if_value(self._distributed_lock_key(cache_key), token)

    def _refresh_in_background(
        self,
        cache_key: str,
        ttl: int,
        stale_ttl: int,
        loader,
        metric: str,
    ) -> None:
        lock = self._lock_for(cache_key)
        if not lock.acquire(blocking=False):
            return

        def _runner():
            token = self._acquire_distributed_lock(cache_key)
            try:
                start = time.perf_counter()
                value = loader()
                self._write_cache(cache_key, value, ttl=ttl, stale_ttl=stale_ttl)
                logger.info(
                    "market_data_refresh metric=%s cache=stale duration_ms=%s",
                    metric,
                    int((time.perf_counter() - start) * 1000),
                )
            except Exception:
                logger.warning("market_data background refresh failed for %s", metric, exc_info=True)
            finally:
                self._release_distributed_lock(cache_key, token)
                lock.release()

        threading.Thread(target=_runner, daemon=True, name=f"market-data-{metric}").start()

    def _cached_fetch(
        self,
        cache_key: str,
        *,
        ttl: int,
        stale_ttl: int,
        loader,
        metric: str,
    ):
        state, cached_value = self._read_cache(cache_key)
        if state == "fresh":
            logger.info("market_data metric=%s cache=fresh", metric)
            return cached_value
        if state == "stale":
            logger.info("market_data metric=%s cache=stale", metric)
            self._refresh_in_background(cache_key, ttl, stale_ttl, loader, metric)
            return cached_value

        lock = self._lock_for(cache_key)
        with lock:
            state, cached_value = self._read_cache(cache_key)
            if state == "fresh":
                logger.info("market_data metric=%s cache=fresh-after-wait", metric)
                return cached_value
            if state == "stale":
                logger.info("market_data metric=%s cache=stale-after-wait", metric)
                self._refresh_in_background(cache_key, ttl, stale_ttl, loader, metric)
                return cached_value

            token = self._acquire_distributed_lock(cache_key)
            try:
                start = time.perf_counter()
                value = loader()
                result = self._write_cache(cache_key, value, ttl=ttl, stale_ttl=stale_ttl)
                logger.info(
                    "market_data metric=%s cache=miss duration_ms=%s",
                    metric,
                    int((time.perf_counter() - start) * 1000),
                )
                return result
            except Exception:
                if cached_value is not None:
                    logger.warning("market_data metric=%s cache=fallback-stale", metric)
                    return cached_value
                raise
            finally:
                self._release_distributed_lock(cache_key, token)

    def _quote_response(self, symbol: str, market: MarketCode, quote: dict | None) -> dict:
        default_instrument_type = (
            "future"
            if market == MarketCode.CN_FUT
            else "option"
            if market == MarketCode.CN_OPT
            else "equity"
        )
        if not quote:
            return {
                "symbol": symbol,
                "market": market.value,
                "name": None,
                "current_price": None,
                "change_pct": None,
                "change_amount": None,
                "prev_close": None,
                "open_price": None,
                "high_price": None,
                "low_price": None,
                "volume": None,
                "turnover": None,
                "turnover_rate": None,
                "pe_ratio": None,
                "total_market_value": None,
                "circulating_market_value": None,
                "instrument_type": default_instrument_type,
                "exchange": None,
                "currency": None,
                "underlying_symbol": None,
                "underlying_name": None,
                "contract_multiplier": None,
                "tick_size": None,
                "expiry_date": None,
                "is_main_contract": None,
                "position": None,
                "bid_price": None,
                "ask_price": None,
                "trade_date": None,
                "tick_time": None,
            }
        payload = {
            "symbol": symbol,
            "market": market.value,
            "name": quote.get("name"),
            "current_price": quote.get("current_price"),
            "change_pct": quote.get("change_pct"),
            "change_amount": quote.get("change_amount"),
            "prev_close": quote.get("prev_close"),
            "open_price": quote.get("open_price"),
            "high_price": quote.get("high_price"),
            "low_price": quote.get("low_price"),
            "volume": quote.get("volume"),
            "turnover": quote.get("turnover"),
            "turnover_rate": quote.get("turnover_rate"),
            "pe_ratio": quote.get("pe_ratio"),
            "total_market_value": quote.get("total_market_value"),
            "circulating_market_value": quote.get("circulating_market_value"),
            "instrument_type": quote.get("instrument_type") or default_instrument_type,
            "exchange": quote.get("exchange"),
            "currency": quote.get("currency"),
            "underlying_symbol": quote.get("underlying_symbol"),
            "underlying_name": quote.get("underlying_name"),
            "contract_multiplier": quote.get("contract_multiplier"),
            "tick_size": quote.get("tick_size"),
            "expiry_date": quote.get("expiry_date"),
            "is_main_contract": quote.get("is_main_contract"),
            "position": quote.get("position"),
            "bid_price": quote.get("bid_price"),
            "ask_price": quote.get("ask_price"),
            "trade_date": quote.get("trade_date"),
            "tick_time": quote.get("tick_time"),
        }
        return _json_safe_value(payload)

    def get_quotes_batch(self, items: Iterable[dict[str, Any] | tuple[str, str]]) -> list[dict]:
        normalized: list[tuple[MarketCode, str]] = []
        for item in items or []:
            if isinstance(item, dict):
                symbol = str(item.get("symbol") or "").strip().upper()
                market = _ensure_supported_runtime_market(_normalize_market(item.get("market") or "CN"))
            else:
                symbol = str(item[0] or "").strip().upper()
                market = _ensure_supported_runtime_market(_normalize_market(item[1] or "CN"))
            if symbol:
                normalized.append((market, symbol))
        if not normalized:
            return []

        grouped: dict[MarketCode, list[str]] = {}
        for market, symbol in normalized:
            grouped.setdefault(market, [])
            if symbol not in grouped[market]:
                grouped[market].append(symbol)

        quotes_by_market: dict[MarketCode, dict[str, dict]] = {}
        for market, symbols in grouped.items():
            cache_key = self._cache_key("quotes", f"{market.value}:{'|'.join(sorted(symbols))}")
            ttl = 30 if market == MarketCode.CN_FUT else 8
            stale_ttl = 120 if market == MarketCode.CN_FUT else 30

            def _loader(market_code=market, market_symbols=list(symbols)):
                if market_code == MarketCode.CN_FUT:
                    mapped = get_futures_quotes(market_symbols)
                elif market_code == MarketCode.CN_OPT:
                    mapped = {}
                else:
                    tencent_symbols = [_tencent_symbol(symbol, market_code) for symbol in market_symbols]
                    rows = _fetch_tencent_quotes(tencent_symbols)
                    mapped = {str(row.get("symbol") or "").upper(): row for row in rows}
                return {
                    symbol: self._quote_response(symbol, market_code, mapped.get(symbol))
                    for symbol in market_symbols
                }

            quotes_by_market[market] = self._cached_fetch(
                cache_key,
                ttl=ttl,
                stale_ttl=stale_ttl,
                loader=_loader,
                metric=f"quotes:{market.value}:{len(symbols)}",
            )

        return [
            deepcopy(quotes_by_market.get(market, {}).get(symbol) or self._quote_response(symbol, market, None))
            for market, symbol in normalized
        ]

    def get_quote(self, symbol: str, market: MarketCode | str = MarketCode.CN) -> dict | None:
        rows = self.get_quotes_batch(
            [{"symbol": symbol, "market": _ensure_supported_runtime_market(_normalize_market(market)).value}]
        )
        return rows[0] if rows else None

    def get_quotes_map(self, items: Iterable[dict[str, Any] | tuple[str, str]]) -> dict[tuple[str, str], dict]:
        rows = self.get_quotes_batch(items)
        return {
            (str(row.get("market") or "CN"), str(row.get("symbol") or "")): row
            for row in rows
        }

    def get_stock_data_batch(self, items: Iterable[dict[str, Any] | tuple[str, str]]) -> list[StockData]:
        rows = self.get_quotes_batch(items)
        out: list[StockData] = []
        for row in rows:
            market = _normalize_market(row.get("market") or "CN")
            price = row.get("current_price")
            if price is None:
                continue
            out.append(
                StockData(
                    symbol=str(row.get("symbol") or ""),
                    name=str(row.get("name") or row.get("symbol") or ""),
                    market=market,
                    current_price=float(price),
                    change_pct=float(row.get("change_pct") or 0),
                    change_amount=float(row.get("change_amount") or 0),
                    volume=float(row.get("volume") or 0),
                    turnover=float(row.get("turnover") or 0),
                    open_price=float(row.get("open_price") or 0),
                    high_price=float(row.get("high_price") or 0),
                    low_price=float(row.get("low_price") or 0),
                    prev_close=float(row.get("prev_close") or 0),
                    timestamp=datetime.now(),
                )
            )
        return out

    def get_market_indices(self) -> list[dict]:
        cache_key = self._cache_key("indices", "major")

        def _loader():
            tencent_symbols = [item["tencent_symbol"] for item in MARKET_INDICES]
            rows = _fetch_tencent_quotes(tencent_symbols)
            mapped = {str(row.get("symbol") or ""): row for row in rows}
            result = []
            for item in MARKET_INDICES:
                quote = mapped.get(item["response_symbol"])
                result.append(
                    {
                        "symbol": item["symbol"],
                        "name": item["name"],
                        "market": item["market"],
                        "current_price": quote.get("current_price") if quote else None,
                        "change_pct": quote.get("change_pct") if quote else None,
                        "change_amount": quote.get("change_amount") if quote else None,
                        "prev_close": quote.get("prev_close") if quote else None,
                        "open_price": quote.get("open_price") if quote else None,
                        "high_price": quote.get("high_price") if quote else None,
                        "low_price": quote.get("low_price") if quote else None,
                        "trade_date": "",
                        "tick_time": "",
                        "source_name": item["name"],
                    }
                )

            future_map: dict[str, dict[str, Any]] = {}
            try:
                future_rows = _fetch_sina_global_futures_quotes(
                    [item["symbol"] for item in MARKET_REFERENCE_QUOTES]
                )
                future_map = {
                    str(row.get("symbol") or "").upper(): row
                    for row in future_rows
                }
            except Exception:
                logger.warning("failed to load market reference futures", exc_info=True)
            for item in MARKET_REFERENCE_QUOTES:
                quote = future_map.get(str(item["symbol"]).upper())
                result.append(
                    {
                        "symbol": item["symbol"],
                        "name": item["name"],
                        "market": item["market"],
                        "current_price": quote.get("current_price") if quote else None,
                        "change_pct": quote.get("change_pct") if quote else None,
                        "change_amount": quote.get("change_amount") if quote else None,
                        "prev_close": quote.get("prev_close") if quote else None,
                        "open_price": quote.get("open_price") if quote else None,
                        "high_price": quote.get("high_price") if quote else None,
                        "low_price": quote.get("low_price") if quote else None,
                        "trade_date": quote.get("trade_date") if quote else "",
                        "tick_time": quote.get("tick_time") if quote else "",
                        "source_name": quote.get("name") if quote else item["name"],
                    }
                )
            return result

        return self._cached_fetch(
            cache_key,
            ttl=12,
            stale_ttl=60,
            loader=_loader,
            metric="indices",
        )

    def get_market_index_objects(self) -> list[IndexData]:
        items = self.get_market_indices()
        out: list[IndexData] = []
        for item in items:
            price = item.get("current_price")
            if price is None:
                continue
            out.append(
                IndexData(
                    symbol=str(item.get("symbol") or ""),
                    name=str(item.get("name") or ""),
                    market=_normalize_market(item.get("market") or "CN"),
                    current_price=float(price),
                    change_pct=float(item.get("change_pct") or 0),
                    change_amount=float(item.get("change_amount") or 0),
                    volume=0.0,
                    turnover=0.0,
                    timestamp=datetime.now(),
                )
            )
        return out

    def get_klines(self, symbol: str, market: MarketCode | str = MarketCode.CN, *, days: int = 60) -> list[KlineData]:
        market_code = _ensure_supported_runtime_market(_normalize_market(market))
        cache_key = self._cache_key("klines", f"{market_code.value}:{symbol.upper()}:{int(days)}")

        def _loader():
            rows = KlineCollector(market_code).get_klines(symbol, days=days)
            return [
                {
                    "date": row.date,
                    "open": row.open,
                    "close": row.close,
                    "high": row.high,
                    "low": row.low,
                    "volume": row.volume,
                }
                for row in rows
            ]

        data = self._cached_fetch(
            cache_key,
            ttl=120,
            stale_ttl=600,
            loader=_loader,
            metric=f"klines:{market_code.value}",
        )
        return [
            KlineData(
                date=str(row.get("date") or ""),
                open=float(row.get("open") or 0),
                close=float(row.get("close") or 0),
                high=float(row.get("high") or 0),
                low=float(row.get("low") or 0),
                volume=float(row.get("volume") or 0),
            )
            for row in (data or [])
        ]

    def get_kline_summary(self, symbol: str, market: MarketCode | str = MarketCode.CN) -> dict:
        market_code = _ensure_supported_runtime_market(_normalize_market(market))
        cache_key = self._cache_key("kline-summary", f"{market_code.value}:{symbol.upper()}")

        def _loader():
            return KlineCollector(market_code).get_kline_summary(symbol)

        return self._cached_fetch(
            cache_key,
            ttl=90,
            stale_ttl=300,
            loader=_loader,
            metric=f"kline-summary:{market_code.value}",
        )

    def get_kline_summary_batch(self, items: Iterable[dict[str, Any] | tuple[str, str]]) -> list[dict]:
        normalized: list[tuple[str, MarketCode]] = []
        seen: set[tuple[str, str]] = set()
        for item in items or []:
            if isinstance(item, dict):
                symbol = str(item.get("symbol") or "").strip().upper()
                market = _ensure_supported_runtime_market(_normalize_market(item.get("market") or "CN"))
            else:
                symbol = str(item[0] or "").strip().upper()
                market = _ensure_supported_runtime_market(_normalize_market(item[1] or "CN"))
            key = (market.value, symbol)
            if symbol and key not in seen:
                normalized.append((symbol, market))
                seen.add(key)
        if not normalized:
            return []

        results: dict[tuple[str, str], dict] = {}
        max_workers = min(6, len(normalized))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.get_kline_summary, symbol, market): (market.value, symbol)
                for symbol, market in normalized
            }
            for future, key in future_map.items():
                try:
                    results[key] = future.result()
                except Exception:
                    results[key] = {}
        return [
            {
                "symbol": symbol,
                "market": market.value,
                "summary": deepcopy(results.get((market.value, symbol)) or {}),
            }
            for symbol, market in normalized
        ]

    def get_fx_rate(self, pair: str) -> float:
        return 1.0

    def get_live_hot_stocks(self, market: str, mode: str, limit: int, *, proxy: str | None = None) -> list[dict]:
        market_code = _ensure_supported_discovery_market(market).value
        mode_value = (mode or "turnover").lower()
        cache_key = self._cache_key("discovery-stocks", f"{market_code}:{mode_value}:{int(limit)}")

        def _loader():
            async def _run():
                collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
                items = await collector.fetch_hot_stocks(
                    market=market_code,
                    mode=mode_value,
                    limit=max(1, min(int(limit), 100)),
                )
                return [
                    {
                        "symbol": item.symbol,
                        "market": item.market,
                        "name": item.name,
                        "price": item.price,
                        "change_pct": item.change_pct,
                        "turnover": item.turnover,
                        "volume": item.volume,
                    }
                    for item in items
                ]

            return asyncio.run(_run())

        return self._cached_fetch(
            cache_key,
            ttl=45,
            stale_ttl=180,
            loader=_loader,
            metric=f"discovery-stocks:{market_code}:{mode_value}",
        )

    def get_live_hot_boards(self, market: str, mode: str, limit: int, *, proxy: str | None = None) -> list[dict]:
        market_code = _ensure_supported_discovery_market(market).value
        mode_value = (mode or "gainers").lower()
        cache_key = self._cache_key("discovery-boards", f"{market_code}:{mode_value}:{int(limit)}")

        def _loader():
            async def _run():
                collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
                items = await collector.fetch_hot_boards(
                    market=market_code,
                    mode=mode_value,
                    limit=max(1, min(int(limit), 100)),
                )
                return [
                    {
                        "code": item.code,
                        "name": item.name,
                        "change_pct": item.change_pct,
                        "change_amount": item.change_amount,
                        "turnover": item.turnover,
                    }
                    for item in items
                ]

            return asyncio.run(_run())

        return self._cached_fetch(
            cache_key,
            ttl=60,
            stale_ttl=180,
            loader=_loader,
            metric=f"discovery-boards:{market_code}:{mode_value}",
        )

    def get_live_board_stocks(self, board_code: str, mode: str, limit: int, *, proxy: str | None = None) -> list[dict]:
        code = str(board_code or "").strip()
        if code.startswith(("HK_", "US_")):
            raise ValueError(f"discovery board unsupported in CN-only mode: {code}")
        mode_value = (mode or "gainers").lower()
        cache_key = self._cache_key("discovery-board-stocks", f"{code}:{mode_value}:{int(limit)}")

        def _loader():
            async def _run():
                collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
                items = await collector.fetch_board_stocks(
                    board_code=code,
                    mode=mode_value,
                    limit=max(1, min(int(limit), 100)),
                )
                return [
                    {
                        "symbol": item.symbol,
                        "market": "CN",
                        "name": item.name,
                        "price": item.price,
                        "change_pct": item.change_pct,
                        "turnover": item.turnover,
                        "volume": item.volume,
                    }
                    for item in items
                ]

            return asyncio.run(_run())

        return self._cached_fetch(
            cache_key,
            ttl=60,
            stale_ttl=180,
            loader=_loader,
            metric=f"discovery-board-stocks:{code}:{mode_value}",
        )


market_data = MarketDataFacade()
