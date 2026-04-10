from fastapi import APIRouter

from src.core.market_data import market_data

router = APIRouter()


@router.get("/indices")
async def get_market_indices():
    return market_data.get_market_indices()
