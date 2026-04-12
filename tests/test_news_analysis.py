import asyncio
import sys
import types
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


if "tushare" not in sys.modules:
    sys.modules["tushare"] = types.SimpleNamespace(pro_api=lambda _token: None)

from src.collectors.news_collector import NewsItem, RssFeedNewsCollector
from src.core import news_analysis as news_analysis_module
from src.core.data_collector import DataCollectorManager
from src.core.news_analysis import NewsAnalysisService, _score_item, _translate_english_summary, dedupe_items
from src.web.database import Base
from src.web.models import DataSource, Stock


def test_rss_feed_parses_single_line_xml_and_cleans_html(monkeypatch):
    xml_text = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss><channel><item>'
        '<title>FT &amp; Markets</title>'
        '<link>https://example.com/story</link>'
        '<description><![CDATA[<p>Hello <b>world</b></p>]]></description>'
        '<pubDate>Sat, 12 Apr 2025 10:30:00 GMT</pubDate>'
        '<guid>story-1</guid>'
        '</item></channel></rss>'
    )

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, _url):
            return FakeResponse(xml_text)

    monkeypatch.setattr("src.collectors.news_collector.httpx.AsyncClient", FakeAsyncClient)

    collector = RssFeedNewsCollector(
        feed_url="https://example.com/rss",
        language="en",
        source_name="FT",
        fetch_limit=10,
    )
    items = asyncio.run(collector.fetch_news())

    assert len(items) == 1
    assert items[0].title == "FT & Markets"
    assert items[0].summary == "Hello world"
    assert items[0].url == "https://example.com/story"
    assert items[0].language == "en"


def test_rss_feed_missing_pub_date_falls_back_to_now():
    collector = RssFeedNewsCollector(feed_url="https://example.com/rss", source_name="FT")
    row = ET.fromstring(
        "<item><title>Missing Date</title><link>https://example.com/a</link>"
        "<description>hello</description></item>"
    )
    before = datetime.now() - timedelta(seconds=5)
    item = collector._parse_row(row)

    assert item is not None
    assert item.publish_time >= before
    assert item.title == "Missing Date"


def test_dedupe_items_uses_hash_fallback():
    payload = {"url_hash": "same-url", "title_hash": "same-title"}
    items = [
        NewsItem(
            source="rss_feed:1",
            external_id="",
            title="A",
            content="",
            summary="",
            publish_time=datetime.now(),
            payload=payload.copy(),
        ),
        NewsItem(
            source="rss_feed:1",
            external_id="",
            title="A",
            content="",
            summary="",
            publish_time=datetime.now(),
            payload=payload.copy(),
        ),
    ]

    deduped = dedupe_items(items)
    assert len(deduped) == 1


def test_translate_english_summary_branch():
    item = NewsItem(
        source="rss_feed:1",
        external_id="1",
        title="Oil rises on supply worries",
        content="Oil climbed after supply risks increased.",
        summary="Oil climbed after supply risks increased.",
        publish_time=datetime.now(),
        language="en",
    )

    class FakeAI:
        async def chat(self, *_args, **_kwargs):
            return "油价因供应担忧走强。"

    translated = asyncio.run(_translate_english_summary(item, FakeAI()))
    fallback = asyncio.run(_translate_english_summary(item, None))

    assert translated == "油价因供应担忧走强。"
    assert "Oil climbed" in fallback


def test_score_item_boosts_related_holdings():
    profile = {
        "watch_symbols": {"600519"},
        "holding_symbols": {"000001"},
        "keywords": {"贵州茅台", "平安银行"},
    }

    holding_item = NewsItem(
        source="rss_feed:1",
        external_id="1",
        title="平安银行发布新公告",
        content="",
        summary="",
        publish_time=datetime.now(),
        symbols=["000001"],
        importance=1,
        language="zh",
    )
    watch_item = NewsItem(
        source="rss_feed:1",
        external_id="2",
        title="贵州茅台渠道调研",
        content="",
        summary="",
        publish_time=datetime.now(),
        symbols=["600519"],
        importance=1,
        language="zh",
    )
    plain_item = NewsItem(
        source="rss_feed:1",
        external_id="3",
        title="宏观市场综述",
        content="",
        summary="",
        publish_time=datetime.now(),
        symbols=[],
        importance=1,
        language="zh",
    )

    assert _score_item(holding_item, profile, 1.5) > _score_item(watch_item, profile, 1.5)
    assert _score_item(watch_item, profile, 1.5) > _score_item(plain_item, profile, 1.5)


