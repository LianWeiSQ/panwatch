from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.agents.news_digest import NewsDigestAgent
from src.core.news_analysis import NewsAnalysisService
from src.web.database import get_db
from src.web.models import AgentConfig

router = APIRouter()


def _load_news_digest_config(db: Session) -> dict:
    row = db.query(AgentConfig).filter(AgentConfig.name == "news_digest").first()
    return row.config if row and isinstance(row.config, dict) else {}


@router.get("/runtime")
def get_news_analysis_runtime(
    hours: int = Query(default=72, ge=1, le=720),
    related_only: bool = Query(default=False),
    language: str = Query(default=""),
    source: str = Query(default=""),
    limit: int = Query(default=120, ge=1, le=300),
    db: Session = Depends(get_db),
):
    service = NewsAnalysisService(db)
    return service.build_runtime(
        hours=hours,
        related_only=related_only,
        language=language,
        source=source,
        limit=limit,
    )


@router.post("/refresh")
async def refresh_news_analysis(
    hours: int = Query(default=72, ge=1, le=720),
    related_only: bool = Query(default=False),
    language: str = Query(default=""),
    source: str = Query(default=""),
    limit: int = Query(default=120, ge=1, le=300),
    db: Session = Depends(get_db),
):
    from server import build_context

    agent_config = _load_news_digest_config(db)
    try:
        agent = NewsDigestAgent(**agent_config)
    except TypeError:
        agent = NewsDigestAgent()
    context = build_context("news_digest")
    data = await agent.collect(context)
    await agent.analyze(context, data)

    db.expire_all()
    service = NewsAnalysisService(db)
    runtime = service.build_runtime(
        hours=hours,
        related_only=related_only,
        language=language,
        source=source,
        limit=limit,
    )
    runtime["refresh_result"] = {
        "timestamp": data.get("timestamp"),
        "articles": len(data.get("news") or []),
        "related_articles": len(data.get("related_news") or []),
        "sentiment": data.get("sentiment") or "neutral",
    }
    return runtime
