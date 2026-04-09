"""模拟盘引擎：自动按策略信号建仓/平仓，跟踪虚拟账户收益。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.collectors.akshare_collector import _fetch_tencent_quotes, _tencent_symbol
from src.models.market import MarketCode, MARKETS
from src.web.database import SessionLocal
from src.web.models import (
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    StrategySignalRun,
)

logger = logging.getLogger(__name__)

FIXED_QUANTITY = 100


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except Exception:
        return MarketCode.CN


def _is_trading_time(market: str) -> bool:
    mc = _to_market(market)
    market_def = MARKETS.get(mc)
    if not market_def:
        return False
    return market_def.is_trading_time()


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


class PaperTradingEngine:
    """模拟盘扫描引擎。"""

    def _get_or_create_account(self, db: Session) -> PaperTradingAccount:
        account = db.query(PaperTradingAccount).first()
        if not account:
            account = PaperTradingAccount(
                initial_capital=1000000.0,
                current_capital=1000000.0,
                peak_capital=1000000.0,
            )
            db.add(account)
            db.commit()
            db.refresh(account)
        return account

    def _fetch_quotes_map(self, symbols_markets: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """批量获取报价，返回 {(market, symbol): quote_dict}"""
        grouped: dict[MarketCode, list[str]] = {}
        for symbol, market in symbols_markets:
            mc = _to_market(market)
            grouped.setdefault(mc, []).append(symbol)

        out: dict[tuple[str, str], dict] = {}
        for market, symbols in grouped.items():
            if not symbols:
                continue
            tencent_syms = [_tencent_symbol(sym, market) for sym in symbols]
            try:
                rows = _fetch_tencent_quotes(tencent_syms)
            except Exception as e:
                logger.error(f"[模拟盘] 拉行情失败 {market.value}: {e}")
                rows = []
            by_symbol = {str(r.get("symbol")): r for r in rows}
            for sym in symbols:
                q = by_symbol.get(sym)
                if q:
                    out[(market.value, sym)] = q
        return out

    def _check_entries(
        self, db: Session, account: PaperTradingAccount,
    ) -> tuple[int, set[tuple[str, str]], list[tuple[PaperTradingPosition, StrategySignalRun | None]]]:
        """检查可入场的策略信号，自动建仓。返回 (建仓数, 新建仓股票key集合, 建仓事件列表)。"""
        # 查询最新活跃买入信号
        query = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.status == "active",
                StrategySignalRun.action.in_(["buy", "add"]),
                StrategySignalRun.entry_low.isnot(None),
                StrategySignalRun.entry_high.isnot(None),
            )
        )
        # 排除用户不关注的市场
        excluded = account.excluded_markets or []
        if excluded:
            query = query.filter(StrategySignalRun.stock_market.notin_(excluded))
        signals = query.order_by(StrategySignalRun.rank_score.desc()).limit(50).all()
        entry_events: list[tuple[PaperTradingPosition, StrategySignalRun | None]] = []
        new_keys: set[tuple[str, str]] = set()
        if not signals:
            return 0, new_keys, entry_events

        # 已有 open position 的股票
        open_keys = set()
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        for p in open_positions:
            open_keys.add((p.stock_symbol, p.stock_market))

        # 收集需要报价的信号（去重：同股票只取 rank_score 最高的一条）
        candidates = []
        seen = set()
        for sig in signals:
            key = (sig.stock_symbol, sig.stock_market)
            if key in open_keys:
                continue
            if key in seen:
                continue
            seen.add(key)
            candidates.append(sig)

        if not candidates:
            return 0, new_keys, entry_events

        # 批量获取报价
        syms = [(s.stock_symbol, s.stock_market) for s in candidates]
        quotes = self._fetch_quotes_map(syms)

        opened = 0
        for sig in candidates:
            key = (sig.stock_market, sig.stock_symbol)
            quote = quotes.get(key)
            if not quote:
                continue
            current_price = _safe_float(quote.get("current_price"))
            if current_price is None or current_price <= 0:
                continue

            # 用当前市价入场
            entry_price = current_price
            cost = entry_price * FIXED_QUANTITY
            if cost > account.current_capital:
                continue

            # 基于入场价计算止损/止盈
            # 优先用信号的止损/止盈比例，否则用默认 -8%/+15%
            stop_loss = sig.stop_loss
            target_price = sig.target_price
            if stop_loss and sig.entry_low and sig.entry_low > 0:
                # 保留信号的止损比例，映射到实际入场价
                orig_mid = (sig.entry_low + (sig.entry_high or sig.entry_low)) / 2
                if orig_mid > 0:
                    stop_ratio = (stop_loss - orig_mid) / orig_mid
                    target_ratio = ((target_price - orig_mid) / orig_mid) if target_price else 0.15
                    stop_loss = round(entry_price * (1 + stop_ratio), 4)
                    target_price = round(entry_price * (1 + target_ratio), 4) if target_price else None
            # 兜底：止损不合理时用默认 -8%
            if not stop_loss or stop_loss <= 0 or stop_loss >= entry_price:
                stop_loss = round(entry_price * 0.92, 4)
            # 兜底：止盈不合理时用默认 +15%
            if not target_price or target_price <= 0 or target_price <= entry_price:
                target_price = round(entry_price * 1.15, 4)

            pos = PaperTradingPosition(
                stock_symbol=sig.stock_symbol,
                stock_market=sig.stock_market,
                stock_name=sig.stock_name or "",
                quantity=FIXED_QUANTITY,
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_price=target_price,
                current_price=current_price,
                unrealized_pnl=0.0,
                status="open",
                signal_run_id=sig.id,
                signal_snapshot_date=sig.snapshot_date or "",
                signal_action=sig.action or "",
                strategy_code=sig.strategy_code or "",
            )
            db.add(pos)
            account.current_capital -= cost
            open_keys.add((sig.stock_symbol, sig.stock_market))
            new_keys.add((sig.stock_symbol, sig.stock_market))
            entry_events.append((pos, sig))
            opened += 1
            logger.info(
                "[模拟盘] 建仓: %s %s @ %.2f, 止损=%.2f, 止盈=%s, 策略=%s",
                sig.stock_name or sig.stock_symbol,
                sig.stock_market,
                entry_price,
                sig.stop_loss or 0,
                sig.target_price or "无",
                sig.strategy_code,
            )

        if opened > 0:
            db.commit()
        return opened, new_keys, entry_events

    def _close_position(
        self,
        db: Session,
        account: PaperTradingAccount,
        pos: PaperTradingPosition,
        exit_price: float,
        exit_reason: str,
    ) -> PaperTradingTrade:
        """平仓单个持仓，返回交易记录。"""
        now = _utc_now()
        pnl = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0.0

        holding_days = 0
        if pos.opened_at:
            opened = pos.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            holding_days = max(0, (now - opened).days)

        trade = PaperTradingTrade(
            stock_symbol=pos.stock_symbol,
            stock_market=pos.stock_market,
            stock_name=pos.stock_name or "",
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=round(pnl_pct, 2),
            exit_reason=exit_reason,
            signal_run_id=pos.signal_run_id,
            signal_snapshot_date=pos.signal_snapshot_date or "",
            strategy_code=pos.strategy_code or "",
            holding_days=holding_days,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        db.add(trade)

        pos.status = "closed"
        pos.closed_at = now
        pos.current_price = exit_price
        pos.unrealized_pnl = pnl

        # 回收资金
        account.current_capital += exit_price * pos.quantity
        account.total_pnl += pnl
        account.total_trades += 1
        if pnl > 0:
            account.winning_trades += 1

        logger.info(
            "[模拟盘] 平仓: %s %s @ %.2f, 盈亏=%.2f (%.2f%%), 原因=%s",
            pos.stock_name or pos.stock_symbol,
            pos.stock_market,
            exit_price,
            pnl,
            pnl_pct,
            exit_reason,
        )
        return trade

    def _check_exits(
        self, db: Session, account: PaperTradingAccount, skip_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[int, list[tuple[PaperTradingPosition, PaperTradingTrade]]]:
        """检查持仓止损/止盈/信号反转，自动平仓。skip_keys 中的股票跳过（本轮新建仓）。"""
        exit_events: list[tuple[PaperTradingPosition, PaperTradingTrade]] = []
        positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        if not positions:
            return 0, exit_events

        # 批量获取报价
        syms = [(p.stock_symbol, p.stock_market) for p in positions]
        quotes = self._fetch_quotes_map(syms)

        closed = 0
        for pos in positions:
            # 跳过本轮刚建仓的持仓
            if skip_keys and (pos.stock_symbol, pos.stock_market) in skip_keys:
                continue
            key = (pos.stock_market, pos.stock_symbol)
            quote = quotes.get(key)
            current_price = _safe_float(quote.get("current_price")) if quote else None

            if current_price is None or current_price <= 0:
                continue

            # 更新现价和浮动盈亏
            pos.current_price = current_price
            pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity

            # 检查止损
            if pos.stop_loss and current_price <= pos.stop_loss:
                trade = self._close_position(db, account, pos, current_price, "stop_loss")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查止盈
            if pos.target_price and current_price >= pos.target_price:
                trade = self._close_position(db, account, pos, current_price, "target_price")
                exit_events.append((pos, trade))
                closed += 1
                continue

            # 检查信号反转
            if pos.signal_run_id:
                latest = (
                    db.query(StrategySignalRun)
                    .filter(
                        StrategySignalRun.stock_symbol == pos.stock_symbol,
                        StrategySignalRun.stock_market == pos.stock_market,
                        StrategySignalRun.status == "active",
                    )
                    .order_by(StrategySignalRun.created_at.desc())
                    .first()
                )
                if latest and latest.action in ("sell", "reduce"):
                    trade = self._close_position(db, account, pos, current_price, "signal_reversal")
                    exit_events.append((pos, trade))
                    closed += 1
                    continue

        self._update_account_metrics(db, account)
        db.commit()
        return closed, exit_events

    def _update_account_metrics(self, db: Session, account: PaperTradingAccount) -> None:
        """更新账户峰值和最大回撤。"""
        # 计算包含浮动盈亏的总资产
        open_positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )
        unrealized_total = sum(p.unrealized_pnl or 0 for p in open_positions)
        total_equity = account.current_capital + sum(
            (p.current_price or p.entry_price) * p.quantity for p in open_positions
        )

        if total_equity > account.peak_capital:
            account.peak_capital = total_equity

        if account.peak_capital > 0:
            drawdown = (account.peak_capital - total_equity) / account.peak_capital * 100
            if drawdown > account.max_drawdown_pct:
                account.max_drawdown_pct = round(drawdown, 2)

    def _scan_sync(self) -> dict:
        """同步扫描（在线程中执行）。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db)
            if not account.enabled:
                return {"status": "disabled"}

            opened, new_keys, entry_events = self._check_entries(db, account)
            closed, exit_events = self._check_exits(db, account, skip_keys=new_keys)

            return {
                "status": "ok",
                "opened": opened,
                "closed": closed,
                "entry_events": entry_events,
                "exit_events": exit_events,
            }
        except Exception as e:
            logger.exception(f"[模拟盘] 扫描异常: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            db.close()

    async def scan_once(self) -> dict:
        """异步扫描入口。"""
        result = await asyncio.to_thread(self._scan_sync)
        # 发送通知（异步，失败不影响交易）
        await self._send_notifications(result)
        return result

    def close_position_manual(self, position_id: int) -> dict:
        """手动平仓。"""
        db = SessionLocal()
        try:
            account = self._get_or_create_account(db)
            pos = (
                db.query(PaperTradingPosition)
                .filter(
                    PaperTradingPosition.id == position_id,
                    PaperTradingPosition.status == "open",
                )
                .first()
            )
            if not pos:
                return {"ok": False, "error": "持仓不存在或已平仓"}

            # 获取最新报价
            mc = _to_market(pos.stock_market)
            tsym = _tencent_symbol(pos.stock_symbol, mc)
            try:
                rows = _fetch_tencent_quotes([tsym])
            except Exception:
                rows = []

            exit_price = pos.current_price or pos.entry_price
            if rows:
                p = _safe_float(rows[0].get("current_price"))
                if p and p > 0:
                    exit_price = p

            trade = self._close_position(db, account, pos, exit_price, "manual")
            self._update_account_metrics(db, account)
            db.commit()
            return {"ok": True, "pos": pos, "trade": trade}
        finally:
            db.close()

    async def close_position_manual_async(self, position_id: int) -> dict:
        """异步手动平仓，含通知。"""
        result = await asyncio.to_thread(self.close_position_manual, position_id)
        if result.get("ok"):
            try:
                from src.core.paper_trading_notifier import notify_exit
                pos = result.pop("pos", None)
                trade = result.pop("trade", None)
                if pos and trade:
                    await notify_exit(pos, trade)
            except Exception:
                logger.exception("[模拟盘] 手动平仓通知失败")
        return result

    async def _send_notifications(self, result: dict) -> None:
        """从扫描结果中取出事件，发送通知。"""
        try:
            from src.core.paper_trading_notifier import notify_entry, notify_exit

            for pos, sig in result.pop("entry_events", []):
                await notify_entry(pos, sig)
            for pos, trade in result.pop("exit_events", []):
                await notify_exit(pos, trade)
        except Exception:
            logger.exception("[模拟盘] 通知发送失败")

    def reset_account(self) -> dict:
        """重置模拟盘（清空所有数据）。"""
        db = SessionLocal()
        try:
            db.query(PaperTradingPosition).delete()
            db.query(PaperTradingTrade).delete()
            account = db.query(PaperTradingAccount).first()
            if account:
                account.current_capital = account.initial_capital
                account.total_pnl = 0.0
                account.total_trades = 0
                account.winning_trades = 0
                account.max_drawdown_pct = 0.0
                account.peak_capital = account.initial_capital
                account.enabled = True
            db.commit()
            return {"ok": True}
        finally:
            db.close()


ENGINE = PaperTradingEngine()
