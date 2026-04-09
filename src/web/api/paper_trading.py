"""模拟盘 API 端点。"""

import logging
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from src.config import Settings
from src.core.paper_trading_engine import ENGINE
from src.web.database import get_db
from src.web.models import (
    AppSettings,
    NotifyChannel,
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _format_dt(dt) -> str:
    if not dt:
        return ""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


class ToggleBody(BaseModel):
    enabled: bool


class UpdateSettingsBody(BaseModel):
    excluded_markets: list[str] | None = None


def _account_response(acc: PaperTradingAccount, open_positions: list[PaperTradingPosition] | None = None) -> dict:
    unrealized = 0.0
    positions_value = 0.0
    if open_positions:
        for p in open_positions:
            unrealized += p.unrealized_pnl or 0
            positions_value += (p.current_price or p.entry_price) * p.quantity
    total_equity = acc.current_capital + positions_value
    win_rate = (acc.winning_trades / acc.total_trades * 100) if acc.total_trades > 0 else 0.0
    return {
        "id": acc.id,
        "initial_capital": acc.initial_capital,
        "current_capital": acc.current_capital,
        "total_equity": round(total_equity, 2),
        "total_pnl": round(acc.total_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_trades": acc.total_trades,
        "winning_trades": acc.winning_trades,
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": acc.max_drawdown_pct,
        "peak_capital": acc.peak_capital,
        "enabled": acc.enabled,
        "excluded_markets": acc.excluded_markets or [],
        "created_at": _format_dt(acc.created_at),
        "updated_at": _format_dt(acc.updated_at),
    }


def _position_response(p: PaperTradingPosition) -> dict:
    holding_days = 0
    if p.opened_at:
        from datetime import datetime, timezone as tz
        now = datetime.now(tz.utc)
        opened = p.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=tz.utc)
        holding_days = max(0, (now - opened).days)
    return {
        "id": p.id,
        "stock_symbol": p.stock_symbol,
        "stock_market": p.stock_market,
        "stock_name": p.stock_name or "",
        "quantity": p.quantity,
        "entry_price": p.entry_price,
        "stop_loss": p.stop_loss,
        "target_price": p.target_price,
        "current_price": p.current_price,
        "unrealized_pnl": round(p.unrealized_pnl or 0, 2),
        "unrealized_pnl_pct": round(
            ((p.current_price - p.entry_price) / p.entry_price * 100)
            if p.current_price and p.entry_price > 0 else 0, 2
        ),
        "status": p.status,
        "signal_run_id": p.signal_run_id,
        "signal_snapshot_date": p.signal_snapshot_date or "",
        "signal_action": p.signal_action or "",
        "strategy_code": p.strategy_code or "",
        "holding_days": holding_days,
        "opened_at": _format_dt(p.opened_at),
        "closed_at": _format_dt(p.closed_at),
        "updated_at": _format_dt(p.updated_at),
    }


def _trade_response(t: PaperTradingTrade) -> dict:
    return {
        "id": t.id,
        "stock_symbol": t.stock_symbol,
        "stock_market": t.stock_market,
        "stock_name": t.stock_name or "",
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "pnl": round(t.pnl, 2),
        "pnl_pct": round(t.pnl_pct, 2),
        "exit_reason": t.exit_reason,
        "signal_run_id": t.signal_run_id,
        "signal_snapshot_date": t.signal_snapshot_date or "",
        "strategy_code": t.strategy_code or "",
        "holding_days": t.holding_days or 0,
        "opened_at": _format_dt(t.opened_at),
        "closed_at": _format_dt(t.closed_at),
    }


@router.get("/account")
def get_account(db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        acc = PaperTradingAccount(
            initial_capital=1000000.0,
            current_capital=1000000.0,
            peak_capital=1000000.0,
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)
    open_positions = (
        db.query(PaperTradingPosition)
        .filter(PaperTradingPosition.status == "open")
        .all()
    )
    return _account_response(acc, open_positions)


@router.get("/positions")
def list_positions(status: str = "open", db: Session = Depends(get_db)):
    query = db.query(PaperTradingPosition)
    if status != "all":
        query = query.filter(PaperTradingPosition.status == status)
    rows = query.order_by(PaperTradingPosition.opened_at.desc()).all()
    return [_position_response(p) for p in rows]


@router.get("/trades")
def list_trades(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    total = db.query(PaperTradingTrade).count()
    rows = (
        db.query(PaperTradingTrade)
        .order_by(PaperTradingTrade.closed_at.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "total": total,
        "items": [_trade_response(t) for t in rows],
    }


@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        return {"account": None, "equity_curve": [], "open_positions": 0}

    open_positions = (
        db.query(PaperTradingPosition)
        .filter(PaperTradingPosition.status == "open")
        .all()
    )
    open_count = len(open_positions)

    # 构建收益曲线：已平仓盈亏按日聚合 + 当天浮动权益
    trades = (
        db.query(PaperTradingTrade)
        .order_by(PaperTradingTrade.closed_at.asc())
        .all()
    )
    equity_curve = []
    by_date: dict[str, float] = {}
    for t in trades:
        date_str = ""
        if t.closed_at:
            dt = t.closed_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
        if not date_str:
            continue
        by_date.setdefault(date_str, 0)
        by_date[date_str] += t.pnl

    running = acc.initial_capital
    for date_str in sorted(by_date.keys()):
        running += by_date[date_str]
        equity_curve.append({"date": date_str, "equity": round(running, 2)})

    # 追加当前时刻的总权益（含未平仓浮动盈亏）
    from datetime import datetime as dt_cls
    today_str = dt_cls.now(timezone.utc).strftime("%Y-%m-%d")
    positions_value = sum(
        (p.current_price or p.entry_price) * p.quantity for p in open_positions
    )
    total_equity_now = acc.current_capital + positions_value
    if equity_curve and equity_curve[-1]["date"] == today_str:
        # 同一天：用含浮动的总权益覆盖
        equity_curve[-1]["equity"] = round(total_equity_now, 2)
    else:
        equity_curve.append({"date": today_str, "equity": round(total_equity_now, 2)})

    # 如果只有一个点（今天），补一个起始点
    if len(equity_curve) == 1:
        # 用账户创建日作为起始点
        created = acc.created_at
        if created:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            start_str = created.strftime("%Y-%m-%d")
            if start_str != today_str:
                equity_curve.insert(0, {"date": start_str, "equity": acc.initial_capital})
            else:
                # 同一天创建，人为加一个初始点
                equity_curve.insert(0, {"date": today_str + " 00:00", "equity": acc.initial_capital})

    # 按策略聚合绩效（已平仓 + 持仓中）
    all_trades = db.query(PaperTradingTrade).all()
    strategy_stats: dict[str, dict] = {}
    for t in all_trades:
        code = t.strategy_code or "unknown"
        s = strategy_stats.setdefault(code, {
            "strategy_code": code,
            "total_trades": 0, "winning_trades": 0,
            "total_pnl": 0.0, "total_pnl_pct_sum": 0.0,
            "holding_days_sum": 0,
            "open_positions": 0, "unrealized_pnl": 0.0,
        })
        s["total_trades"] += 1
        s["total_pnl"] += t.pnl
        s["total_pnl_pct_sum"] += t.pnl_pct
        s["holding_days_sum"] += t.holding_days or 0
        if t.pnl > 0:
            s["winning_trades"] += 1

    for p in open_positions:
        code = p.strategy_code or "unknown"
        s = strategy_stats.setdefault(code, {
            "strategy_code": code,
            "total_trades": 0, "winning_trades": 0,
            "total_pnl": 0.0, "total_pnl_pct_sum": 0.0,
            "holding_days_sum": 0,
            "open_positions": 0, "unrealized_pnl": 0.0,
        })
        s["open_positions"] += 1
        s["unrealized_pnl"] += p.unrealized_pnl or 0

    strategy_perf = []
    for s in strategy_stats.values():
        n = s["total_trades"]
        strategy_perf.append({
            "strategy_code": s["strategy_code"],
            "total_trades": n,
            "winning_trades": s["winning_trades"],
            "win_rate": round(s["winning_trades"] / n * 100, 1) if n > 0 else 0,
            "total_pnl": round(s["total_pnl"], 2),
            "avg_pnl_pct": round(s["total_pnl_pct_sum"] / n, 2) if n > 0 else 0,
            "avg_holding_days": round(s["holding_days_sum"] / n, 1) if n > 0 else 0,
            "open_positions": s["open_positions"],
            "unrealized_pnl": round(s["unrealized_pnl"], 2),
        })
    strategy_perf.sort(key=lambda x: x["total_pnl"] + x["unrealized_pnl"], reverse=True)

    return {
        "account": _account_response(acc, open_positions),
        "equity_curve": equity_curve,
        "open_positions": open_count,
        "strategy_performance": strategy_perf,
    }


@router.post("/account/toggle")
def toggle_account(body: ToggleBody, db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        raise HTTPException(404, "模拟盘账户不存在")
    acc.enabled = body.enabled
    db.commit()
    db.refresh(acc)
    return _account_response(acc)


@router.post("/account/reset")
def reset_account():
    result = ENGINE.reset_account()
    if not result.get("ok"):
        raise HTTPException(500, "重置失败")
    return {"ok": True}


@router.post("/positions/{position_id}/close")
async def close_position(position_id: int):
    result = await ENGINE.close_position_manual_async(position_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "平仓失败"))
    return {"ok": True}


@router.post("/account/settings")
def update_settings(body: UpdateSettingsBody, db: Session = Depends(get_db)):
    acc = db.query(PaperTradingAccount).first()
    if not acc:
        raise HTTPException(404, "模拟盘账户不存在")
    if body.excluded_markets is not None:
        valid = {"CN", "HK", "US"}
        acc.excluded_markets = [m for m in body.excluded_markets if m in valid]
    db.commit()
    db.refresh(acc)
    return _account_response(acc)


@router.post("/scan")
async def manual_scan():
    """手动触发一次模拟盘扫描（建仓 + 平仓检查）。"""
    result = await ENGINE.scan_once()
    return result


# ---------------------------------------------------------------------------
# 跟单通知设置
# ---------------------------------------------------------------------------

_NOTIFY_KEYS = [
    "pt_notify_enabled",
    "pt_notify_channel_ids",
    "pt_notify_realtime",
    "pt_notify_premarket",
    "pt_notify_summary",
]

_NOTIFY_DEFAULTS = {
    "pt_notify_enabled": "false",
    "pt_notify_channel_ids": "",
    "pt_notify_realtime": "true",
    "pt_notify_premarket": "true",
    "pt_notify_summary": "true",
}


@router.get("/notify-settings")
def get_notify_settings(db: Session = Depends(get_db)):
    """返回当前通知配置 + 可用渠道列表。"""
    rows = db.query(AppSettings).filter(AppSettings.key.in_(_NOTIFY_KEYS)).all()
    settings = dict(_NOTIFY_DEFAULTS)
    for r in rows:
        settings[r.key] = r.value or _NOTIFY_DEFAULTS.get(r.key, "")

    channels = db.query(NotifyChannel).filter(NotifyChannel.enabled.is_(True)).all()
    channel_list = [
        {"id": ch.id, "name": ch.name, "type": ch.type, "is_default": ch.is_default}
        for ch in channels
    ]

    return {"settings": settings, "channels": channel_list}


class NotifySettingsBody(BaseModel):
    pt_notify_enabled: str | None = None
    pt_notify_channel_ids: str | None = None
    pt_notify_realtime: str | None = None
    pt_notify_premarket: str | None = None
    pt_notify_summary: str | None = None


@router.post("/notify-settings")
def update_notify_settings(body: NotifySettingsBody, db: Session = Depends(get_db)):
    """更新通知配置。"""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    for key, value in updates.items():
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSettings(key=key, value=value, description=f"模拟盘通知配置: {key}"))
    db.commit()
    return get_notify_settings(db)


@router.post("/notify-test")
async def test_notify():
    """发送测试通知。"""
    from src.core.paper_trading_notifier import send_test_notification
    result = await send_test_notification()
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "发送失败"))
    return {"ok": True}
