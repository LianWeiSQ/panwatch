"""账户和持仓管理 API"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.core.market_data import market_data
from src.core.portfolio_service import build_portfolio_summary
from src.web.database import get_db
from src.web.models import Account, Position, Stock

logger = logging.getLogger(__name__)
router = APIRouter()

def get_hkd_cny_rate() -> float:
    return 1.0


def get_usd_cny_rate() -> float:
    return 1.0


# ========== Pydantic Models ==========

class AccountCreate(BaseModel):
    name: str
    available_funds: float = 0


class AccountUpdate(BaseModel):
    name: str | None = None
    available_funds: float | None = None
    enabled: bool | None = None


class AccountResponse(BaseModel):
    id: int
    name: str
    available_funds: float
    enabled: bool

    class Config:
        from_attributes = True


class PositionCreate(BaseModel):
    account_id: int
    stock_id: int
    cost_price: float
    quantity: int
    invested_amount: float | None = None
    trading_style: str | None = None  # short: 短线, swing: 波段, long: 长线


class PositionUpdate(BaseModel):
    cost_price: float | None = None
    quantity: int | None = None
    invested_amount: float | None = None
    trading_style: str | None = None


class PositionResponse(BaseModel):
    id: int
    account_id: int
    stock_id: int
    instrument_id: int | None = None
    instrument_type: str | None = None
    cost_price: float
    quantity: int
    invested_amount: float | None
    sort_order: int
    trading_style: str | None
    # 关联信息
    account_name: str | None = None
    stock_symbol: str | None = None
    stock_name: str | None = None
    market: str | None = None
    exchange: str | None = None
    underlying_symbol: str | None = None
    underlying_name: str | None = None
    contract_multiplier: float | None = None
    expiry_date: str | None = None

    class Config:
        from_attributes = True


class PositionReorderItem(BaseModel):
    id: int
    sort_order: int


class PositionReorderRequest(BaseModel):
    items: list[PositionReorderItem]


# ========== Account Endpoints ==========

@router.get("/accounts", response_model=list[AccountResponse])
def list_accounts(db: Session = Depends(get_db)):
    """获取所有账户"""
    return db.query(Account).order_by(Account.id).all()


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: int, db: Session = Depends(get_db)):
    """获取单个账户"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")
    return account


@router.post("/accounts", response_model=AccountResponse)
def create_account(data: AccountCreate, db: Session = Depends(get_db)):
    """创建账户"""
    account = Account(name=data.name, available_funds=data.available_funds)
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info(f"创建账户: {account.name}")
    return account


