"""Evaluation commands deliberately bypass production run/delivery persistence."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from .config import Limits, load_profile
from .evaluation import (
    code_fingerprint,
    digest,
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
    report_text = render_evaluation_report(snapshot, result, review, metrics)
    from .evaluation_judge import load_judge, render_judge

    judge = load_judge(directory, snapshot, result)
    if judge:
        metrics["automated_source_support"] = judge["metrics"]
        (directory / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        report_text += render_judge(judge)
    provenance = directory / "provenance.json"
    if provenance.exists():
        source = json.loads(provenance.read_text())
        if source.get("kind") == "ratings_only":
            report_text += (
                f"\nPrior article ratings only were carried from evaluation {source['source_evaluation_id']}; "
                "all condition outputs were generated under this revision. Reassess carried ratings if needs changed. "
                "New selections and new-summary claims remain unreviewed until explicitly rated. See provenance.json.\n"
            )
        else:
            report_text += (
                f"\nBaseline reuse: generic/current outputs and prior article ratings were preserved from evaluation "
                f"{source['source_evaluation_id']}. New article ratings and corrected-summary claim checks remain pending. "
                "Reassess carried ratings if your reading needs changed. See provenance.json.\n"
            )
    (directory / "report.md").write_text(report_text, encoding="utf-8")
    return directory / "report.md"


async def replay_command(args):
    if args.profile:
        raise ValueError(
            "Replay profiles are frozen; use --eval-corrected-profile for a third condition"
        )
    previous_result = previous_review = None
    seeded_review = None
    if args.eval_seed_review:
        seeded_review = EvaluationReview.model_validate_json(args.eval_seed_review.read_text())
    if args.eval_add_corrected:
        if not args.eval_corrected_profile:
            raise ValueError("--eval-add-corrected requires --eval-corrected-profile")
        snapshot = load_snapshot(args.eval_add_corrected / "snapshot.json")
        previous_result = load_result(args.eval_add_corrected / "result.json")
        previous_review = EvaluationReview.model_validate_json(
            (args.eval_add_corrected / "review.json").read_text()
        )
        calculate_metrics(previous_result, previous_review)
        if (
            previous_result.snapshot_id != snapshot.snapshot_id
            or previous_result.code_fingerprint != snapshot.code_fingerprint
        ):
            raise ValueError("Baseline result does not match its snapshot")
        if {c.name for c in previous_result.conditions} != {"generic", "current"}:
            raise ValueError("Select the original two-condition baseline to extend")
    else:
        snapshot = load_snapshot(args.eval_replay)
    if seeded_review:
        source_snapshot = load_snapshot(args.eval_seed_review.parent / "snapshot.json")
        source_result = load_result(args.eval_seed_review.parent / "result.json")
        calculate_metrics(source_result, seeded_review)
        if {a.id: a.content_hash for a in source_snapshot.articles} != {
            a.id: a.content_hash for a in snapshot.articles
        }:
            raise ValueError("Carried ratings must refer to the same frozen article bodies")
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
    if previous_result:
        result = previous_result.model_copy(
            deep=True,
            update={
                "evaluation_id": uuid4(),
                "result_digest": "",
                "started_at": datetime.now(UTC),
                "completed_at": None,
                "status": "running",
            },
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
        "Preserving the original baseline outputs and human ratings; running only the corrected condition."
        if previous_result
        else f"Replaying frozen articles for {'three' if corrected else 'two'} conditions. "
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
        if seeded_review:
            old = {r.article_id: r for r in seeded_review.articles}
            used = {r.review_code for r in seeded_review.articles}
            for index, row in enumerate(review.articles):
                if row.article_id in old:
                    review.articles[index] = old[row.article_id].model_copy(deep=True)
                    review.articles[
                        index
                    ].notes += f"\nPrior human rating from {seeded_review.evaluation_id}; reassess if needs changed."
                else:
                    next_code = 1
                    while f"A{next_code:02d}" in used:
                        next_code += 1
                    row.review_code = f"A{next_code:02d}"
                    used.add(row.review_code)
            (directory / "provenance.json").write_text(
                json.dumps(
                    {
                        "kind": "ratings_only",
                        "source_evaluation_id": str(seeded_review.evaluation_id),
                        "source_review_digest": digest(seeded_review),
                    },
                    indent=2,
                )
            )
        if previous_review:
            old = {r.article_id: r for r in previous_review.articles}
            used = {r.review_code for r in previous_review.articles}
            for index, row in enumerate(review.articles):
                if row.article_id in old:
                    review.articles[index] = old[row.article_id].model_copy(deep=True)
                else:
                    next_code = 1
                    while f"A{next_code:02d}" in used:
                        next_code += 1
                    row.review_code = f"A{next_code:02d}"
                    used.add(row.review_code)
            old_metadata = {r.article_id: r for r in previous_review.metadata}
            review.metadata = [
                old_metadata.get(r.article_id, r).model_copy(deep=True) for r in review.metadata
            ]
            corrected_claims = [
                c.model_copy(update={"claim_id": f"corrected:{c.claim_id}"})
                for c in review.claims
                if c.condition == "corrected"
            ]
            review.claims = [
                c.model_copy(deep=True) for c in previous_review.claims
            ] + corrected_claims
            review.claim_inventory_checked.update(previous_review.claim_inventory_checked)
            review.reviewer = previous_review.reviewer
            (directory / "provenance.json").write_text(
                json.dumps(
                    {
                        "source_evaluation_id": str(previous_result.evaluation_id),
                        "source_result_digest": previous_result.result_digest,
                        "source_review_digest": digest(previous_review),
                        "note": "Original generic/current outputs and ratings preserved. Reassess carried article ratings if reading needs changed; no new-claim accuracy labels were inferred.",
                    },
                    indent=2,
                )
            )
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
    if args.eval_judge:
        from .evaluation_judge import judge_command

        return await judge_command(args)
    if args.eval_freeze:
        return freeze_command(args)
    if args.eval_replay or args.eval_add_corrected:
        return await replay_command(args)
    path = write_report(args.eval_report)
    print(f"Recalculated from saved outputs, human review, and any saved judge results: {path}")
    return 0
