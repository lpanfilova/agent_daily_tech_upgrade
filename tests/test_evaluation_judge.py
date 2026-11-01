import asyncio
import json

import pytest
from test_evaluation import comparison, frozen

from daily_tech.cli import execute, parser
from daily_tech.evaluation import digest
from daily_tech.evaluation_judge import (
    SupportVerdict,
    judge_metrics,
    judge_result,
    load_judge,
    render_judge,
    save_artifact,
)
from daily_tech.evaluation_review import create_review
from daily_tech.llm import ModelError


class FakeJudge:
    def __init__(self, mode="valid"):
        self.mode = mode
        self.usage = []

    async def generate(self, instructions, data, schema):
        assert "profile" not in data and "condition" not in data
        self.usage.append({"usage": {"input_tokens": 3, "output_tokens": 5}})
        if self.mode == "failure":
            raise ModelError("Synthetic failure")
        evidence_id = next(iter(data["source_passages"]))
        values = {
            key: {
                "verdict": "supported",
                "evidence_ids": [] if self.mode == "missing" else [evidence_id],
                "reason": "Synthetic judgment",
            }
            for key in data["statements"]
        }
        if self.mode == "fabricated":
            # Bypass the mock's schema to exercise the application's defense as well.
            return schema.model_construct(
                **{
                    key: SupportVerdict(**{**value, "evidence_ids": ["absent"]})
                    for key, value in values.items()
                }
            )
        return schema.model_validate(values)


def artifact_for(snapshot, result):
    return {
        "judge_id": "00000000-0000-0000-0000-000000000001",
        "snapshot_id": snapshot.snapshot_id,
        "source_result_digest": result.result_digest,
        "inventory_digest": digest(create_review(result)),
        "groups": [],
        "usage": [],
        "rubric_version": 1,
        "model": "test-model",
        "same_model_as_generator": True,
    }


@pytest.mark.parametrize("mode", ["valid", "fabricated", "missing", "failure"])
def test_judge_validates_source_evidence_and_failure_coverage(mode):
    snapshot, _ = frozen()
    result = comparison(snapshot)
    before = result.model_dump_json()
    artifact = artifact_for(snapshot, result)
    asyncio.run(judge_result(snapshot, result, FakeJudge(mode), artifact))
    assert result.model_dump_json() == before
    for row in artifact["metrics"].values():
        if mode == "failure":
            assert row["estimated_support_rate"] is None
            assert not row["complete"]
        else:
            assert row["complete"]
            assert row["estimated_support_rate"] == (1 if mode == "valid" else 0)
            assert row["validation_flags"] == (0 if mode == "valid" else row["expected_statements"])
    assert "not real-world factual accuracy" in render_judge(artifact)


def test_judge_artifact_bound_to_outputs_and_detects_tampering(tmp_path):
    snapshot, _ = frozen()
    result = comparison(snapshot)
    artifact = artifact_for(snapshot, result)
    asyncio.run(judge_result(snapshot, result, FakeJudge(), artifact))
    path = tmp_path / "judges" / artifact["judge_id"] / "result.json"
    path.parent.mkdir(parents=True)
    save_artifact(path, artifact)
    (tmp_path / "judge_latest.json").write_text(json.dumps({"judge_id": artifact["judge_id"]}))
    assert load_judge(tmp_path, snapshot, result)["metrics"] == judge_metrics(artifact, result)
    changed = json.loads(path.read_text())
    changed["model"] = "changed"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="integrity"):
        load_judge(tmp_path, snapshot, result)


def test_judge_cli_preserves_human_ratings_and_has_no_production_side_effects(
    tmp_path, monkeypatch
):
    snapshot, _ = frozen()
    result = comparison(snapshot)
    review = create_review(result)
    review.articles[0].relevance = 4
    for name, data in [("snapshot", snapshot), ("result", result), ("review", review)]:
        (tmp_path / f"{name}.json").write_text(data.model_dump_json(indent=2))
    original = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("daily_tech.evaluation_judge.OpenAIModel", lambda *a: FakeJudge())

    def forbidden(*a, **k):
        raise AssertionError("Production operation during evaluation")

    monkeypatch.setattr("daily_tech.cli.Repository", forbidden)
    monkeypatch.setattr("daily_tech.evaluation_cli.Repository", forbidden)
    monkeypatch.setattr("daily_tech.cli.TelegramClient", forbidden)
    args = parser().parse_args(["--eval-judge", str(tmp_path)])
    assert asyncio.run(execute(args)) == 0
    assert all((tmp_path / name).read_bytes() == data for name, data in original.items())
    assert "Automated source-support estimates" in (tmp_path / "report.md").read_text()
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert "automated_source_support" in metrics
    assert metrics["conditions"]["generic"]["claims"]["supported_rate"] is None
    # Report-only regeneration retains model results without another API call.
    monkeypatch.setattr("daily_tech.evaluation_judge.OpenAIModel", forbidden)
    assert asyncio.run(execute(parser().parse_args(["--eval-report", str(tmp_path)]))) == 0
    assert "Automated source-support estimates" in (tmp_path / "report.md").read_text()


def test_denominator_excludes_subjective_only_and_partial_group_suppresses_rate():
    snapshot, _ = frozen()
    result = comparison(snapshot)
    artifact = artifact_for(snapshot, result)
    asyncio.run(judge_result(snapshot, result, FakeJudge(), artifact))
    group = artifact["groups"][0]
    group["claims"][0]["verdict"] = "not_factual"
    row = judge_metrics(artifact, result)[group["condition"]]
    assert row["factual_statements"] == row["expected_statements"] - 1
    assert row["estimated_support_rate"] == 1
    group["status"] = "failed"
    row = judge_metrics(artifact, result)[group["condition"]]
    assert row["estimated_support_rate"] is None
    assert not row["complete"]
