import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .config import Limits
from .editorial import choose_group, rank, summarize, topic_key
from .llm import ModelError, StructuredModel
from .models import Rating, Recommendation, RunReport, Source
from .retrieval import Fetcher, discover, event, verify


class State(TypedDict, total=False):
    ratings: list[Rating]


async def run_pipeline(
    report: RunReport,
    sources: list[Source],
    fetcher: Fetcher,
    model: StructuredModel | None,
    limits: Limits,
) -> RunReport:
    async def discovery(state: State) -> dict:
        report.candidates = await discover(sources, fetcher, report.started_at, report.events)
        return {}

    async def verification(state: State) -> dict:
        report.articles = await verify(
            report.candidates, sources, fetcher, report.started_at, report.events
        )
        return {}

    async def ranking(state: State) -> dict:
        report.ratings = await rank(report.articles, report.profile, model)
        return {"ratings": report.ratings}

    async def summarization(state: State) -> dict:
        articles = {a.id: a for a in report.articles}
        publishers: set[str] = set()
        topics: set[str] = set()
        minutes = 0
        remaining = list(state["ratings"])
        attempts = 0
        while remaining and len(report.recommendations) < 3:
            group = choose_group(
                remaining,
                articles,
                3 - len(report.recommendations),
                limits.max_reading_minutes - minutes,
                publishers,
                topics,
            )
            if not group or attempts >= limits.max_model_calls - 1:
                break
            rating = group[0]
            remaining.remove(rating)
            article = articles[rating.article_id]
            topic = topic_key(rating.topic)
            attempts += 1
            try:
                summary = await summarize(article, report.profile, model)
            except (ModelError, ValueError) as exc:
                event(report.events, "summary_rejected", article.url, str(exc))
                continue
            report.recommendations.append(
                Recommendation(article=article, rating=rating, summary=summary)
            )
            publishers.add(article.publisher)
            topics.add(topic)
            minutes += article.reading_minutes
        for rating in remaining:
            reason = (
                rating.reason
                if rating.quality < 3 or rating.relevance < 3
                else "Not selected within diversity, reading-time, or model-call limits"
            )
            event(report.events, "editorial_rejected", articles[rating.article_id].url, reason)
        report.status = "complete" if len(report.recommendations) == 3 else "partial"
        return {}

    builder = StateGraph(State)
    builder.add_node("discover", discovery)
    builder.add_node("verify", verification)
    builder.add_edge(START, "discover")
    builder.add_edge("discover", "verify")
    if model is None:
        builder.add_edge("verify", END)
    else:
        builder.add_node("rank", ranking)
        builder.add_node("summarize", summarization)
        builder.add_edge("verify", "rank")
        builder.add_edge("rank", "summarize")
        builder.add_edge("summarize", END)
    try:
        async with asyncio.timeout(limits.runtime_seconds):
            await builder.compile().ainvoke({})
    except (TimeoutError, ModelError) as exc:
        report.status = "partial" if report.recommendations else "failed"
        event(report.events, "run_error", "pipeline", str(exc) or "Runtime budget exhausted")
    finally:
        report.model_usage = model.usage if model else []
    return report


def render_brief(report: RunReport) -> str:
    date = report.started_at.date().isoformat()
    lines = [
        f"# Daily Tech Upgrade — {date}",
        "",
        f"Interests: {', '.join(report.profile.interests)}. Level: {report.profile.depth}.",
        "",
    ]
    if report.status == "discovery_only":
        lines.extend(
            [
                "Discovery only: no model was called and no brief was generated.",
                "",
                f"Retrieved {len(report.articles)} readable articles:",
                "",
            ]
        )
        for a in report.articles:
            lines.append(
                f"- [{a.title}]({a.url}) — {a.publisher}, "
                f"{a.published_at.date()}, ~{a.reading_minutes} min"
            )
    else:
        minutes = sum(r.article.reading_minutes for r in report.recommendations)
        lines.extend(
            [
                f"Status: {report.status}. Selected {len(report.recommendations)}/3 articles; "
                f"~{minutes} minutes of article reading at 220 words/minute.",
                "",
            ]
        )
        if len(report.recommendations) < 3:
            lines.extend(
                [
                    "Fewer than three articles met the content, relevance, diversity, "
                    "reading-time, and execution limits. See run.json for details.",
                    "",
                ]
            )
        for i, rec in enumerate(report.recommendations, 1):
            a, s = rec.article, rec.summary
            lines.extend(
                [
                    f"## {i}. {a.title}",
                    "",
                    f"{a.publisher} · Published {a.published_at.date()} · ~{a.reading_minutes} min",
                    "",
                    f"[Original article]({a.url})",
                    "",
                    s.summary,
                    "",
                    "Technical insights:",
                    "",
                ]
            )
            lines.extend(f"- {insight.claim}" for insight in s.insights)
            lines.extend(["", f"**Why it matters:** {s.why_it_matters}", ""])
    lines.extend(
        [
            "",
            "Publication dates come from publisher feeds and are compared with page "
            "dates when available. Reading times are estimates. "
            "Summaries are AI-generated; evidence excerpts are retained in run.json.",
            "",
        ]
    )
    return "\n".join(lines)


def save_report(report: RunReport, output: Path) -> Path:
    run_dir = output / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "run.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (run_dir / "brief.md").write_text(render_brief(report), encoding="utf-8")
    return run_dir
