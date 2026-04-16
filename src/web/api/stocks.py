import asyncio
import logging
import threading
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.core.market_data import market_data
from src.core.instrument_service import (
    FUTURES_MARKET,
    OPTIONS_MARKET,
    ensure_stock_compatibility,
    search_future_instruments,
)
from src.core.option_service import resolve_option_contract, search_option_instruments
from src.core.portfolio_service import list_watchlist_stocks
from src.core.runtime_views import build_market_status, build_stocks_workspace
from src.web.database import get_db
from src.web.models import (
    Stock,
    StockAgent,
    AgentConfig,
    Position,
    PriceAlertRule,
    PriceAlertHit,
)
from src.web.stock_list import search_stocks, refresh_stock_list
from src.core.agent_catalog import AGENT_KIND_WORKFLOW, infer_agent_kind

logger = logging.getLogger(__name__)
router = APIRouter()


class StockCreate(BaseModel):
    symbol: str
    name: str
    market: str = "CN"
    exchange: str = ""
    underlying_symbol: str = ""
    underlying_name: str = ""
    contract_multiplier: float | None = None
    tick_size: float | None = None
    expiry_date: str = ""
    option_type: str = ""
    strike_price: float | None = None
    exercise_style: str = ""


class StockUpdate(BaseModel):
    name: str | None = None


class StockAgentInfo(BaseModel):
    agent_name: str
    schedule: str = ""
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = []


class StockResponse(BaseModel):
    id: int
    symbol: str
    name: str
    market: str
    instrument_id: int | None = None
    instrument_type: str = "equity"
    exchange: str | None = None
    underlying_symbol: str | None = None
    underlying_name: str | None = None
    contract_multiplier: float | None = None
    tick_size: float | None = None
    expiry_date: str | None = None
    is_main_contract: bool | None = None
    option_type: str | None = None
    strike_price: float | None = None
    sort_order: int
    agents: list[StockAgentInfo] = []

    class Config:
        from_attributes = True


class StockAgentItem(BaseModel):
    agent_name: str
    schedule: str = ""
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = []


class StockAgentUpdate(BaseModel):
    agents: list[StockAgentItem]


class StockReorderItem(BaseModel):
    id: int
    sort_order: int


class StockReorderRequest(BaseModel):
    items: list[StockReorderItem]


def _stock_to_response(stock: Stock) -> dict:
    instrument = stock.instrument
    return {
        "id": stock.id,
        "symbol": stock.symbol,
        "name": stock.name,
        "market": stock.market,
        "instrument_id": stock.instrument_id,
        "instrument_type": getattr(instrument, "instrument_type", "equity"),
        "exchange": getattr(instrument, "exchange", None),
        "underlying_symbol": getattr(instrument, "underlying_symbol", None),
        "underlying_name": getattr(instrument, "underlying_name", None),
        "contract_multiplier": getattr(instrument, "contract_multiplier", 1.0),
        "tick_size": getattr(instrument, "tick_size", None),
        "expiry_date": getattr(instrument, "expiry_date", None),
        "is_main_contract": getattr(instrument, "is_main_contract", None),
        "option_type": getattr(instrument, "option_type", None),
        "strike_price": getattr(instrument, "strike_price", None),
        "sort_order": stock.sort_order or 0,
        "agents": [
            {
                "agent_name": sa.agent_name,
                "schedule": sa.schedule or "",
                "ai_model_id": sa.ai_model_id,
                "notify_channel_ids": sa.notify_channel_ids or [],
            }
            for sa in stock.agents
            if infer_agent_kind(sa.agent_name) == AGENT_KIND_WORKFLOW
        ],
    }


@router.get("/markets/status")
def get_market_status():
    return build_market_status()


@router.get("/search")
def search(q: str = Query("", min_length=1), market: str = Query("")):
    """模糊搜索股票(代码/名称)"""
    market_value = str(market or "").strip().upper()
    if market_value in {"HK", "US"}:
        raise HTTPException(400, f"unsupported market in CN-only mode: {market_value}")
    results: list[dict] = []
    if market_value not in {FUTURES_MARKET, OPTIONS_MARKET}:
        for item in search_stocks(q, market_value if market_value == "CN" else ""):
            results.append(
                {
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "market": item.get("market"),
                    "instrument_type": "equity",
                    "contract_multiplier": 1.0,
                    "option_type": "",
                    "strike_price": None,
                }
            )
    if market_value in {"", FUTURES_MARKET}:
        results.extend(search_future_instruments(q, limit=20))
    if market_value in {"", OPTIONS_MARKET}:
        results.extend(search_option_instruments(q, limit=20))

    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in results:
        key = (
            str(item.get("market") or "").upper(),
            str(item.get("symbol") or "").upper(),
        )
        if not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:20]


@router.post("/refresh-list")
def refresh_list():
    """刷新股票列表缓存"""
    stocks = refresh_stock_list()
    return {"count": len(stocks)}


