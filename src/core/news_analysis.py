from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.collectors.news_collector import NewsCollector, NewsItem
from src.core.context_store import get_latest_news_topic_snapshot, save_news_topic_snapshot
from src.core.json_safe import to_jsonable
from src.core.news_ranker import summarize_news_topics
from src.web.models import AnalysisHistory, DataSource, NewsArticle, NewsSourceStatus, Position, Stock

logger = logging.getLogger(__name__)

SUPPORTED_NEWS_PROVIDERS = {"rss_feed", "tushare"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _app_timezone() -> str:
    try:
        from src.config import Settings

        return Settings().app_timezone or "Asia/Shanghai"
    except Exception:
        return "Asia/Shanghai"


def _format_datetime(dt: datetime | None) -> str:
    if not dt:
        return ""
    try:
        tzinfo = ZoneInfo(_app_timezone())
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


def hashlib_sha(text: str) -> str:
    import hashlib

    return hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()


def _normalize_language(value: str | None) -> str:
    raw = str(value or "zh").strip().lower()
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("en"):
        return "en"
    return raw or "zh"


def _provider_key(source: DataSource) -> str:
    return f"{source.provider}:{source.id}"


def _load_watch_profile(db: Session) -> dict:
    watchlist = db.query(Stock).order_by(Stock.sort_order.asc(), Stock.id.asc()).all()
    positions = db.query(Position, Stock).join(Stock, Position.stock_id == Stock.id).all()

    symbol_names: dict[str, str] = {}
    watch_symbols: set[str] = set()
    holding_symbols: set[str] = set()

    for stock in watchlist:
        symbol_names[stock.symbol] = stock.name
        watch_symbols.add(stock.symbol)

    for _position, stock in positions:
        symbol_names[stock.symbol] = stock.name
        holding_symbols.add(stock.symbol)

    keywords = set(symbol_names.keys())
    keywords.update(name for name in symbol_names.values() if name)
    keyword_index = {str(keyword).lower(): symbol for symbol, keyword in symbol_names.items() if keyword}
    keyword_index.update({symbol.lower(): symbol for symbol in symbol_names})

    return {
        "symbol_names": symbol_names,
        "watch_symbols": watch_symbols,
        "holding_symbols": holding_symbols,
        "keywords": keywords,
        "keyword_index": keyword_index,
    }


def _match_symbols(item: NewsItem, profile: dict) -> list[str]:
    text = " ".join([item.title or "", item.summary or "", item.content or ""]).lower()
    matched = list(item.symbols or [])
    for keyword, symbol in (profile.get("keyword_index") or {}).items():
        if keyword and keyword in text and symbol not in matched:
            matched.append(symbol)
    return matched


def _score_item(item: NewsItem, profile: dict, watchlist_boost: float) -> float:
    score = float(item.importance or 0)
    matched = set(item.symbols or [])
    holdings = set(profile.get("holding_symbols") or set())
    watchlist = set(profile.get("watch_symbols") or set())

    if matched & holdings:
        score += 3.0 * watchlist_boost
    elif matched & watchlist:
        score += 2.0 * watchlist_boost
    elif matched:
        score += 1.0

    text = " ".join([item.title or "", item.summary or "", item.content or ""]).lower()
    if any(str(keyword).lower() in text for keyword in profile.get("keywords") or set()):
        score += 1.0
    if _normalize_language(item.language) == "en":
        score += 0.2
    return score


def _normalize_topics(raw_topics: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw_topics, list):
        return out
    for item in raw_topics:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("topic") or "").strip()
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "score": float(item.get("score") or 0.0),
                    "sentiment": str(item.get("sentiment") or "neutral"),
                }
            )
        else:
            name = str(item or "").strip()
            if not name:
                continue
            out.append({"name": name, "score": 0.0, "sentiment": "neutral"})
        if len(out) >= 8:
            break
    return out


