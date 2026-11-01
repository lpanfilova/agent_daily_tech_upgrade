"""Evaluation commands deliberately bypass production run/delivery persistence."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from .config import Limits, load_profile
from .evaluation import (
    code_fingerprint,
    freeze_report,
    load_result,
    load_snapshot,
    replay,
    save_snapshot,
    seal_result,
)
from .evaluation_models import EvaluationResult, EvaluationReview
from .evaluation_review import (
    calculate_metrics,
    create_review,
    render_evaluation_report,
    render_review_guide,
)
from .llm import OpenAIModel
from .models import RunReport
from .pipeline import render_brief
from .storage import Repository


def freeze_command(args):
    source = args.eval_freeze
    profile = load_profile(args.profile) if args.profile else None
    if source != "latest" and Path(source).is_file():
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        if "run_id" not in data:
            data["run_id"] = str(
                uuid5(NAMESPACE_URL, hashlib.sha256(Path(source).read_bytes()).hexdigest())
            )
        report = RunReport.model_validate(data)
        origin = str(Path(source))
    else:
        if args.no_db:
            raise ValueError("Use a run.json file to freeze without a database")
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            raise ValueError("Set DATABASE_URL, or freeze an existing run.json file")
        with Repository(database_url) as repo:
            repo.require_migrations()
            report = repo.latest_report() if source == "latest" else repo.get_report(UUID(source))
            if report is None:
                raise ValueError("No stored brief is available to freeze")
            profile = profile or repo.get_profile()
        origin = f"database:{report.run_id}"
    recorded_models = [u["model"] for u in report.model_usage if u.get("model")]
    model = args.model or (recorded_models[0] if recorded_models else os.getenv("OPENAI_MODEL", ""))
    snapshot = freeze_report(
        report,
        origin=origin,
        model=model,
        profile=profile,
        limits=Limits(days=report.days, runtime_seconds=args.runtime_seconds),
    )
    path = save_snapshot(snapshot, args.output)
    print(
        f"Frozen {len(snapshot.articles)} retrieved articles and a profile snapshot.\nSnapshot: {path}"
    )
    print("No model calls, production writes, or Telegram sends occurred.")
    return 0


def write_report(directory: Path):
    snapshot = load_snapshot(directory / "snapshot.json")
    result = load_result(directory / "result.json")
    if (
        result.snapshot_id != snapshot.snapshot_id
        or result.model != snapshot.model
        or result.code_fingerprint != snapshot.code_fingerprint
    ):
        raise ValueError("Evaluation results do not match the copied snapshot")
    review = EvaluationReview.model_validate_json(
        (directory / "review.json").read_text(encoding="utf-8")
    )
    metrics = calculate_metrics(result, review)
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (directory / "report.md").write_text(
        render_evaluation_report(snapshot, result, review, metrics), encoding="utf-8"
    )
    return directory / "report.md"


async def replay_command(args):
    if args.profile:
        raise ValueError(
            "Replay profiles are frozen; use --eval-corrected-profile for a third condition"
        )
    snapshot = load_snapshot(args.eval_replay)
    if snapshot.code_fingerprint != code_fingerprint():
        raise ValueError(
            "Editorial code differs from the snapshot; export a new snapshot for this revision"
        )
    if args.model and args.model != snapshot.model:
        raise ValueError("Replay model is frozen; export a new snapshot to use another model")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError(
            "Set OPENAI_API_KEY to replay; freezing and report calculation need no key"
        )
    corrected = load_profile(args.eval_corrected_profile) if args.eval_corrected_profile else None
    result = EvaluationResult(
        snapshot_id=snapshot.snapshot_id,
        model=snapshot.model,
        code_fingerprint=snapshot.code_fingerprint,
        started_at=datetime.now(UTC),
        summary_profile=snapshot.profile,
    )
    directory = (
        args.output
        / "evaluations"
        / snapshot.snapshot_id[:16]
        / "attempts"
        / str(result.evaluation_id)
    )
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "snapshot.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"Replaying frozen articles for {'three' if corrected else 'two'} conditions. "
        f"At most {snapshot.limits.max_model_calls} model calls per condition; no delivery or production writes.",
        flush=True,
    )
    try:
        async with httpx.AsyncClient() as http:
            await replay(
                snapshot,
                result,
                lambda: OpenAIModel(http, api_key, snapshot.model, snapshot.limits),
                corrected_profile=corrected,
            )
    except asyncio.CancelledError:
        result.status = "interrupted"
        raise
    except Exception:
        result.status = "partial"
        raise
    finally:
        seal_result(result)
        (directory / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        review = create_review(result)
        (directory / "review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")
        (directory / "review_guide.md").write_text(
            render_review_guide(result, review), encoding="utf-8"
        )
        for condition in result.conditions:
            (directory / f"{condition.name}_brief.md").write_text(
                f"Evaluation-only condition: {condition.name}. Summary audience/style held fixed.\n\n"
                + render_brief(condition.report),
                encoding="utf-8",
            )
        path = write_report(directory)
        print(
            f"Evaluation report: {path}\nHuman judgments: {directory / 'review.json'}", flush=True
        )
    for condition in result.conditions:
        print(
            f"{condition.name}: {condition.report.status}, {len(condition.report.recommendations)}/3 articles"
        )
    return 0 if result.status == "complete" else 2


async def execute_evaluation(args):
    if args.eval_freeze:
        return freeze_command(args)
    if args.eval_replay:
        return await replay_command(args)
    path = write_report(args.eval_report)
    print(f"Recalculated from saved outputs and human review only: {path}")
    return 0