@router.get("", response_model=list[StockResponse])
def list_stocks(db: Session = Depends(get_db)):
    stocks = list_watchlist_stocks(db)
    return [_stock_to_response(s) for s in stocks]


@router.get("/quotes")
def get_quotes(db: Session = Depends(get_db)):
    """????????????"""
    stocks = list_watchlist_stocks(db)
    if not stocks:
        return {}
    rows = market_data.get_quotes_batch([{"symbol": stock.symbol, "market": stock.market} for stock in stocks])
    return {
        str(row.get("symbol") or "").upper(): {
            "current_price": row.get("current_price"),
            "change_pct": row.get("change_pct"),
            "change_amount": row.get("change_amount"),
            "prev_close": row.get("prev_close"),
        }
        for row in rows
    }


@router.get("/workspace")
def get_workspace(db: Session = Depends(get_db)):
    return build_stocks_workspace(db)


@router.post("", response_model=StockResponse)
def create_stock(stock: StockCreate, db: Session = Depends(get_db)):
    market_value = str(stock.market or "CN").upper()
    if market_value in {"HK", "US"}:
        raise HTTPException(400, f"unsupported market in CN-only mode: {market_value}")
    symbol_value = (
        str(stock.symbol or "").strip().upper()
        if market_value in {FUTURES_MARKET, OPTIONS_MARKET}
        else str(stock.symbol or "").strip()
    )
    if market_value == OPTIONS_MARKET:
        resolved = resolve_option_contract(symbol_value)
        if not resolved:
            raise HTTPException(400, f"unsupported option symbol: {symbol_value}")
        symbol_value = str(resolved.get("symbol") or symbol_value)
    existing = db.query(Stock).filter(
        Stock.symbol == symbol_value, Stock.market == market_value
    ).first()
    if existing:
        raise HTTPException(400, f"股票 {stock.symbol} 已存在")

    try:
        ensured = ensure_stock_compatibility(
            db,
            symbol=symbol_value,
            name=stock.name,
            market=market_value,
        )
        if market_value == OPTIONS_MARKET and ensured.instrument:
            instrument = ensured.instrument
            instrument.exchange = stock.exchange or instrument.exchange
            instrument.underlying_symbol = stock.underlying_symbol or instrument.underlying_symbol
            instrument.underlying_name = stock.underlying_name or instrument.underlying_name
            if stock.contract_multiplier is not None:
                instrument.contract_multiplier = stock.contract_multiplier
            if stock.tick_size is not None:
                instrument.tick_size = stock.tick_size
            if stock.expiry_date:
                instrument.expiry_date = stock.expiry_date
            if stock.option_type:
                instrument.option_type = stock.option_type
            if stock.strike_price is not None:
                instrument.strike_price = stock.strike_price
            if stock.exercise_style:
                instrument.exercise_style = stock.exercise_style
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    db_stock = ensured.stock
    if not db_stock:
        raise HTTPException(500, "failed to create compatibility stock")
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.put("/reorder")
def reorder_stocks(body: StockReorderRequest, db: Session = Depends(get_db)):
    if not body.items:
        return {"updated": 0}
    ids = [int(x.id) for x in body.items]
    rows = db.query(Stock).filter(Stock.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in body.items:
        row = row_map.get(int(item.id))
        if not row:
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


@router.put("/{stock_id}", response_model=StockResponse)
def update_stock(stock_id: int, stock: StockUpdate, db: Session = Depends(get_db)):
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    for key, value in stock.model_dump(exclude_unset=True).items():
        setattr(db_stock, key, value)

    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.delete("/{stock_id}")
def delete_stock(stock_id: int, db: Session = Depends(get_db)):
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    # 删除股票前，要求先清理持仓，避免误删资产数据。
    has_position = db.query(Position.id).filter(Position.stock_id == stock_id).first()
    if has_position:
        raise HTTPException(400, "该股票存在持仓，请先删除持仓后再删除股票")

    # SQLite 默认可能不启用 FK 级联，手动清理提醒数据避免孤儿记录。
    rule_ids = [
        row[0]
        for row in db.query(PriceAlertRule.id).filter(
            PriceAlertRule.stock_id == stock_id
        ).all()
    ]
    if rule_ids:
        db.query(PriceAlertHit).filter(PriceAlertHit.rule_id.in_(rule_ids)).delete(
            synchronize_session=False
        )
    db.query(PriceAlertHit).filter(PriceAlertHit.stock_id == stock_id).delete(
        synchronize_session=False
    )
    db.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock_id).delete(
        synchronize_session=False
    )
    db.query(StockAgent).filter(StockAgent.stock_id == stock_id).delete(
        synchronize_session=False
    )

    db.delete(db_stock)
    db.commit()
    return {"ok": True}


