"""News collectors for stock-specific and global news sources."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod

import httpx
try:
    import tushare as ts
except Exception:  # pragma: no cover - optional dependency
    ts = None

from src.config import Settings
from src.core.cn_symbol import get_cn_prefix

logger = logging.getLogger(__name__)

_news_cache: dict[str, tuple[datetime, list["NewsItem"]]] = {}
_cache_ttl = timedelta(minutes=5)


def _get_cached(key: str) -> list["NewsItem"] | None:
    hit = _news_cache.get(key)
    if not hit:
        return None
    cached_time, data = hit
    if datetime.now() - cached_time > _cache_ttl:
        _news_cache.pop(key, None)
        return None
    return data


def _set_cached(key: str, data: list["NewsItem"]) -> None:
    _news_cache[key] = (datetime.now(), data)


def _clean_text(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _hash_text(*parts: str) -> str:
    raw = "||".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _with_env_defaults(provider: str, config: dict | None) -> dict:
    merged = dict(config or {})
    settings = Settings()
    if provider == "tushare" and not str(merged.get("token") or "").strip():
        merged["token"] = settings.tushare_token
    return merged


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        try:
            if float(value) > 10_000_000_000:
                return datetime.fromtimestamp(float(value) / 1000.0)
            return datetime.fromtimestamp(float(value))
        except Exception:
            return datetime.now()

    text = str(value or "").strip()
    if not text:
        return datetime.now()

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue

    try:
        dt = parsedate_to_datetime(text)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        pass

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return datetime.now()


def _importance_from_text(title: str, content: str = "") -> int:
    text = f"{title} {content}"
    if any(k in text for k in ("重大", "突发", "独家", "业绩预告", "业绩快报", "年报", "并购", "停牌")):
        return 3
    if any(k in text for k in ("快讯", "公告", "研报", "上调", "下调", "回购", "减持", "增持", "利空", "利好")):
        return 2
    if any(k in text for k in ("市场", "公司", "财报", "业务", "产品")):
        return 1
    return 0


def _match_symbols(text: str, symbol_names: dict[str, str] | None) -> list[str]:
    if not symbol_names:
        return []
    matched: list[str] = []
    haystack = (text or "").upper()
    for symbol, name in symbol_names.items():
        if symbol and symbol.upper() in haystack:
            matched.append(symbol)
            continue
        if name and name in text:
            matched.append(symbol)
    return matched


@dataclass
class NewsItem:
    source: str
    external_id: str
    title: str
    content: str
    publish_time: datetime
    symbols: list[str] = field(default_factory=list)
    importance: int = 0
    url: str = ""
    source_name: str = ""
    language: str = "zh"
    summary: str = ""
    cn_summary: str = ""
    payload: dict = field(default_factory=dict)
    relevance_score: float = 0.0


class BaseNewsCollector(ABC):
    source: str = ""

    @abstractmethod
    async def fetch_news(
        self,
        symbols: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[NewsItem]:
        ...


class XueqiuNewsCollector(BaseNewsCollector):
    source = "xueqiu"
    api_url = "https://xueqiu.com/statuses/stock_timeline.json"

    def __init__(self, cookies: str = ""):
        self.cookies = cookies

    def _symbol_id(self, symbol: str) -> str:
        if len(symbol) == 6 and symbol.isdigit():
            prefix = get_cn_prefix(symbol, upper=True)
            if prefix in {"SH", "SZ"}:
                return f"{prefix}{symbol}"
        return symbol

    async def fetch_news(self, symbols: list[str] | None = None, since: datetime | None = None) -> list[NewsItem]:
        if not symbols:
            return []
        a_share_symbols = [s for s in symbols if len(s) == 6 and s.isdigit()]
        if not a_share_symbols:
            return []

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://xueqiu.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies

        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            tasks = [self._fetch_one(client, symbol, since) for symbol in a_share_symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        items: list[NewsItem] = []
        for result in results:
            if isinstance(result, list):
                items.extend(result)
        return items

    async def _fetch_one(self, client: httpx.AsyncClient, symbol: str, since: datetime | None) -> list[NewsItem]:
        try:
            resp = await client.get(
                self.api_url,
                params={
                    "symbol_id": self._symbol_id(symbol),
                    "count": 15,
                    "source": "自选股新闻",
                    "page": 1,
                },
            )
            if resp.status_code == 400:
                return []
            resp.raise_for_status()
            rows = resp.json().get("list", [])
        except Exception as exc:
            logger.debug("xueqiu fetch failed for %s: %s", symbol, exc)
            return []

        out: list[NewsItem] = []
        for row in rows:
            external_id = str(row.get("id") or "")
            title = _clean_text(row.get("title") or row.get("description"))
            if not external_id or not title:
                continue
            publish_time = _parse_time(row.get("created_at"))
            if since and publish_time < since:
                continue
            content = _clean_text(row.get("description"))
            out.append(
                NewsItem(
                    source=self.source,
                    external_id=external_id,
                    title=title,
                    content=content,
                    summary=content[:280],
                    publish_time=publish_time,
                    symbols=[symbol],
                    importance=_importance_from_text(title, content),
                    url=row.get("target") or f"https://xueqiu.com/{row.get('user_id', '')}/{external_id}",
                    source_name="雪球资讯",
                    language="zh",
                    payload=row,
                )
            )
        return out


class EastMoneyStockNewsCollector(BaseNewsCollector):
    source = "eastmoney_news"
    api_url = "https://search-api-web.eastmoney.com/search/jsonp"

    def __init__(self, symbol_names: dict[str, str] | None = None):
        self._symbol_names = symbol_names

    def _get_symbol_names(self, symbols: list[str]) -> dict[str, str]:
        if self._symbol_names:
            return {sym: self._symbol_names.get(sym, sym) for sym in symbols}

        try:
            from src.web.database import SessionLocal
            from src.web.models import Stock

            db = SessionLocal()
            try:
                stocks = db.query(Stock).filter(Stock.symbol.in_(symbols)).all()
                result = {row.symbol: row.name for row in stocks}
                for symbol in symbols:
                    result.setdefault(symbol, symbol)
                return result
            finally:
                db.close()
        except Exception:
            return {symbol: symbol for symbol in symbols}

    async def fetch_news(self, symbols: list[str] | None = None, since: datetime | None = None) -> list[NewsItem]:
        if not symbols:
            return []

        symbol_names = self._get_symbol_names(symbols)
        cache_key = f"eastmoney_news:{','.join(sorted(symbols))}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return [item for item in cached if not since or item.publish_time >= since]

        semaphore = asyncio.Semaphore(5)
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://so.eastmoney.com/",
            "Accept": "*/*",
        }

        async with httpx.AsyncClient(timeout=8, verify=False, headers=headers) as client:
            async def _wrapped(symbol: str, stock_name: str) -> list[NewsItem]:
                async with semaphore:
                    return await self._fetch_one(client, symbol, stock_name, since=None)

            tasks = [_wrapped(symbol, symbol_names.get(symbol, symbol)) for symbol in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: list[NewsItem] = []
        seen: set[str] = set()
        for result in results:
            if not isinstance(result, list):
                continue
            for item in result:
                if item.external_id in seen:
                    continue
                seen.add(item.external_id)
                all_items.append(item)

        _set_cached(cache_key, all_items)
        return [item for item in all_items if not since or item.publish_time >= since]

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        stock_name: str,
        since: datetime | None,
    ) -> list[NewsItem]:
        search_param = {
            "uid": "",
            "keyword": stock_name,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": 15,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        try:
            resp = await client.get(
                self.api_url,
                params={"cb": "jQuery", "param": json.dumps(search_param, separators=(",", ":"))},
            )
            resp.raise_for_status()
            text = resp.text
            if text.startswith("jQuery(") and text.endswith(")"):
                payload = json.loads(text[7:-1])
            else:
                return []
            rows = payload.get("result", {}).get("cmsArticleWebOld", [])
        except Exception as exc:
            logger.debug("eastmoney stock news failed for %s: %s", stock_name, exc)
            return []

        items: list[NewsItem] = []
        for row in rows:
            external_id = str(row.get("code") or "")
            title = _clean_text(row.get("title"))
            if not external_id or not title:
                continue
            content = _clean_text(row.get("content"))
            publish_time = _parse_time(row.get("date"))
            if since and publish_time < since:
                continue
            items.append(
                NewsItem(
                    source=self.source,
                    external_id=external_id,
                    title=title,
                    content=content,
                    summary=content[:280],
                    publish_time=publish_time,
                    symbols=[symbol],
                    importance=_importance_from_text(title, content),
                    url=row.get("url") or f"https://finance.eastmoney.com/a/{external_id}.html",
                    source_name="东方财富资讯",
                    language="zh",
                    payload=row,
                )
            )
        return items


class EastMoneyNewsCollector(BaseNewsCollector):
    source = "eastmoney"
    api_url = "https://np-anotice-stock.eastmoney.com/api/security/ann"

    async def fetch_news(self, symbols: list[str] | None = None, since: datetime | None = None) -> list[NewsItem]:
        if not symbols:
            return []
        a_share_symbols = [s for s in symbols if len(s) == 6 and s.isdigit()]
        if not a_share_symbols:
            return []

        cache_key = f"eastmoney_ann:{','.join(sorted(a_share_symbols))}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return [item for item in cached if not since or item.publish_time >= since]

        params = {
            "sr": -1,
            "page_size": 50,
            "page_index": 1,
            "ann_type": "A",
            "stock_list": ",".join(a_share_symbols),
            "f_node": 0,
            "s_node": 0,
        }
        try:
            async with httpx.AsyncClient(timeout=8, verify=False) as client:
                resp = await client.get(self.api_url, params=params)
                resp.raise_for_status()
                rows = resp.json().get("data", {}).get("list", [])
        except Exception as exc:
            logger.debug("eastmoney announcements failed: %s", exc)
            return []

        items: list[NewsItem] = []
        for row in rows:
            external_id = str(row.get("art_code") or "")
            title = _clean_text(row.get("title"))
            if not external_id or not title:
                continue
            publish_time = _parse_time(row.get("notice_date"))
            related_symbols = [c.get("stock_code", "") for c in row.get("codes", []) if c.get("stock_code")]
            related_symbols = [s for s in related_symbols if s]
            if since and publish_time < since:
                continue
            items.append(
                NewsItem(
                    source=self.source,
                    external_id=external_id,
                    title=title,
                    content="",
                    summary="",
                    publish_time=publish_time,
                    symbols=related_symbols or a_share_symbols[:1],
                    importance=_importance_from_text(title),
                    url=f"https://data.eastmoney.com/notices/detail/{(related_symbols or a_share_symbols[:1])[0]}/{external_id}.html",
                    source_name="东方财富公告",
                    language="zh",
                    payload=row,
                )
            )

        _set_cached(cache_key, items)
        return [item for item in items if not since or item.publish_time >= since]


class RssFeedNewsCollector(BaseNewsCollector):
    source = "rss_feed"

    def __init__(
        self,
        feed_url: str,
        language: str = "zh",
        source_name: str = "",
        timeout_s: float = 12.0,
        fetch_limit: int = 40,
    ):
        self.feed_url = feed_url
        self.language = language or "zh"
        self.source_name = source_name or feed_url
        self.timeout_s = timeout_s
        self.fetch_limit = fetch_limit
        self.source = f"rss_feed::{self.source_name}"

    async def fetch_news(self, symbols: list[str] | None = None, since: datetime | None = None) -> list[NewsItem]:
        cache_key = f"rss:{self.feed_url}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return [item for item in cached if not since or item.publish_time >= since]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True, headers=headers) as client:
                resp = await client.get(self.feed_url)
                resp.raise_for_status()
                xml_text = resp.text.lstrip("\ufeff").strip()
        except Exception as exc:
            logger.warning("rss feed fetch failed %s: %s", self.feed_url, exc)
            return []

        try:
            root = ET.fromstring(xml_text)
        except Exception as exc:
            logger.warning("rss xml parse failed %s: %s", self.feed_url, exc)
            return []

        rows = root.findall(".//item")
        if not rows:
            rows = root.findall(".//{*}entry")

        items: list[NewsItem] = []
        for row in rows[: self.fetch_limit]:
            item = self._parse_row(row)
            if not item:
                continue
            if since and item.publish_time < since:
                continue
            items.append(item)

        _set_cached(cache_key, items)
        return items

    def _child_text(self, node: ET.Element, *names: str) -> str:
        for name in names:
            for child in node.iter():
                tag = child.tag.split("}", 1)[-1]
                if tag == name and (child.text or "").strip():
                    return child.text or ""
        return ""

    def _link(self, node: ET.Element) -> str:
        for child in node.iter():
            tag = child.tag.split("}", 1)[-1]
            if tag != "link":
                continue
            href = child.attrib.get("href")
            if href:
                return href
            if (child.text or "").strip():
                return child.text or ""
        return ""

    def _parse_row(self, row: ET.Element) -> NewsItem | None:
        title = _clean_text(self._child_text(row, "title"))
        link = self._link(row)
        summary = _clean_text(self._child_text(row, "description", "summary", "encoded", "content"))
        content = _clean_text(self._child_text(row, "encoded", "content", "description", "summary"))
        external_id = _clean_text(self._child_text(row, "guid", "id")) or _hash_text(link, title)
        published = _parse_time(
            self._child_text(row, "pubDate", "published", "updated", "dc:date", "date")
        )
        if not title:
            return None

        return NewsItem(
            source=self.source,
            external_id=external_id,
            title=title,
            content=content or summary,
            summary=summary or content[:320],
            publish_time=published,
            importance=_importance_from_text(title, summary or content),
            url=link,
            source_name=self.source_name,
            language=self.language,
            payload={"feed_url": self.feed_url},
        )


class TushareNewsCollector(BaseNewsCollector):
    source = "tushare"

    def __init__(
        self,
        token: str = "",
        endpoint: str = "news",
        src: str = "",
        source_name: str = "Tushare",
        timeout_s: float = 15.0,
        fetch_limit: int = 40,
    ):
        settings = Settings()
        self.token = token or settings.tushare_token
        self.base_url = settings.tushare_base_url
        self.endpoint = endpoint or "news"
        self.src = src
        self.source_name = source_name
        self.timeout_s = timeout_s
        self.fetch_limit = fetch_limit
        self.source = f"tushare::{self.endpoint}:{self.src or 'all'}"

    async def fetch_news(self, symbols: list[str] | None = None, since: datetime | None = None) -> list[NewsItem]:
        if not self.token:
            logger.info("skip tushare news fetch because token is empty")
            return []

        cache_key = f"tushare:{self.endpoint}:{self.src}:{since.strftime('%Y%m%d%H') if since else ''}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached

        rows = await asyncio.to_thread(self._fetch_rows, since)
        items: list[NewsItem] = []
        for row in rows:
            title = _clean_text(row.get("title") or row.get("headline"))
            if not title:
                continue
            summary = _clean_text(row.get("summary") or row.get("content") or row.get("brief"))
            content = _clean_text(row.get("content") or row.get("summary") or row.get("brief"))
            url = row.get("url") or row.get("website") or ""
            external_id = str(
                row.get("id")
                or row.get("news_id")
                or row.get("uuid")
                or row.get("title_id")
                or _hash_text(url, title)
            )
            publish_time = _parse_time(
                row.get("datetime")
                or row.get("pub_time")
                or row.get("pubdate")
                or row.get("pub_date")
                or row.get("date")
            )
            item = NewsItem(
                source=self.source,
                external_id=external_id,
                title=title,
                content=content,
                summary=summary or content[:320],
                publish_time=publish_time,
                importance=_importance_from_text(title, content),
                url=url,
                source_name=self.source_name,
                language="zh",
                payload=row,
            )
            if since and item.publish_time < since:
                continue
            items.append(item)

        items.sort(key=lambda item: item.publish_time, reverse=True)
        items = items[: self.fetch_limit]
        _set_cached(cache_key, items)
        return items

    def _fetch_rows(self, since: datetime | None) -> list[dict]:
        if ts is None:
            logger.warning("tushare sdk is not installed")
            return []
        try:
            if self.base_url and hasattr(ts, "set_token"):
                try:
                    ts.set_token(self.token)
                except Exception:
                    pass
            pro = ts.pro_api(self.token)
            start = (since or (datetime.now() - timedelta(days=2))).strftime("%Y-%m-%d %H:%M:%S")
            end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            method = getattr(pro, self.endpoint)
            kwargs: dict = {"start_date": start, "end_date": end}
            if self.src:
                kwargs["src"] = self.src
            frame = method(**kwargs)
        except TypeError:
            try:
                pro = ts.pro_api(self.token)
                frame = getattr(pro, self.endpoint)(src=self.src or None)
            except Exception as exc:
                logger.warning("tushare %s fetch failed: %s", self.endpoint, exc)
                return []
        except Exception as exc:
            logger.warning("tushare %s fetch failed: %s", self.endpoint, exc)
            return []

        if frame is None or getattr(frame, "empty", True):
            return []

        try:
            rows = frame.to_dict("records")
        except Exception:
            return []
        return rows[: self.fetch_limit]


class NewsCollector:
    """Aggregate multiple news sources."""

    COLLECTOR_MAP = {
        "xueqiu": lambda config, name: XueqiuNewsCollector(cookies=config.get("cookies", "")),
        "eastmoney_news": lambda config, name: EastMoneyStockNewsCollector(
            symbol_names=config.get("symbol_names")
        ),
        "eastmoney": lambda config, name: EastMoneyNewsCollector(),
        "rss_feed": lambda config, name: RssFeedNewsCollector(
            feed_url=config.get("feed_url", ""),
            language=config.get("language", "zh"),
            source_name=name or config.get("source_name") or config.get("feed_url", ""),
            timeout_s=float(config.get("timeout_s", 12.0) or 12.0),
            fetch_limit=int(config.get("fetch_limit", 40) or 40),
        ),
        "tushare": lambda config, name: TushareNewsCollector(
            token=config.get("token", ""),
            endpoint=config.get("endpoint", "news"),
            src=config.get("src", ""),
            source_name=name or "Tushare",
            timeout_s=float(config.get("timeout_s", 15.0) or 15.0),
            fetch_limit=int(config.get("fetch_limit", 40) or 40),
        ),
    }

    def __init__(self, collectors: list[BaseNewsCollector] | None = None):
        self.collectors = collectors or []

    @classmethod
    def from_database(cls, provider_allowlist: set[str] | None = None) -> "NewsCollector":
        from src.web.database import SessionLocal
        from src.web.models import DataSource

        db = SessionLocal()
        collectors: list[BaseNewsCollector] = []
        try:
            query = (
                db.query(DataSource)
                .filter(DataSource.type == "news", DataSource.enabled == True)
                .order_by(DataSource.priority.asc(), DataSource.id.asc())
            )
            for ds in query.all():
                if provider_allowlist and ds.provider not in provider_allowlist:
                    continue
                factory = cls.COLLECTOR_MAP.get(ds.provider)
                if not factory:
                    continue
                try:
                    collector = factory(_with_env_defaults(ds.provider, ds.config), ds.name)
                    if ds.provider == "rss_feed" and not getattr(collector, "feed_url", ""):
                        continue
                    collectors.append(collector)
                except Exception as exc:
                    logger.warning("failed to build news collector %s: %s", ds.provider, exc)
        finally:
            db.close()
        return cls(collectors=collectors)

    async def fetch_all(
        self,
        symbols: list[str] | None = None,
        since_hours: int = 24,
        symbol_names: dict[str, str] | None = None,
    ) -> list[NewsItem]:
        if not self.collectors:
            return []

        since = datetime.now() - timedelta(hours=since_hours)

        async def _fetch_one(collector: BaseNewsCollector) -> list[NewsItem]:
            try:
                return await collector.fetch_news(symbols=symbols, since=since)
            except Exception as exc:
                logger.error("news collector failed %s: %s", getattr(collector, "source", "?"), exc)
                return []

        results = await asyncio.gather(*[_fetch_one(collector) for collector in self.collectors])
        items: list[NewsItem] = []
        seen: set[tuple[str, str]] = set()
        for group in results:
            for item in group:
                self._annotate_item(item, symbol_names)
                key = (item.source, item.external_id or _hash_text(item.url, item.title))
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)

        items.sort(key=lambda item: (item.relevance_score, item.importance, item.publish_time), reverse=True)
        return items

    def _annotate_item(self, item: NewsItem, symbol_names: dict[str, str] | None) -> None:
        text = " ".join([item.title or "", item.summary or "", item.content or ""])
        matched = _match_symbols(text, symbol_names)
        if matched:
            merged = list(dict.fromkeys([*item.symbols, *matched]))
            item.symbols = merged
            item.relevance_score = max(item.relevance_score, 2.0 + len(merged))
        else:
            item.relevance_score = max(item.relevance_score, float(item.importance))
