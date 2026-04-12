from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from src.agents.base import AnalysisResult, AgentContext, BaseAgent
from src.collectors.news_collector import NewsItem
from src.core.analysis_history import save_analysis
from src.core.news_analysis import NewsAnalysisService
from src.core.signals.structured_output import TAG_START, strip_tagged_json, try_extract_tagged_json
from src.core.suggestion_pool import save_suggestion
from src.web.database import SessionLocal

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "news_digest.txt"

ACTION_LABELS = {
    "alert": "设置预警",
    "watch": "关注",
    "hold": "继续持有",
    "reduce": "考虑减仓",
    "sell": "考虑减仓",
    "avoid": "暂时回避",
    "buy": "关注",
    "add": "关注",
}

POSITIVE_HINTS = ("增长", "签约", "中标", "回购", "增持", "利好", "突破", "创新高")
NEGATIVE_HINTS = ("减持", "处罚", "下调", "亏损", "诉讼", "风险", "利空", "暴跌")


class NewsDigestAgent(BaseAgent):
    name = "news_digest"
    display_name = "新闻分析"
    description = "多源新闻汇总、主题提炼、情绪判断与自选股/持仓关联分析"

    def __init__(
        self,
        lookback_hours: int = 24,
        max_items_per_source: int = 40,
        top_n_for_ai: int = 24,
        translate_english: bool = True,
        watchlist_boost: float = 1.5,
    ):
        self.lookback_hours = max(1, int(lookback_hours or 24))
        self.max_items_per_source = max(1, int(max_items_per_source or 40))
        self.top_n_for_ai = max(1, int(top_n_for_ai or 24))
        self.translate_english = bool(translate_english)
        self.watchlist_boost = float(watchlist_boost or 1.5)

    async def collect(self, context: AgentContext) -> dict:
        db = SessionLocal()
        try:
            service = NewsAnalysisService(db)
            data = await service.collect_and_persist(
                lookback_hours=self.lookback_hours,
                max_items_per_source=self.max_items_per_source,
                top_n_for_ai=self.top_n_for_ai,
                translate_english=self.translate_english,
                watchlist_boost=self.watchlist_boost,
                ai_client=context.ai_client if self.translate_english else None,
            )
            data["lookback_hours"] = self.lookback_hours
            data["top_n_for_ai"] = self.top_n_for_ai
            return data
        finally:
            db.close()

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines: list[str] = [
            f"## 时间\n- 生成时间: {now_text}",
            f"- 回看窗口: 近 {data.get('lookback_hours') or self.lookback_hours} 小时",
        ]

        lines.append("\n## 自选与持仓")
        if context.watchlist:
            for stock in context.watchlist:
                position = context.portfolio.get_aggregated_position(stock.symbol)
                if position:
                    lines.append(
                        f"- {stock.name}({stock.symbol}) 已持仓 {position['total_quantity']} 股，均价 {position['avg_cost']:.2f}"
                    )
                else:
                    lines.append(f"- {stock.name}({stock.symbol}) 自选关注")
        else:
            lines.append("- 当前未绑定自选股，侧重全市场新闻分析")

        lines.append("\n## 机器预摘要")
        lines.append(f"- 总览: {data.get('summary') or '暂无明显主线'}")
        topic_names = [str(topic.get('name') or '') for topic in data.get("topics") or [] if topic.get("name")]
        lines.append(f"- 主题: {' / '.join(topic_names[:8]) if topic_names else '暂无'}")
        coverage = data.get("coverage") or {}
        lines.append(
            "- 覆盖: "
            f"文章 {coverage.get('total_articles', 0)} 篇，"
            f"关联 {coverage.get('related_articles', 0)} 篇，"
            f"启用源 {coverage.get('enabled_sources', 0)} 个"
        )
        lines.append(f"- 情绪: {data.get('sentiment') or 'neutral'}")

        lines.append(
            f"\n## 自选/持仓关联新闻 ({len(data.get('related_news') or [])} 篇)"
        )
        if data.get("related_news"):
            for item in (data.get("related_news") or [])[: min(self.top_n_for_ai, 12)]:
                lines.extend(self._format_news_item(item))
        else:
            lines.append("- 暂无直接关联新闻")

        major_news = [
            item
            for item in (data.get("news") or [])
            if item not in (data.get("related_news") or [])
        ]
        lines.append(f"\n## 全市场重点新闻 ({len(major_news)} 篇)")
        if major_news:
            for item in major_news[: min(self.top_n_for_ai, 12)]:
                lines.extend(self._format_news_item(item))
        else:
            lines.append("- 暂无全市场重点新闻")

        lines.append("\n## 源状态")
        for status in data.get("source_statuses") or []:
            label = status.get("source_name") or status.get("provider")
            if not status.get("enabled"):
                lines.append(f"- {label}: disabled")
                continue
            state = status.get("status") or "idle"
            last_success = status.get("last_success_at") or "-"
            error = status.get("last_error") or ""
            suffix = f" / 最近成功 {last_success}"
            if error:
                suffix += f" / 错误 {error[:120]}"
            lines.append(f"- {label}: {state}{suffix}")

        return system_prompt, "\n".join(lines)

    def _format_news_item(self, item: NewsItem) -> list[str]:
        time_str = item.publish_time.strftime("%m-%d %H:%M") if item.publish_time else "--"
        source = item.source_name or item.source
        symbols = f" [{' / '.join(item.symbols)}]" if item.symbols else ""
        title = item.title or "未命名新闻"
        summary = item.cn_summary or item.summary or item.content[:160] or ""
        link = f" ({item.url})" if item.url else ""
        lines = [f"- [{source} {time_str}] {title}{symbols}{link}"]
        if summary:
            lines.append(f"  - 摘要: {summary[:220]}")
        return lines

    def _build_title(self, context: AgentContext) -> str:
        stock_items = [f"{(stock.name or stock.symbol).strip()}({stock.symbol})" for stock in context.watchlist[:5]]
        if not stock_items:
            return f"【{self.display_name}】全市场"
        title = f"【{self.display_name}】{'、'.join(stock_items)}"
        if len(context.watchlist) > 5:
            title += f" 等{len(context.watchlist)}只"
        return title

    def _payload_news(self, items: list[NewsItem]) -> list[dict]:
        out: list[dict] = []
        for item in items[:30]:
            out.append(
                {
                    "source": item.source,
                    "source_name": item.source_name,
                    "external_id": item.external_id,
                    "title": item.title,
                    "summary": item.summary or "",
                    "cn_summary": item.cn_summary or "",
                    "publish_time": item.publish_time.isoformat() if item.publish_time else "",
                    "symbols": item.symbols or [],
                    "importance": int(item.importance or 0),
                    "url": item.url or "",
                    "language": item.language or "zh",
                    "relevance_score": float(item.relevance_score or 0.0),
                }
            )
        return out

    def _keywords_score(self, text: str) -> int:
        raw = str(text or "")
        positive = sum(1 for keyword in POSITIVE_HINTS if keyword in raw)
        negative = sum(1 for keyword in NEGATIVE_HINTS if keyword in raw)
        return positive - negative

    def _default_suggestions(self, context: AgentContext, data: dict) -> dict[str, dict]:
        suggestions: dict[str, dict] = {}
        related_news = data.get("related_news") or []
        for stock in context.watchlist:
            stock_items = []
            for item in related_news:
                text = " ".join([item.title or "", item.summary or "", item.cn_summary or "", item.content or ""])
                if stock.symbol in (item.symbols or []) or (stock.name and stock.name in text) or stock.symbol in text:
                    stock_items.append(item)

            has_position = context.portfolio.has_position(stock.symbol)
            if not stock_items:
                action = "hold" if has_position else "watch"
                suggestions[stock.symbol] = {
                    "action": action,
                    "action_label": ACTION_LABELS[action],
                    "signal": "",
                    "reason": "今日暂无强相关资讯，继续跟踪量价与市场情绪变化。",
                    "triggers": [],
                    "invalidations": [],
                    "risks": [],
                    "should_alert": False,
                }
                continue

            strongest = sorted(
                stock_items,
                key=lambda item: (
                    float(item.relevance_score or 0.0),
                    int(item.importance or 0),
                    item.publish_time or datetime.min,
                ),
                reverse=True,
            )[0]
            sentiment_score = sum(
                self._keywords_score(
                    " ".join([item.title or "", item.summary or "", item.cn_summary or "", item.content or ""])
                )
                for item in stock_items[:3]
            )
            if sentiment_score < 0:
                action = "reduce" if has_position else "avoid"
            elif strongest.importance >= 3:
                action = "alert"
            elif has_position:
                action = "hold"
            else:
                action = "watch"

            summary = strongest.cn_summary or strongest.summary or strongest.title
            suggestions[stock.symbol] = {
                "action": action,
                "action_label": ACTION_LABELS.get(action, "关注"),
                "signal": strongest.title[:18],
                "reason": summary[:160],
                "triggers": [],
                "invalidations": [],
                "risks": [],
                "should_alert": action in {"alert", "reduce", "sell", "avoid"},
            }
        return suggestions

    def _parse_structured_suggestions(self, structured: dict, context: AgentContext) -> dict[str, dict]:
        out: dict[str, dict] = {}
        items = structured.get("suggestions")
        if not isinstance(items, list):
            return out

        watch_symbols = {stock.symbol for stock in context.watchlist}
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip()
            if symbol not in watch_symbols:
                continue
            action = str(item.get("action") or "watch").strip().lower()
            action_label = str(item.get("action_label") or ACTION_LABELS.get(action, "关注")).strip()
            out[symbol] = {
                "action": action,
                "action_label": action_label or ACTION_LABELS.get(action, "关注"),
                "signal": str(item.get("signal") or "").strip()[:60],
                "reason": str(item.get("reason") or "").strip()[:160],
                "triggers": item.get("triggers") if isinstance(item.get("triggers"), list) else [],
                "invalidations": item.get("invalidations") if isinstance(item.get("invalidations"), list) else [],
                "risks": item.get("risks") if isinstance(item.get("risks"), list) else [],
                "should_alert": action in {"alert", "reduce", "sell", "avoid"},
            }
        return out

    def _fallback_markdown(self, context: AgentContext, data: dict, error: str = "") -> str:
        lines = ["# 新闻分析简报", ""]
        if error:
            lines.extend([f"> AI 生成失败，已回退为规则摘要：{error}", ""])

        lines.extend(
            [
                "## 总览",
                f"- 市场摘要：{data.get('summary') or '暂无明显主线'}",
                f"- 情绪：{data.get('sentiment') or 'neutral'}",
            ]
        )
        topics = [str(topic.get("name") or "") for topic in data.get("topics") or [] if topic.get("name")]
        if topics:
            lines.append(f"- 主题：{' / '.join(topics[:8])}")
        coverage = data.get("coverage") or {}
        lines.append(
            "- 覆盖："
            f"文章 {coverage.get('total_articles', 0)} 篇，"
            f"关联 {coverage.get('related_articles', 0)} 篇，"
            f"启用源 {coverage.get('enabled_sources', 0)} 个"
        )

        lines.extend(["", "## 自选/持仓关联新闻"])
        related = data.get("related_news") or []
        if related:
            for item in related[:8]:
                lines.append(f"- {item.title}：{(item.cn_summary or item.summary or item.content[:120] or '').strip()}")
        else:
            lines.append("- 暂无直接关联新闻")

        lines.extend(["", "## 全市场重点新闻"])
        global_items = [
            item
            for item in (data.get("news") or [])
            if item not in related
        ]
        if global_items:
            for item in global_items[:8]:
                lines.append(f"- {item.title}：{(item.cn_summary or item.summary or item.content[:120] or '').strip()}")
        else:
            lines.append("- 暂无全市场重点新闻")

        lines.extend(["", "## 个股建议摘要"])
        for stock in context.watchlist:
            suggestion = self._default_suggestions(context, data).get(stock.symbol)
            if not suggestion:
                continue
            lines.append(
                f"- [{stock.symbol}] {suggestion['action_label']}：{suggestion['reason']}"
            )

        lines.extend(["", "以上内容由 AI 生成，仅供参考，不构成投资建议。"])
        return "\n".join(lines)

    async def analyze(self, context: AgentContext, data: dict) -> AnalysisResult:
        system_prompt, user_content = self.build_prompt(data, context)
        content = ""
        structured: dict = {}

        try:
            content = await context.ai_client.chat(system_prompt, user_content)
        except Exception as exc:
            logger.warning("news_digest ai analyze failed, using fallback summary: %s", exc)
            content = self._fallback_markdown(context, data, str(exc))

        structured = try_extract_tagged_json(content) or {}
        display_content = strip_tagged_json(content).strip() or self._fallback_markdown(context, data)

        if context.model_label:
            idx = display_content.rfind(TAG_START)
            if idx >= 0:
                display_content = (
                    display_content[:idx].rstrip()
                    + f"\n\n---\nAI: {context.model_label}\n\n"
                    + display_content[idx:]
                )
            else:
                display_content = display_content.rstrip() + f"\n\n---\nAI: {context.model_label}"

        suggestions = self._default_suggestions(context, data)
        suggestions.update(self._parse_structured_suggestions(structured, context))

        title = self._build_title(context)
        raw_data = {
            "timestamp": data.get("timestamp"),
            "summary": data.get("summary") or "",
            "topic_summary": data.get("summary") or "",
            "topics": data.get("topics") or [],
            "sentiment": data.get("sentiment") or "neutral",
            "coverage": data.get("coverage") or {},
            "source_statuses": data.get("source_statuses") or [],
            "related_count": len(data.get("related_news") or []),
            "important_count": len(data.get("important_news") or []),
            "lookback_hours": self.lookback_hours,
            "top_n_for_ai": self.top_n_for_ai,
            "news": self._payload_news(data.get("news") or []),
            "suggestions": suggestions,
            "prompt_context": user_content[:4000],
        }
        if structured:
            raw_data["structured"] = structured

        save_analysis(
            agent_name=self.name,
            stock_symbol="*",
            content=display_content,
            title=title,
            raw_data=raw_data,
        )

        stock_map = {stock.symbol: stock for stock in context.watchlist}
        for symbol, suggestion in suggestions.items():
            stock = stock_map.get(symbol)
            if not stock:
                continue
            save_suggestion(
                stock_symbol=symbol,
                stock_name=stock.name,
                action=suggestion.get("action", "watch"),
                action_label=suggestion.get("action_label", "关注"),
                signal=suggestion.get("signal", ""),
                reason=suggestion.get("reason", ""),
                agent_name=self.name,
                agent_label=self.display_name,
                expires_hours=12,
                prompt_context=user_content,
                ai_response=display_content,
                stock_market=stock.market.value,
                meta={
                    "source": "news_digest",
                    "lookback_hours": self.lookback_hours,
                    "related_count": len(data.get("related_news") or []),
                    "important_count": len(data.get("important_news") or []),
                    "triggers": suggestion.get("triggers") or [],
                    "invalidations": suggestion.get("invalidations") or [],
                    "risks": suggestion.get("risks") or [],
                },
            )

        return AnalysisResult(
            agent_name=self.name,
            title=title,
            content=display_content,
            raw_data=raw_data,
        )

    async def should_notify(self, result: AnalysisResult) -> bool:
        raw = result.raw_data or {}
        return bool(raw.get("related_count") or raw.get("important_count"))