@router.put("/{stock_id}/agents", response_model=StockResponse)
def update_stock_agents(stock_id: int, body: StockAgentUpdate, db: Session = Depends(get_db)):
    """更新股票关联的 Agent 列表（含调度配置和 AI/通知覆盖）"""
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    for item in body.agents:
        agent = db.query(AgentConfig).filter(AgentConfig.name == item.agent_name).first()
        if not agent:
            raise HTTPException(400, f"Agent {item.agent_name} 不存在")
        agent_kind = (agent.kind or "").strip() or infer_agent_kind(agent.name)
        if agent_kind != AGENT_KIND_WORKFLOW:
            raise HTTPException(400, f"Agent {item.agent_name} 为内部能力，不支持绑定到股票")

    # 清除旧关联，重建
    db.query(StockAgent).filter(StockAgent.stock_id == stock_id).delete()
    for item in body.agents:
        db.add(StockAgent(
            stock_id=stock_id,
            agent_name=item.agent_name,
            schedule=item.schedule,
            ai_model_id=item.ai_model_id,
            notify_channel_ids=item.notify_channel_ids,
        ))

    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.post("/{stock_id}/agents/{agent_name}/trigger")
async def trigger_stock_agent(
    stock_id: int,
    agent_name: str,
    bypass_throttle: bool = False,
    bypass_market_hours: bool = False,
    allow_unbound: bool = False,
    wait: bool = False,
    symbol: str = Query(""),
    market: str = Query("CN"),
    name: str = Query(""),
    db: Session = Depends(get_db),
):
    """手动触发单只股票 Agent。

    - 正常模式：传有效 stock_id
    - 无绑定模式：stock_id<=0 且传 symbol/market（需 allow_unbound=true）
    - 无绑定模式默认禁用通知（仅生成建议）
    - 默认异步执行（立即返回），传 wait=true 可同步等待结果
    """
    sa = None
    trigger_stock = None
    suppress_notify = stock_id <= 0

    if stock_id > 0:
        db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
        if not db_stock:
            raise HTTPException(404, "股票不存在")

        sa = db.query(StockAgent).filter(
            StockAgent.stock_id == stock_id, StockAgent.agent_name == agent_name
        ).first()
        if not sa and not allow_unbound:
            raise HTTPException(400, f"股票未关联 Agent {agent_name}")
        if not sa and allow_unbound:
            # 允许无绑定触发时，至少确保 Agent 存在。
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if not agent:
                raise HTTPException(400, f"Agent {agent_name} 不存在")
        trigger_stock = db_stock
    else:
        symbol = (symbol or "").strip()
        if not symbol:
            raise HTTPException(400, "当 stock_id<=0 时，symbol 不能为空")
        if not allow_unbound:
            raise HTTPException(400, "当 stock_id<=0 时，需设置 allow_unbound=true")

        market = (market or "CN").strip().upper() or "CN"
        name = (name or "").strip() or symbol
        db_stock = db.query(Stock).filter(
            Stock.symbol == symbol, Stock.market == market
        ).first()
        if db_stock:
            sa = db.query(StockAgent).filter(
                StockAgent.stock_id == db_stock.id, StockAgent.agent_name == agent_name
            ).first()
            trigger_stock = db_stock
        else:
            # 不落库：用于详情弹窗未持仓且未关注股票的一次性分析。
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if not agent:
                raise HTTPException(400, f"Agent {agent_name} 不存在")
            trigger_stock = SimpleNamespace(
                id=0,
                symbol=symbol,
                name=name,
                market=market,
            )

    logger.info(
        f"手动触发 Agent {agent_name} - {trigger_stock.name}({trigger_stock.symbol})"
    )

    from server import trigger_agent_for_stock

    if not wait:
        # 异步模式：后台执行，立即返回
        sa_id = sa.id if sa else None

        def _runner():
            try:
                asyncio.run(trigger_agent_for_stock(
                    agent_name,
                    trigger_stock,
                    stock_agent_id=sa_id,
                    bypass_throttle=bypass_throttle,
                    bypass_market_hours=bypass_market_hours,
                    suppress_notify=suppress_notify,
                ))
                logger.info(f"Agent {agent_name} 后台执行完成 - {trigger_stock.symbol}")
            except Exception:
                logger.exception(f"Agent {agent_name} 后台执行失败 - {trigger_stock.symbol}")

        t = threading.Thread(
            target=_runner,
            name=f"stock-trigger-{agent_name}-{trigger_stock.symbol}",
            daemon=True,
        )
        t.start()
        return {"queued": True, "message": "已提交后台执行"}

    # 同步模式：等待结果返回
    try:
        result = await trigger_agent_for_stock(
            agent_name,
            trigger_stock,
            stock_agent_id=sa.id if sa else None,
            bypass_throttle=bypass_throttle,
            bypass_market_hours=bypass_market_hours,
            suppress_notify=suppress_notify,
        )
        logger.info(f"Agent {agent_name} 执行完成 - {trigger_stock.symbol}")
        return {
            "result": result,
            "code": int(result.get("code", 0)),
            "success": bool(result.get("success", True)),
            "message": result.get("message", "ok"),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Agent {agent_name} 执行失败 - {trigger_stock.symbol}: {e}")
        raise HTTPException(500, f"Agent 执行失败: {e}")
