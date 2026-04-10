import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core.discovery_service import (
    get_board_stocks as load_board_stocks,
    get_hot_boards as load_hot_boards,
    get_hot_stocks as load_hot_stocks,
)
from src.web.database import get_db

router = APIRouter()


def _ensure_cn_market(market: str) -> str:
    market_value = str(market or "CN").strip().upper() or "CN"
    if market_value != "CN":
        raise HTTPException(400, f"discovery supports CN only in current mode: {market_value}")
    return "CN"


@router.get("/stocks")
async def get_hot_stocks(
    market: str = "CN",
    mode: str = "turnover",
    limit: int = 20,
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            load_hot_stocks,
            db,
            _ensure_cn_market(market),
            mode,
            limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(503, "discovery hot stocks unavailable")


@router.get("/boards")
async def get_hot_boards(
    market: str = "CN",
    mode: str = "gainers",
    limit: int = 12,
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            load_hot_boards,
            db,
            _ensure_cn_market(market),
            mode,
            limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(503, "discovery hot boards unavailable")


@router.get("/boards/{board_code}/stocks")
async def get_board_stocks(
    board_code: str,
    mode: str = "gainers",
    limit: int = 20,
    market: str = "CN",
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            load_board_stocks,
            db,
            board_code,
            mode,
            limit,
            _ensure_cn_market(market),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(503, "discovery board stocks unavailable")