def _find_existing_article(
    db: Session,
    *,
    provider: str,
    external_id: str,
    url_hash: str,
    title_hash: str,
) -> NewsArticle | None:
    record = None
    if external_id:
        record = (
            db.query(NewsArticle)
            .filter(NewsArticle.provider == provider, NewsArticle.external_id == external_id)
            .first()
        )
    if not record and url_hash and title_hash:
        record = (
            db.query(NewsArticle)
            .filter(
                NewsArticle.provider == provider,
                NewsArticle.url_hash == url_hash,
                NewsArticle.title_hash == title_hash,
            )
            .first()
        )
    return record


def _upsert_article(db: Session, item: NewsItem) -> None:
    provider = str(item.source or "")
    external_id = str(item.external_id or "")
    payload = dict(item.payload or {})
    url_hash = str(payload.get("url_hash") or hashlib_sha(item.url or ""))
    title_hash = str(payload.get("title_hash") or hashlib_sha(item.title or ""))
    payload["url_hash"] = url_hash
    payload["title_hash"] = title_hash

    record = _find_existing_article(
        db,
        provider=provider,
        external_id=external_id,
        url_hash=url_hash,
        title_hash=title_hash,
    )

    if not record:
        record = NewsArticle(
            provider=provider,
            source_name=item.source_name or provider,
            external_id=external_id,
            language=_normalize_language(item.language),
            title=item.title or "",
            summary=item.summary or "",
            content=item.content or "",
            cn_summary=item.cn_summary or "",
            url=item.url or "",
            url_hash=url_hash,
            title_hash=title_hash,
            published_at=item.publish_time or _utcnow(),
            fetched_at=_utcnow(),
            symbols=item.symbols or [],
            relevance_score=float(item.relevance_score or 0.0),
            payload=to_jsonable(payload),
        )
        db.add(record)
        return

    record.source_name = item.source_name or record.source_name
    record.language = _normalize_language(item.language)
    record.title = item.title or record.title
    record.summary = item.summary or ""
    record.content = item.content or ""
    record.cn_summary = item.cn_summary or ""
    record.url = item.url or ""
    record.url_hash = url_hash
    record.title_hash = title_hash
    record.published_at = item.publish_time or record.published_at
    record.fetched_at = _utcnow()
    record.symbols = item.symbols or []
    record.relevance_score = float(item.relevance_score or 0.0)
    record.payload = to_jsonable(payload)


def _upsert_source_status(
    db: Session,
    *,
    source: DataSource,
    status: str,
    article_count: int = 0,
    error: str = "",
    meta: dict | None = None,
) -> None:
    provider_key = _provider_key(source)
    row = db.query(NewsSourceStatus).filter(NewsSourceStatus.provider == provider_key).first()
    now = _utcnow()
    if not row:
        row = NewsSourceStatus(provider=provider_key)
        db.add(row)

    row.source_name = source.name
    row.enabled = bool(source.enabled)
    row.status = status
    row.last_attempt_at = now
    row.article_count = int(article_count or 0)
    row.last_error = str(error or "")[:1000]
    row.meta = to_jsonable(
        {
            "provider": source.provider,
            "source_id": source.id,
            **(meta or {}),
        }
    )
    if status == "success":
        row.last_success_at = now


async def _translate_english_summary(item: NewsItem, ai_client) -> str:
    fallback = item.summary or item.content[:220] or item.title
    if not ai_client:
        return fallback

    system_prompt = (
        "你是财经新闻编辑。"
        "请把英文新闻压缩成简洁中文摘要，控制在80字以内，只输出摘要正文。"
    )
    user_prompt = "\n".join(
        [
            f"Title: {item.title}",
            f"Summary: {item.summary or item.content[:500]}",
        ]
    )
    try:
        translated = await ai_client.chat(system_prompt, user_prompt, temperature=0.2)
        return str(translated or "").strip() or fallback
    except Exception as exc:
        logger.warning("translate english summary failed for %s: %s", item.title, exc)
        return fallback