@router.put("/accounts/{account_id}", response_model=AccountResponse)
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db)):
    """更新账户"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    if data.name is not None:
        account.name = data.name
    if data.available_funds is not None:
        account.available_funds = data.available_funds
    if data.enabled is not None:
        account.enabled = data.enabled

    db.commit()
    db.refresh(account)
    logger.info(f"更新账户: {account.name}")
    return account


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账户（会同时删除该账户的所有持仓）"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    db.delete(account)
    db.commit()
    logger.info(f"删除账户: {account.name}")
    return {"success": True}


# ========== Position Endpoints ==========

@router.get("/positions", response_model=list[PositionResponse])
def list_positions(
    account_id: int | None = None,
    stock_id: int | None = None,
    db: Session = Depends(get_db)
):
    """获取持仓列表，可按账户或股票筛选"""
    query = db.query(Position)
    if account_id:
        query = query.filter(Position.account_id == account_id)
    if stock_id:
        query = query.filter(Position.stock_id == stock_id)

    positions = query.order_by(Position.account_id.asc(), Position.sort_order.asc(), Position.id.asc()).all()
    result = []
    for pos in positions:
        instrument = pos.stock.instrument if pos.stock else None
        result.append({
            "id": pos.id,
            "account_id": pos.account_id,
            "stock_id": pos.stock_id,
            "instrument_id": pos.stock.instrument_id if pos.stock else None,
            "instrument_type": getattr(instrument, "instrument_type", "equity"),
            "cost_price": pos.cost_price,
            "quantity": pos.quantity,
            "invested_amount": pos.invested_amount,
            "sort_order": pos.sort_order or 0,
            "trading_style": pos.trading_style,
            "account_name": pos.account.name if pos.account else None,
            "stock_symbol": pos.stock.symbol if pos.stock else None,
            "stock_name": pos.stock.name if pos.stock else None,
            "market": pos.stock.market if pos.stock else None,
            "exchange": getattr(instrument, "exchange", None),
            "underlying_symbol": getattr(instrument, "underlying_symbol", None),
            "underlying_name": getattr(instrument, "underlying_name", None),
            "contract_multiplier": getattr(instrument, "contract_multiplier", 1.0),
            "expiry_date": getattr(instrument, "expiry_date", None),
        })
    return result


@router.post("/positions", response_model=PositionResponse)
def create_position(data: PositionCreate, db: Session = Depends(get_db)):
    """创建持仓"""
    # 检查账户和股票是否存在
    account = db.query(Account).filter(Account.id == data.account_id).first()
    if not account:
        raise HTTPException(400, "账户不存在")

    stock = db.query(Stock).filter(Stock.id == data.stock_id).first()
    if not stock:
        raise HTTPException(400, "股票不存在")

    # 检查是否已存在该账户的该股票持仓
    existing = db.query(Position).filter(
        Position.account_id == data.account_id,
        Position.stock_id == data.stock_id,
    ).first()
    if existing:
        raise HTTPException(400, f"账户 {account.name} 已有 {stock.name} 的持仓，请编辑现有持仓")

    max_order = db.query(func.max(Position.sort_order)).filter(
        Position.account_id == data.account_id
    ).scalar() or 0

    position = Position(
        account_id=data.account_id,
        stock_id=data.stock_id,
        cost_price=data.cost_price,
        quantity=data.quantity,
        invested_amount=data.invested_amount,
        sort_order=int(max_order) + 1,
        trading_style=data.trading_style,
    )
    db.add(position)
    db.commit()
    db.refresh(position)

    logger.info(f"创建持仓: {account.name} - {stock.name}")
    return {
        "id": position.id,
        "account_id": position.account_id,
        "stock_id": position.stock_id,
        "instrument_id": stock.instrument_id,
        "instrument_type": getattr(stock.instrument, "instrument_type", "equity"),
        "cost_price": position.cost_price,
        "quantity": position.quantity,
        "invested_amount": position.invested_amount,
        "sort_order": position.sort_order or 0,
        "trading_style": position.trading_style,
        "account_name": account.name,
        "stock_symbol": stock.symbol,
        "stock_name": stock.name,
        "market": stock.market,
        "exchange": getattr(stock.instrument, "exchange", None),
        "underlying_symbol": getattr(stock.instrument, "underlying_symbol", None),
        "underlying_name": getattr(stock.instrument, "underlying_name", None),
        "contract_multiplier": getattr(stock.instrument, "contract_multiplier", 1.0),
        "expiry_date": getattr(stock.instrument, "expiry_date", None),
    }


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position(position_id: int, data: PositionUpdate, db: Session = Depends(get_db)):
    """更新持仓"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(404, "持仓不存在")

    if data.cost_price is not None:
        position.cost_price = data.cost_price
    if data.quantity is not None:
        position.quantity = data.quantity
    if data.invested_amount is not None:
        position.invested_amount = data.invested_amount
    if data.trading_style is not None:
        # 空字符串表示清空，设为 None
        position.trading_style = data.trading_style if data.trading_style else None

    db.commit()
    db.refresh(position)

    logger.info(f"更新持仓: {position.account.name} - {position.stock.name}")
    return {
        "id": position.id,
        "account_id": position.account_id,
        "stock_id": position.stock_id,
        "instrument_id": position.stock.instrument_id if position.stock else None,
        "instrument_type": getattr(position.stock.instrument, "instrument_type", "equity") if position.stock else "equity",
        "cost_price": position.cost_price,
        "quantity": position.quantity,
        "invested_amount": position.invested_amount,
        "sort_order": position.sort_order or 0,
        "trading_style": position.trading_style,
        "account_name": position.account.name,
        "stock_symbol": position.stock.symbol,
        "stock_name": position.stock.name,
        "market": position.stock.market,
        "exchange": getattr(position.stock.instrument, "exchange", None),
        "underlying_symbol": getattr(position.stock.instrument, "underlying_symbol", None),
        "underlying_name": getattr(position.stock.instrument, "underlying_name", None),
        "contract_multiplier": getattr(position.stock.instrument, "contract_multiplier", 1.0),
        "expiry_date": getattr(position.stock.instrument, "expiry_date", None),
    }


@router.delete("/positions/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    """删除持仓"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(404, "持仓不存在")

    db.delete(position)
    db.commit()
    logger.info(f"删除持仓: {position.account.name} - {position.stock.name}")
    return {"success": True}


@router.put("/positions/reorder/batch")
def reorder_positions(data: PositionReorderRequest, db: Session = Depends(get_db)):
    """批量更新持仓排序"""
    if not data.items:
        return {"updated": 0}
    ids = [int(x.id) for x in data.items]
    rows = db.query(Position).filter(Position.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in data.items:
        row = row_map.get(int(item.id))
        if not row:
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


# ========== Portfolio Summary ==========

@router.get("/portfolio/summary")
def get_portfolio_summary(
    account_id: int | None = None,
    include_quotes: bool = True,
    db: Session = Depends(get_db),
):
    summary = build_portfolio_summary(
        db,
        account_id=account_id,
        include_quotes=include_quotes,
    )
    summary.pop("quotes_by_key", None)
    return summary