def test_collect_and_persist_fail_open_when_one_source_errors(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Stock(symbol="600519", name="贵州茅台", market="CN", sort_order=1))
    db.add_all(
        [
            DataSource(
                name="Good Feed",
                type="news",
                provider="rss_feed",
                config={"feed_url": "good", "language": "zh"},
                enabled=True,
                priority=1,
                supports_batch=True,
                test_symbols=[],
            ),
            DataSource(
                name="Bad Feed",
                type="news",
                provider="rss_feed",
                config={"feed_url": "bad", "language": "zh"},
                enabled=True,
                priority=2,
                supports_batch=True,
                test_symbols=[],
            ),
        ]
    )
    db.commit()

    class GoodCollector:
        def __init__(self, config, name):
            self.name = name
            self.config = config

        async def fetch_news(self, symbols=None, since=None):
            return [
                NewsItem(
                    source="rss_feed",
                    external_id="good-1",
                    title="贵州茅台发布新品",
                    content="新品发布，市场关注度提升",
                    summary="新品发布，市场关注度提升",
                    publish_time=datetime.now(),
                    source_name=self.name,
                    language="zh",
                )
            ]

    class BadCollector:
        def __init__(self, config, name):
            self.name = name
            self.config = config

        async def fetch_news(self, symbols=None, since=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        news_analysis_module.NewsCollector,
        "COLLECTOR_MAP",
        {
            "rss_feed": lambda config, name: GoodCollector(config, name)
            if config.get("feed_url") == "good"
            else BadCollector(config, name),
            "tushare": news_analysis_module.NewsCollector.COLLECTOR_MAP.get("tushare"),
        },
    )
    monkeypatch.setattr(news_analysis_module, "save_news_topic_snapshot", lambda **kwargs: True)

    service = NewsAnalysisService(db)
    result = asyncio.run(
        service.collect_and_persist(
            lookback_hours=24,
            max_items_per_source=10,
            top_n_for_ai=5,
            translate_english=False,
            watchlist_boost=1.5,
            ai_client=None,
        )
    )

    assert len(result["news"]) == 1
    statuses = {item["source_name"]: item for item in result["source_statuses"]}
    assert statuses["Good Feed"]["status"] == "success"
    assert statuses["Bad Feed"]["status"] == "error"


def test_build_runtime_without_analysis_history_returns_empty_state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(
        DataSource(
            name="FT",
            type="news",
            provider="rss_feed",
            config={"feed_url": "https://example.com/rss", "language": "en"},
            enabled=True,
            priority=1,
            supports_batch=True,
            test_symbols=[],
        )
    )
    db.commit()

    service = NewsAnalysisService(db)
    runtime = service.build_runtime(hours=72, related_only=False, language="", source="", limit=20)

    assert runtime["summary"] == ""
    assert runtime["topics"] == []
    assert runtime["analysis"]["id"] is None
    assert runtime["analysis"]["content"] == ""
    assert runtime["articles"] == []
    assert runtime["source_statuses"][0]["status"] in {"idle", "disabled", "success", "error"}


def test_tushare_source_test_requires_token():
    manager = DataCollectorManager()
    source = DataSource(
        name="Tushare",
        type="news",
        provider="tushare",
        config={"token": "", "endpoint": "news", "src": ""},
        enabled=False,
        priority=0,
        supports_batch=True,
        test_symbols=[],
    )

    result = asyncio.run(manager._test_source_impl(source, []))

    assert result.success is False
    assert "token" in result.error.lower()