def dedupe_items(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[tuple[str, str, str]] = set()
    out: list[NewsItem] = []
    for item in items:
        payload = item.payload or {}
        external = str(item.external_id or payload.get("url_hash") or "")
        title_hash = str(payload.get("title_hash") or hashlib_sha(item.title or ""))
        key = (str(item.source or ""), external, title_hash)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def article_to_dict(row: NewsArticle) -> dict:
    payload = row.payload or {}
    return {
        "id": row.id,
        "provider": row.provider,
        "provider_type": str(payload.get("provider") or row.provider).strip(),
        "source_name": row.source_name,
        "language": row.language,
        "title": row.title,
        "summary": row.summary or "",
        "cn_summary": row.cn_summary or "",
        "content": row.content or "",
        "url": row.url or "",
        "published_at": _format_datetime(row.published_at),
        "fetched_at": _format_datetime(row.fetched_at),
        "symbols": row.symbols or [],
        "relevance_score": float(row.relevance_score or 0.0),
        "importance": int(payload.get("importance") or 0),
        "payload": payload,
    }


class NewsAnalysisService:
    def __init__(self, db: Session):
        self.db = db

    def _list_sources(self) -> list[DataSource]:
        return (
            self.db.query(DataSource)
            .filter(
                DataSource.type == "news",
                DataSource.provider.in_(tuple(SUPPORTED_NEWS_PROVIDERS)),
            )
            .order_by(DataSource.priority.asc(), DataSource.id.asc())
            .all()
        )

    def list_source_statuses(self) -> list[dict]:
        sources = self._list_sources()
        rows = {
            row.provider: row
            for row in self.db.query(NewsSourceStatus)
            .filter(NewsSourceStatus.provider.like("rss_feed:%") | NewsSourceStatus.provider.like("tushare:%"))
            .all()
        }

        result: list[dict] = []
        for source in sources:
            provider_key = _provider_key(source)
            row = rows.get(provider_key)
            result.append(
                {
                    "provider": provider_key,
                    "provider_type": source.provider,
                    "source_id": source.id,
                    "source_name": source.name,
                    "enabled": bool(source.enabled),
                    "status": (
                        row.status
                        if row
                        else ("disabled" if not source.enabled else "idle")
                    ),
                    "last_success_at": _format_datetime(row.last_success_at) if row else "",
                    "last_attempt_at": _format_datetime(row.last_attempt_at) if row else "",
                    "last_error": row.last_error or "" if row else "",
                    "article_count": int(row.article_count or 0) if row else 0,
                    "meta": (
                        row.meta
                        if row
                        else {
                            "provider": source.provider,
                            "source_id": source.id,
                        }
                    )
                    or {},
                }
            )
        return result

    async def collect_and_persist(
        self,
        *,
        lookback_hours: int = 24,
        max_items_per_source: int = 40,
        top_n_for_ai: int = 24,
        translate_english: bool = True,
        watchlist_boost: float = 1.5,
        ai_client=None,
    ) -> dict:
        profile = _load_watch_profile(self.db)
        since = _utcnow() - timedelta(hours=max(1, int(lookback_hours or 24)))
        enabled_sources = [source for source in self._list_sources() if source.enabled]
        all_items: list[NewsItem] = []

        for source in enabled_sources:
            factory = NewsCollector.COLLECTOR_MAP.get(source.provider)
            if not factory:
                _upsert_source_status(
                    self.db,
                    source=source,
                    status="error",
                    error=f"unsupported provider: {source.provider}",
                )
                continue

            try:
                collector = factory(source.config or {}, source.name)
                rows = await collector.fetch_news(symbols=None, since=since)
                rows = rows[: max(1, int(max_items_per_source or 40))]

                for item in rows:
                    item.source = _provider_key(source)
                    item.source_name = item.source_name or source.name
                    item.language = _normalize_language(
                        item.language or (source.config or {}).get("language")
                    )
                    item.payload = dict(item.payload or {})
                    item.payload.update(
                        {
                            "provider": source.provider,
                            "source_id": source.id,
                            "importance": int(item.importance or 0),
                        }
                    )
                    item.payload["url_hash"] = hashlib_sha(item.url or "")
                    item.payload["title_hash"] = hashlib_sha(item.title or "")
                    item.symbols = list(dict.fromkeys(_match_symbols(item, profile)))
                    item.relevance_score = _score_item(item, profile, watchlist_boost)
                    item.cn_summary = item.summary or item.content[:220] or item.title
                    all_items.append(item)

                _upsert_source_status(
                    self.db,
                    source=source,
                    status="success",
                    article_count=len(rows),
                )
            except Exception as exc:
                logger.warning("news source refresh failed for %s: %s", source.name, exc)
                _upsert_source_status(
                    self.db,
                    source=source,
                    status="error",
                    error=str(exc),
                )

        items = dedupe_items(all_items)
        items.sort(
            key=lambda item: (
                float(item.relevance_score or 0.0),
                int(item.importance or 0),
                item.publish_time or datetime.min,
            ),
            reverse=True,
        )

        if translate_english:
            translate_limit = max(1, int(top_n_for_ai or 24))
            english_items = [item for item in items if _normalize_language(item.language) == "en"][:translate_limit]
            for item in english_items:
                item.cn_summary = await _translate_english_summary(item, ai_client)

        for item in items:
            _upsert_article(self.db, item)

        topics_input = [
            {
                "title": item.title,
                "content": item.cn_summary or item.summary or item.content or item.title,
                "importance": item.importance,
                "time": item.publish_time.isoformat() if item.publish_time else "",
            }
            for item in items[:120]
        ]
        topic_result = summarize_news_topics(topics_input)
        topics = _normalize_topics(topic_result.get("topics"))
        related_news = [
            item
            for item in items
            if set(item.symbols or []) & (profile["watch_symbols"] | profile["holding_symbols"])
        ]
        coverage = {
            "total_articles": len(items),
            "related_articles": len(related_news),
            "configured_sources": len(self._list_sources()),
            "enabled_sources": len(enabled_sources),
            "languages": sorted({_normalize_language(item.language) for item in items if item.language}),
        }

        self.db.commit()
        save_news_topic_snapshot(
            snapshot_date=_utcnow().strftime("%Y-%m-%d"),
            window_days=max(1, ceil(max(1, int(lookback_hours or 24)) / 24)),
            symbols=sorted(profile["watch_symbols"] | profile["holding_symbols"]),
            summary=str(topic_result.get("summary") or ""),
            topics=[topic["name"] for topic in topics],
            sentiment=str(topic_result.get("sentiment") or "neutral"),
            coverage=coverage,
        )

        return {
            "news": items,
            "related_news": related_news[:30],
            "important_news": [item for item in items if int(item.importance or 0) >= 2][:30],
            "topics": topics,
            "summary": str(topic_result.get("summary") or ""),
            "sentiment": str(topic_result.get("sentiment") or "neutral"),
            "coverage": coverage,
            "source_statuses": self.list_source_statuses(),
            "timestamp": _format_datetime(_utcnow()),
            "watch_profile": profile,
        }

    def build_runtime(
        self,
        *,
        hours: int = 72,
        related_only: bool = False,
        language: str = "",
        source: str = "",
        limit: int = 120,
    ) -> dict:
        profile = _load_watch_profile(self.db)
        since = _utcnow() - timedelta(hours=max(1, int(hours or 72)))
        rows = (
            self.db.query(NewsArticle)
            .filter(NewsArticle.published_at >= since)
            .order_by(NewsArticle.relevance_score.desc(), NewsArticle.published_at.desc(), NewsArticle.id.desc())
            .limit(max(200, int(limit or 120) * 3))
            .all()
        )

        language_filter = _normalize_language(language) if language else ""
        source_filters = {part.strip() for part in str(source or "").split(",") if part.strip()}
        watch_symbols = profile["watch_symbols"] | profile["holding_symbols"]

        articles: list[dict] = []
        related_articles: list[dict] = []
        for row in rows:
            row_language = _normalize_language(row.language)
            payload = row.payload or {}
            provider_type = str(payload.get("provider") or row.provider)
            if language_filter and row_language != language_filter:
                continue
            if source_filters:
                match_values = {row.provider, provider_type, row.source_name}
                if not match_values & source_filters:
                    continue

            item = article_to_dict(row)
            is_related = bool(set(row.symbols or []) & watch_symbols)
            item["is_related"] = is_related
            articles.append(item)
            if is_related:
                related_articles.append(item)
            if len(articles) >= max(1, int(limit or 120)) and len(related_articles) >= max(1, min(int(limit or 120), 30)):
                continue

        if related_only:
            articles = related_articles[: max(1, int(limit or 120))]
        else:
            articles = articles[: max(1, int(limit or 120))]
            related_articles = related_articles[: max(1, min(int(limit or 120), 30))]

        latest_analysis = (
            self.db.query(AnalysisHistory)
            .filter(AnalysisHistory.agent_name == "news_digest")
            .order_by(AnalysisHistory.analysis_date.desc(), AnalysisHistory.updated_at.desc(), AnalysisHistory.id.desc())
            .first()
        )
        latest_topic = get_latest_news_topic_snapshot(window_days=max(1, ceil(max(1, int(hours or 72)) / 24)))
        history_payload = (
            latest_analysis.raw_data
            if latest_analysis and isinstance(latest_analysis.raw_data, dict)
            else {}
        )
        latest_article = (
            self.db.query(NewsArticle)
            .order_by(NewsArticle.fetched_at.desc(), NewsArticle.id.desc())
            .first()
        )

        topics = history_payload.get("topics") or _normalize_topics(getattr(latest_topic, "topics", []))
        if topics and isinstance(topics[0], str):
            topics = _normalize_topics(topics)

        summary = (
            history_payload.get("topic_summary")
            or history_payload.get("summary")
            or getattr(latest_topic, "summary", "")
            or ""
        )
        sentiment = (
            history_payload.get("sentiment")
            or getattr(latest_topic, "sentiment", "neutral")
            or "neutral"
        )
        coverage = history_payload.get("coverage") or getattr(latest_topic, "coverage", None) or {
            "total_articles": len(articles),
            "related_articles": len(related_articles),
            "configured_sources": len(self._list_sources()),
        }

        latest_snapshot_at = ""
        if latest_analysis and latest_analysis.updated_at:
            latest_snapshot_at = _format_datetime(latest_analysis.updated_at)
        elif latest_topic and latest_topic.created_at:
            latest_snapshot_at = _format_datetime(latest_topic.created_at)
        elif latest_article and latest_article.fetched_at:
            latest_snapshot_at = _format_datetime(latest_article.fetched_at)

        return {
            "generated_at": _format_datetime(_utcnow()),
            "hours": int(hours or 72),
            "summary": summary,
            "topics": topics,
            "sentiment": sentiment,
            "coverage": coverage,
            "analysis": {
                "id": latest_analysis.id if latest_analysis else None,
                "title": latest_analysis.title if latest_analysis else "",
                "content": latest_analysis.content if latest_analysis else "",
                "analysis_date": latest_analysis.analysis_date if latest_analysis else "",
                "updated_at": _format_datetime(latest_analysis.updated_at) if latest_analysis else "",
            },
            "articles": articles,
            "related_articles": related_articles,
            "source_statuses": self.list_source_statuses(),
            "watchlist": [
                {"symbol": symbol, "name": name}
                for symbol, name in sorted(profile["symbol_names"].items(), key=lambda item: item[0])
            ],
            "latest_article_at": _format_datetime(latest_article.fetched_at) if latest_article else "",
            "latest_snapshot_at": latest_snapshot_at,
        }
