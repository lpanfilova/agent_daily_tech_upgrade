import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_brief import PROFILE, FakeModel, article

from daily_tech.cli import execute, parser
from daily_tech.evaluation import (
    freeze_report,
    load_result,
    load_snapshot,
    replay,
    save_snapshot,
    seal_result,
)
from daily_tech.evaluation_cli import write_report
from daily_tech.evaluation_models import ArticleReview, EvaluationResult
from daily_tech.evaluation_review import calculate_metrics, create_review, render_evaluation_report
from daily_tech.llm import ModelError
from daily_tech.models import RunReport


def frozen():
    articles = [article(id, id.upper()) for id in "abcd"]
    for item in articles:
        item.text += f" Unique article {item.id}."
        item.content_hash = hashlib.sha256(" ".join(item.text.split()).encode()).hexdigest()
    source = RunReport(
        started_at=datetime.now(UTC), status="complete", days=7, profile=PROFILE, articles=articles
    )
    return freeze_report(source, origin="synthetic", model="test-model"), source


class EvaluationModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.profiles = []

    async def generate(self, instructions, data, schema):
        self.profiles.append(data.get("profile"))
        if "articles" in data:
            self.usage.append({"usage": {"input_tokens": 10, "output_tokens": 5}})
            return schema.model_validate(
                {
                    a["response_key"]: {
                        "relevance": 1
                        if data["profile"] and a["response_key"] == "article_0"
                        else 5,
                        "quality": 4,
                        "topic": a["response_key"],
                        "reason": "Synthetic review",
                    }
                    for a in data["articles"]
                }
            )
        return await super().generate(instructions, data, schema)


def comparison(snapshot, factory=EvaluationModel, corrected=None):
    result = EvaluationResult(
        snapshot_id=snapshot.snapshot_id,
        model=snapshot.model,
        code_fingerprint=snapshot.code_fingerprint,
        started_at=datetime.now(UTC),
        summary_profile=snapshot.profile,
    )
    asyncio.run(replay(snapshot, result, factory, corrected_profile=corrected))
    seal_result(result)
    return result


def test_snapshot_roundtrip_detects_modified_inputs_and_keeps_all_articles(tmp_path):
    snapshot, source = frozen()
    path = save_snapshot(snapshot, tmp_path)
    restored = load_snapshot(path)
    assert len(restored.articles) == 4
    source.profile = PROFILE.model_copy(update={"depth": "advanced"})
    assert snapshot.profile.depth == "novice"
    data = json.loads(path.read_text())
    data["articles"][0]["text"] += " tampered"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        load_snapshot(path)


def test_freeze_rejects_bad_source_hash():
    _, source = frozen()
    source.articles[0].content_hash = "wrong"
    with pytest.raises(ValueError, match="content hash"):
        freeze_report(source, origin="synthetic", model="test-model")


def test_replay_varies_ranking_only_and_preserves_original_profile():
    snapshot, source = frozen()
    instances = []

    def factory():
        model = EvaluationModel()
        instances.append(model)
        return model

    corrected = PROFILE.model_copy(update={"interests": ["evaluation harnesses"]})
    result = comparison(snapshot, factory, corrected)
    assert result.status == "complete"
    assert [c.name for c in result.conditions] == ["generic", "current", "corrected"]
    assert instances[0].profiles[0] is None
    assert instances[1].profiles[0] == PROFILE.model_dump()
    assert instances[2].profiles[0] == corrected.model_dump()
    assert all(p == PROFILE.model_dump() for model in instances for p in model.profiles[1:])
    assert source.profile == PROFILE
    assert all(
        len(c.report.model_usage) <= snapshot.limits.max_model_calls for c in result.conditions
    )
    assert {r.article.id for r in result.conditions[0].report.recommendations} == set("abc")
    assert {r.article.id for r in result.conditions[1].report.recommendations} == set("bcd")


def test_editorial_revision_mismatch_prevents_replay(monkeypatch):
    snapshot, _ = frozen()
    monkeypatch.setattr("daily_tech.evaluation.code_fingerprint", lambda: "different")
    with pytest.raises(ValueError, match="differs"):
        comparison(snapshot, lambda: pytest.fail("No model should be created"))


def test_missing_human_review_never_becomes_a_quality_score():
    snapshot, _ = frozen()
    result = comparison(snapshot)
    review = create_review(result)
    metrics = calculate_metrics(result, review)
    current = metrics["conditions"]["current"]
    assert current["selected_slots"] == 3
    assert current["ratings"]["relevance"] == {"mean": None, "rated": 0, "selected": 3}
    assert current["claims"]["supported_rate"] is None
    assert current["metadata"]["verified_rate"] is None
    assert metrics["comparisons"]["current_minus_generic"]["rating_deltas"]["relevance"] is None
    assert "Not measured" in render_evaluation_report(snapshot, result, review, metrics)


