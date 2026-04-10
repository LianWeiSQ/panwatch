from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.discovery_service import get_hot_boards, get_hot_stocks
from src.core.market_data import market_data
from src.core.portfolio_service import collect_quote_items_from_accounts, list_enabled_accounts, list_watchlist_stocks
from src.web.database import SessionLocal

logger = logging.getLogger(__name__)


class MarketWarmScheduler:
    def __init__(
        self,
        timezone: str = "UTC",
        quote_interval_seconds: int = 45,
        discovery_interval_seconds: int = 300,
    ):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.quote_interval_seconds = max(15, int(quote_interval_seconds))
        self.discovery_interval_seconds = max(60, int(discovery_interval_seconds))
        self._warming_quotes = False
        self._warming_discovery = False

    async def _warm_quotes_job(self):
        if self._warming_quotes:
            return
        self._warming_quotes = True
        try:
            db = SessionLocal()
            try:
                watchlist = list_watchlist_stocks(db)
                accounts = list_enabled_accounts(db)
                quote_items = collect_quote_items_from_accounts(accounts) + [
                    {"symbol": stock.symbol, "market": stock.market}
                    for stock in watchlist
                ]
            finally:
                db.close()
            if quote_items:
                await asyncio.to_thread(market_data.get_quotes_batch, quote_items)
                await asyncio.to_thread(market_data.get_kline_summary_batch, quote_items[:80])
            await asyncio.to_thread(market_data.get_market_indices)
        except Exception:
            logger.exception("market warm quotes job failed")
        finally:
            self._warming_quotes = False

    async def _warm_discovery_job(self):
        if self._warming_discovery:
            return
        self._warming_discovery = True
        db = SessionLocal()
        try:
            for market in ("CN",):
                await asyncio.to_thread(get_hot_stocks, db, market, "turnover", 20)
                await asyncio.to_thread(get_hot_stocks, db, market, "gainers", 20)
                await asyncio.to_thread(get_hot_boards, db, market, "gainers", 12)
        except Exception:
            logger.exception("market warm discovery job failed")
        finally:
            db.close()
            self._warming_discovery = False

    def start(self):
        self.scheduler.add_job(
            self._warm_quotes_job,
            "interval",
            seconds=self.quote_interval_seconds,
            id="market_warm_quotes",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._warm_discovery_job,
            "interval",
            seconds=self.discovery_interval_seconds,
            id="market_warm_discovery",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        logger.info(
            "market warm scheduler started quote_interval=%ss discovery_interval=%ss",
            self.quote_interval_seconds,
            self.discovery_interval_seconds,
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("market warm scheduler stopped")
