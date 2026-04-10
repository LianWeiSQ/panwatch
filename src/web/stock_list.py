"""CN stock list cache and search helpers."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
CACHE_FILE = os.path.join(DATA_DIR, "stock_list_cache.json")
CACHE_TTL = 86400 * 7
PAGE_SIZE = 100

EASTMONEY_URL = "https://80.push2delay.eastmoney.com/api/qt/clist/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://quote.eastmoney.com/",
}

CN_PARAMS = {
    "po": "1",
    "np": "1",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "fields": "f12,f14",
}

BJ_PARAMS = {
    "po": "1",
    "np": "1",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
    "fs": "m:0+t:81",
    "fields": "f12,f14",
}


def _load_cache() -> list[dict] | None:
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    if time.time() - float(payload.get("ts") or 0) >= CACHE_TTL:
        return None
    rows = payload.get("stocks")
    return rows if isinstance(rows, list) else None


def _save_cache(stocks: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"ts": time.time(), "stocks": stocks}, fh, ensure_ascii=False)


def _fetch_page(
    client: httpx.Client,
    *,
    params: dict[str, str],
    page: int,
) -> list[dict]:
    payload = {**params, "pn": str(page), "pz": str(PAGE_SIZE)}
    response = client.get(EASTMONEY_URL, params=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    items = (data.get("data") or {}).get("diff") or []
    return [
        {"symbol": str(item.get("f12") or ""), "name": str(item.get("f14") or ""), "market": "CN"}
        for item in items
        if str(item.get("f12") or "").strip()
    ]


def _fetch_market_from_eastmoney(params: dict[str, str]) -> list[dict]:
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        first_page = _fetch_page(client, params=params, page=1)
        response = client.get(EASTMONEY_URL, params={**params, "pn": "1", "pz": str(PAGE_SIZE)})
        response.raise_for_status()
        total = int(((response.json().get("data") or {}).get("total")) or 0)
        if total <= PAGE_SIZE:
            return first_page

        stocks = list(first_page)
        pages_needed = (total + PAGE_SIZE - 1) // PAGE_SIZE
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(_fetch_page, client, params=params, page=page)
                for page in range(2, pages_needed + 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    stocks.extend(future.result())
                except Exception:
                    logger.warning("eastmoney stock list page fetch failed", exc_info=True)
        return stocks


def _fetch_from_akshare() -> list[dict]:
    import akshare as ak

    df = ak.stock_info_a_code_name()
    out: list[dict] = []
    for _, row in df.iterrows():
        symbol = str(row.get("code") or "").strip()
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "name": str(row.get("name") or symbol),
                "market": "CN",
            }
        )
    return out


def refresh_stock_list() -> list[dict]:
    """Refresh the CN/BJ stock universe and persist it to cache."""
    stocks: list[dict] = []

    try:
        cn_rows = _fetch_market_from_eastmoney(CN_PARAMS)
        stocks.extend(cn_rows)
        logger.info("eastmoney CN stock list refreshed: count=%s", len(cn_rows))
    except Exception as exc:
        logger.warning("eastmoney CN stock list refresh failed: %s", exc)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_fetch_from_akshare)
                cn_rows = future.result(timeout=15)
            stocks.extend(cn_rows)
            logger.info("akshare CN stock list refreshed: count=%s", len(cn_rows))
        except Exception:
            logger.exception("fallback CN stock list refresh failed")

    try:
        bj_rows = _fetch_market_from_eastmoney(BJ_PARAMS)
        stocks.extend(bj_rows)
        logger.info("eastmoney BJ stock list refreshed: count=%s", len(bj_rows))
    except Exception:
        logger.warning("eastmoney BJ stock list refresh failed", exc_info=True)

    deduped: list[dict] = []
    seen: set[str] = set()
    for row in stocks:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(
            {
                "symbol": symbol,
                "name": str(row.get("name") or symbol),
                "market": "CN",
            }
        )

    if deduped:
        _save_cache(deduped)
    return deduped


def get_stock_list() -> list[dict]:
    cached = _load_cache()
    if cached:
        return cached
    return refresh_stock_list()


def _normalize_symbol(code: str) -> str:
    value = (code or "").strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if "." in value:
        value = value.split(".", 1)[0]
    return value


def _realtime_search(query: str, market: str = "", limit: int = 20) -> list[dict]:
    if market and market != "CN":
        return []

    url = (
        "https://searchapi.eastmoney.com/api/suggest/get"
        f"?input={urllib.parse.quote(query)}&type=14&count={max(20, limit * 5)}"
    )
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(url, headers=HEADERS)
            response.raise_for_status()
            data = response.json()
    except Exception:
        logger.warning("realtime CN stock search failed", exc_info=True)
        return []

    items = (data.get("QuotationCodeTable") or {}).get("Data") or []
    results: list[dict] = []
    for item in items:
        classify = str(item.get("Classify") or "").strip()
        security_type = str(item.get("SecurityTypeName") or "").strip()
        code_raw = str(item.get("Code") or "").strip().upper()
        if not (
            classify in {"AStock", "BJStock"}
            or any(token in security_type for token in ("沪", "深", "北"))
            or code_raw.endswith(".BJ")
            or code_raw.startswith("BJ")
        ):
            continue
        symbol = _normalize_symbol(code_raw)
        if not symbol:
            continue
        results.append(
            {
                "symbol": symbol,
                "name": str(item.get("Name") or symbol),
                "market": "CN",
            }
        )
        if len(results) >= limit:
            break
    return results


def _cached_search(query: str, market: str = "", limit: int = 20) -> list[dict]:
    if market and market != "CN":
        return []

    rows = get_stock_list()
    if not rows:
        return []

    q = query.strip().upper()
    if not q:
        return []

    matches: list[tuple[int, dict]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        name = str(row.get("name") or "").upper()
        if symbol.startswith(q):
            matches.append((0, row))
        elif q in name:
            matches.append((1, row))
        elif q in symbol:
            matches.append((2, row))
        if len(matches) >= limit * 2:
            break

    matches.sort(key=lambda item: item[0])
    return [item[1] for item in matches[:limit]]


def search_stocks(query: str, market: str = "", limit: int = 20) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    market_value = str(market or "").strip().upper()
    if market_value and market_value != "CN":
        return []

    results = _realtime_search(q, "CN", limit)
    if len(results) >= limit:
        return results[:limit]

    cached = _cached_search(q, "CN", limit)
    if not results:
        return cached

    seen = {(row.get("market"), row.get("symbol")) for row in results}
    for row in cached:
        key = (row.get("market"), row.get("symbol"))
        if key in seen:
            continue
        results.append(row)
        seen.add(key)
        if len(results) >= limit:
            break
    return results[:limit]
