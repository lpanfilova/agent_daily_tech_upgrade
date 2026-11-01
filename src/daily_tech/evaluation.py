"""Freeze and replay editorial inputs without production writes or Telegram delivery."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from . import editorial, llm, models, pipeline
from .config import Limits
from .editorial import rank
from .evaluation_models import (
    EvaluationCondition,
    EvaluationResult,
    EvaluationSnapshot,
)
from .llm import ModelError
from .models import Profile, RunReport
from .pipeline import summarize_ranked
from .retrieval import canonical_url, event


def digest(model, *, exclude=frozenset()) -> str:
    value = json.dumps(
        model.model_dump(mode="json", exclude=set(exclude)),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def code_fingerprint() -> str:
    value = b"".join(
        Path(module.__file__).read_bytes() for module in (editorial, llm, models, pipeline)
    )
    return hashlib.sha256(value).hexdigest()


def validate_articles(snapshot: EvaluationSnapshot):
    if len(snapshot.articles) > snapshot.limits.max_articles:
        raise ValueError("Snapshot exceeds the configured article limit")
    if len({a.id for a in snapshot.articles}) != len(snapshot.articles):
        raise ValueError("Snapshot has duplicate article IDs")
    if len({canonical_url(a.url) for a in snapshot.articles}) != len(snapshot.articles):
        raise ValueError("Snapshot has duplicate canonical URLs")
    for article in snapshot.articles:
        expected = hashlib.sha256(" ".join(article.text.split()).encode()).hexdigest()
        if article.content_hash != expected:
            raise ValueError(f"Article content hash differs: {article.id}")
        if not article.text.strip() or len(article.text) > snapshot.limits.max_text_chars:
            raise ValueError("Snapshot article text is empty or exceeds the input budget")


def freeze_report(
    source: RunReport,
    *,
    origin: str,
    model: str,
    profile: Profile | None = None,
    limits: Limits | None = None,
) -> EvaluationSnapshot:
    if not model.strip():
        raise ValueError("A replay model name must be captured in the snapshot")
    snapshot = EvaluationSnapshot(
        snapshot_id="unsealed",
        frozen_at=datetime.now(UTC),
        eligibility_as_of=source.started_at,
        source_run_id=source.run_id,
        source_origin=origin,
        profile=(profile or source.profile).model_copy(deep=True),
        model=model,
        limits=limits or Limits(days=source.days),
        code_fingerprint=code_fingerprint(),
        articles=[a.model_copy(deep=True) for a in source.articles],
    )
    validate_articles(snapshot)
    snapshot.snapshot_id = digest(snapshot, exclude={"snapshot_id"})
    return snapshot


def load_snapshot(path: Path) -> EvaluationSnapshot:
    snapshot = EvaluationSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    if snapshot.snapshot_id != digest(snapshot, exclude={"snapshot_id"}):
        raise ValueError(
            "Snapshot integrity check failed; export a new snapshot rather than editing it"
        )
    validate_articles(snapshot)
    return snapshot


def save_snapshot(snapshot: EvaluationSnapshot, output: Path) -> Path:
    directory = output / "evaluations" / snapshot.snapshot_id[:16]
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / "snapshot.json"
    target.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return target


async def replay(
    snapshot: EvaluationSnapshot,
    result: EvaluationResult,
    model_factory,
    *,
    corrected_profile: Profile | None = None,
):
    if snapshot.code_fingerprint != code_fingerprint():
        raise ValueError(
            "Editorial code differs from the snapshot; export a new snapshot for this revision"
        )
    conditions = [("generic", None), ("current", snapshot.profile)]
    if corrected_profile is not None:
        conditions.append(("corrected", corrected_profile))
    for name, ranking_profile in conditions:
        report = RunReport(
            started_at=datetime.now(UTC),
            status="failed",
            profile=snapshot.profile,
            days=snapshot.limits.days,
            articles=[a.model_copy(deep=True) for a in snapshot.articles],
        )
        condition = EvaluationCondition(name=name, ranking_profile=ranking_profile, report=report)
        result.conditions.append(condition)
        model = model_factory()
        started = perf_counter()
        try:
            async with asyncio.timeout(snapshot.limits.runtime_seconds):
                report.ratings = await rank(report.articles, ranking_profile, model)
                # Hold the summary audience/style fixed: this comparison measures selection.
                await summarize_ranked(report, model, snapshot.limits, snapshot.profile)
        except (ModelError, TimeoutError) as exc:
            report.status = "partial" if report.recommendations else "failed"
            event(report.events, "run_error", name, str(exc) or "Runtime budget exhausted")
        except asyncio.CancelledError:
            result.status = "interrupted"
            report.status = "partial" if report.recommendations else "failed"
            event(report.events, "run_error", name, "Evaluation interrupted")
            raise
        finally:
            condition.elapsed_seconds = perf_counter() - started
            report.completed_at = datetime.now(UTC)
            report.model_usage = model.usage
    result.status = (
        "complete" if all(c.report.status == "complete" for c in result.conditions) else "partial"
    )


def seal_result(result: EvaluationResult):
    result.completed_at = datetime.now(UTC)
    result.result_digest = digest(result, exclude={"result_digest"})


def load_result(path: Path) -> EvaluationResult:
    result = EvaluationResult.model_validate_json(path.read_text(encoding="utf-8"))
    if result.result_digest != digest(result, exclude={"result_digest"}):
        raise ValueError("Evaluation output integrity check failed")
    return result