def test_review_scores_and_supported_claim_denominators_are_explicit():
    snapshot, _ = frozen()
    result = comparison(snapshot)
    review = create_review(result)
    for row in review.articles:
        row.relevance = {"a": 1, "b": 3, "c": 4, "d": 5}[row.article_id]
        row.difficulty_fit = 3
        row.practical_value = 4
    for row in review.metadata:
        for field in ("title", "publisher", "original_url", "publication_date"):
            getattr(row, field).verdict = "verified"
            getattr(row, field).note = "Synthetic source check"
    review.metadata[-1].publication_date.verdict = "unverified"
    claims = [c for c in review.claims if c.condition == "current"]
    for claim in claims:
        claim.verdict, claim.note = "not_factual", "Synthetic non-factual statement"
    claims[0].verdict = "supported"
    claims[0].source_passage = "An agent harness manages tools and tracks execution state."
    claims[1].verdict = "contradicted"
    claims[2].verdict = "unverifiable"
    metrics = calculate_metrics(result, review)
    assert metrics["conditions"]["current"]["claims"]["supported_rate"] is None
    review.claim_inventory_checked["current"] = True
    metrics = calculate_metrics(result, review)
    assert metrics["conditions"]["current"]["claims"]["supported_rate"] == pytest.approx(1 / 3)
    assert metrics["conditions"]["current"]["metadata"]["verified_rate"] == pytest.approx(11 / 12)
    assert metrics["comparisons"]["current_minus_generic"]["rating_deltas"][
        "relevance"
    ] == pytest.approx(4 / 3)


def test_review_rejects_invented_evidence_and_cross_evaluation_labels():
    snapshot, _ = frozen()
    result = comparison(snapshot)
    review = create_review(result)
    review.claims[0].verdict = "supported"
    review.claims[0].source_passage = "An invented source passage"
    with pytest.raises(ValueError, match="exact passage"):
        calculate_metrics(result, review)
    review.claims[0].verdict = "pending"
    review.articles.append(review.articles[0])
    with pytest.raises(ValueError, match="exactly once"):
        calculate_metrics(result, review)
    with pytest.raises(ValidationError):
        ArticleReview(article_id="a", review_code="A01", relevance=True)


def test_failed_attempts_are_retained_and_do_not_stop_other_conditions():
    snapshot, _ = frozen()

    class FailingModel(EvaluationModel):
        async def generate(self, *args):
            self.usage.append({"status": 401})
            raise ModelError("Synthetic model failure")

    models = iter([FailingModel(), EvaluationModel()])
    result = comparison(snapshot, lambda: next(models))
    assert result.status == "partial"
    assert result.conditions[0].report.status == "failed"
    assert result.conditions[0].report.events[0]["reason"] == "Synthetic model failure"
    metrics = calculate_metrics(result, create_review(result))
    assert metrics["completion"] == {"completed_conditions": 1, "attempted_conditions": 2}
    assert not metrics["conditions"]["generic"]["token_usage_complete"]


def test_cli_freeze_replay_and_report_are_isolated_from_production(monkeypatch, tmp_path):
    snapshot, source = frozen()
    source.model_usage = [{"model": "test-model"}]
    source_file = tmp_path / "run.json"
    source_file.write_text(source.model_dump_json())
    monkeypatch.setattr(
        "daily_tech.cli.Repository", lambda *a: pytest.fail("No production DB connection")
    )
    monkeypatch.setattr(
        "daily_tech.evaluation_cli.Repository",
        lambda *a: pytest.fail("No production DB connection"),
    )
    monkeypatch.setattr("daily_tech.evaluation_cli.OpenAIModel", lambda *a: EvaluationModel())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = parser().parse_args(["--eval-freeze", str(source_file), "--output", str(tmp_path)])
    assert asyncio.run(execute(args)) == 0
    snapshot_path = next(tmp_path.glob("evaluations/*/snapshot.json"))
    args = parser().parse_args(["--eval-replay", str(snapshot_path), "--output", str(tmp_path)])
    assert asyncio.run(execute(args)) == 0
    result_path = next(tmp_path.glob("evaluations/*/attempts/*/result.json"))
    result = load_result(result_path)
    original_bytes = result_path.read_bytes()
    report_path = write_report(result_path.parent)
    assert "Not measured" in report_path.read_text()
    assert result_path.read_bytes() == original_bytes
    assert len(result.conditions) == 2
    data = json.loads(result_path.read_text())
    data["model"] = "modified-model"
    result_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        load_result(result_path)
