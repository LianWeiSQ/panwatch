from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.core.runtime_views import build_dashboard_runtime, build_stocks_workspace
from src.web.database import SessionLocal

router = APIRouter()


def _build_snapshot(
    *,
    kind: Literal["dashboard", "stocks"],
    discover_market: str,
    boards_mode: str,
    stocks_mode: str,
):
    db = SessionLocal()
    try:
        if kind == "dashboard":
            return build_dashboard_runtime(
                db,
                discover_market=discover_market,
                boards_mode=boards_mode,
                stocks_mode=stocks_mode,
            )
        if kind == "stocks":
            return build_stocks_workspace(db)
        raise ValueError(f"unsupported stream kind: {kind}")
    finally:
        db.close()


@router.get("/stream")
async def runtime_stream(
    kind: Literal["dashboard", "stocks"] = Query("dashboard"),
    interval_seconds: int = Query(15, ge=5, le=300),
    discover_market: str = Query("CN"),
    boards_mode: str = Query("gainers"),
    stocks_mode: str = Query("for_you"),
):
    discover_market = (discover_market or "CN").strip().upper() or "CN"
    if discover_market != "CN":
        raise HTTPException(400, f"dashboard discovery supports CN only in current mode: {discover_market}")

    async def event_generator():
        while True:
            try:
                payload = await asyncio.to_thread(
                    _build_snapshot,
                    kind=kind,
                    discover_market=discover_market,
                    boards_mode=boards_mode,
                    stocks_mode=stocks_mode,
                )
                yield f"event: snapshot\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except ValueError as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
                return
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(interval_seconds)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
