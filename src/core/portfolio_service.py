from __future__ import annotations

import math
from collections import OrderedDict

from sqlalchemy.orm import Session, selectinload

from src.core.market_data import market_data
from src.web.models import Account, Position, Stock


def _safe_multiplier(value: object, default: float = 1.0) -> float:
    try:
        number = float(value or 0)
    except Exception:
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return number


def serialize_account(account: Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "available_funds": account.available_funds,
        "enabled": bool(account.enabled),
    }


def list_enabled_accounts(db: Session, account_id: int | None = None) -> list[Account]:
    query = (
        db.query(Account)
        .options(
            selectinload(Account.positions)
            .selectinload(Position.stock)
            .selectinload(Stock.instrument)
        )
        .filter(Account.enabled == True)
        .order_by(Account.id.asc())
    )
    if account_id is not None:
        query = query.filter(Account.id == account_id)
    return query.all()


def collect_quote_items_from_accounts(accounts: list[Account]) -> list[dict]:
    items: "OrderedDict[str, dict]" = OrderedDict()
    for account in accounts or []:
        for position in account.positions or []:
            stock = position.stock
            if not stock:
                continue
            key = f"{stock.market}:{stock.symbol}"
            items.setdefault(key, {"symbol": stock.symbol, "market": stock.market})
    return list(items.values())


def build_portfolio_summary(
    db: Session,
    *,
    account_id: int | None = None,
    include_quotes: bool = True,
) -> dict:
    accounts = list_enabled_accounts(db, account_id=account_id)
    if not accounts:
        return {
            "accounts": [],
            "total": {
                "total_market_value": 0,
                "total_cost": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "available_funds": 0,
                "total_assets": 0,
            },
            "exchange_rates": {},
            "quotes": {},
            "quotes_by_key": {},
        }

    quote_rows = market_data.get_quotes_batch(collect_quote_items_from_accounts(accounts)) if include_quotes else []
    quotes_by_key = {
        f"{str(row.get('market') or 'CN').upper()}:{str(row.get('symbol') or '').upper()}": row
        for row in quote_rows
    }
    quotes_legacy = {
        str(row.get("symbol") or "").upper(): {
            "current_price": row.get("current_price"),
            "change_pct": row.get("change_pct"),
        }
        for row in quote_rows
    }

    account_summaries: list[dict] = []
    grand_total_market_value = 0.0
    grand_total_cost = 0.0
    grand_available_funds = 0.0

    for account in accounts:
        positions = sorted(
            list(account.positions or []),
            key=lambda row: (int(getattr(row, "sort_order", 0) or 0), int(row.id)),
        )
        account_market_value = 0.0
        account_cost = 0.0
        serialized_positions: list[dict] = []

        for position in positions:
            stock = position.stock
            if not stock:
                continue
            instrument = stock.instrument
            quote = quotes_by_key.get(f"{stock.market}:{stock.symbol}")
            current_price = quote.get("current_price") if quote else None
            change_pct = quote.get("change_pct") if quote else None
            contract_multiplier = _safe_multiplier(
                getattr(instrument, "contract_multiplier", None),
                default=1.0,
            )
            rate = 1.0

            cost = (
                float(position.cost_price or 0.0)
                * float(position.quantity or 0)
                * contract_multiplier
            )
            cost_cny = cost * rate
            account_cost += cost_cny

            market_value = None
            market_value_cny = None
            pnl = None
            pnl_pct = None
            if current_price is not None:
                market_value = (
                    float(current_price)
                    * float(position.quantity or 0)
                    * contract_multiplier
                )
                market_value_cny = market_value * rate
                pnl = market_value_cny - cost_cny
                pnl_pct = (pnl / cost_cny * 100) if cost_cny > 0 else 0
                account_market_value += market_value_cny

            serialized_positions.append(
                {
                    "id": position.id,
                    "stock_id": position.stock_id,
                    "instrument_id": stock.instrument_id,
                    "instrument_type": getattr(instrument, "instrument_type", "equity"),
                    "symbol": stock.symbol,
                    "name": stock.name,
                    "market": stock.market,
                    "exchange": getattr(instrument, "exchange", "") or "",
                    "underlying_symbol": getattr(instrument, "underlying_symbol", "") or "",
                    "underlying_name": getattr(instrument, "underlying_name", "") or "",
                    "contract_multiplier": contract_multiplier,
                    "expiry_date": getattr(instrument, "expiry_date", "") or "",
                    "is_main_contract": bool(getattr(instrument, "is_main_contract", False)),
                    "cost_price": position.cost_price,
                    "quantity": position.quantity,
                    "invested_amount": position.invested_amount,
                    "sort_order": position.sort_order or 0,
                    "trading_style": position.trading_style,
                    "current_price": current_price,
                    "current_price_cny": round(float(current_price) * rate, 2) if current_price is not None else None,
                    "change_pct": change_pct,
                    "market_value": round(market_value, 2) if market_value is not None else None,
                    "market_value_cny": round(market_value_cny, 2) if market_value_cny is not None else None,
                    "pnl": round(pnl, 2) if pnl is not None else None,
                    "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                    "exchange_rate": None,
                }
            )

        account_pnl = account_market_value - account_cost if include_quotes else 0.0
        account_pnl_pct = (account_pnl / account_cost * 100) if include_quotes and account_cost > 0 else 0.0
        account_total_assets = (account_market_value + account.available_funds) if include_quotes else account.available_funds

        account_summaries.append(
            {
                "id": account.id,
                "name": account.name,
                "available_funds": account.available_funds,
                "total_market_value": round(account_market_value, 2),
                "total_cost": round(account_cost, 2),
                "total_pnl": round(account_pnl, 2),
                "total_pnl_pct": round(account_pnl_pct, 2),
                "total_assets": round(account_total_assets, 2),
                "positions": serialized_positions,
            }
        )

        grand_total_market_value += account_market_value
        grand_total_cost += account_cost
        grand_available_funds += account.available_funds

    total_pnl = (grand_total_market_value - grand_total_cost) if include_quotes else 0.0
    total_pnl_pct = (total_pnl / grand_total_cost * 100) if include_quotes and grand_total_cost > 0 else 0.0
    total_assets = (grand_total_market_value + grand_available_funds) if include_quotes else grand_available_funds

    return {
        "accounts": account_summaries,
        "total": {
            "total_market_value": round(grand_total_market_value, 2),
            "total_cost": round(grand_total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "available_funds": round(grand_available_funds, 2),
            "total_assets": round(total_assets, 2),
        },
        "exchange_rates": {},
        "quotes": quotes_legacy,
        "quotes_by_key": quotes_by_key,
    }


def list_watchlist_stocks(db: Session) -> list[Stock]:
    return (
        db.query(Stock)
        .options(selectinload(Stock.agents), selectinload(Stock.instrument))
        .order_by(Stock.sort_order.asc(), Stock.id.asc())
        .all()
    )
