"""Human review templates and transparent metric calculations."""

import re
from collections import Counter
from statistics import mean

from .evaluation_models import (
    ArticleReview,
    ClaimReview,
    EvaluationResult,
    EvaluationReview,
    MetadataReview,
)

METRICS = ("relevance", "difficulty_fit", "practical_value")
METADATA_FIELDS = ("title", "publisher", "original_url", "publication_date")


def selected_articles(result):
    return {r.article.id: r.article for c in result.conditions for r in c.report.recommendations}


def create_review(result: EvaluationResult) -> EvaluationReview:
    articles = selected_articles(result)
    codes = {id: f"A{i:02d}" for i, id in enumerate(sorted(articles), 1)}
    claims = []
    for condition in result.conditions:
        for rec in condition.report.recommendations:
            sections = [
                ("summary", rec.summary.summary),
                *(("insight", i.claim) for i in rec.summary.insights),
                ("relevance", rec.summary.why_it_matters),
            ]
            for section, text in sections:
                # Draft inventory only. A human must split compound claims and check coverage.
                for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip()):
                    claims.append(
                        ClaimReview(
                            claim_id=f"C{len(claims) + 1:03d}",
                            condition=condition.name,
                            article_id=rec.article.id,
                            section=section,
                            text=sentence,
                        )
                    )
    target = next((c.ranking_profile for c in result.conditions if c.name == "corrected"), None)
    return EvaluationReview(
        evaluation_id=result.evaluation_id,
        snapshot_id=result.snapshot_id,
        rating_target_profile=target or result.summary_profile,
        articles=[ArticleReview(article_id=id, review_code=codes[id]) for id in sorted(articles)],
        metadata=[MetadataReview(article_id=id) for id in sorted(articles)],
        claim_inventory_checked={c.name: False for c in result.conditions},
        claims=claims,
    )


def validate_review(result, review):
    if (review.evaluation_id, review.snapshot_id) != (result.evaluation_id, result.snapshot_id):
        raise ValueError("Review belongs to a different evaluation")
    articles = selected_articles(result)
    for rows in (review.articles, review.metadata):
        ids = [r.article_id for r in rows]
        if len(ids) != len(set(ids)) or set(ids) != set(articles):
            raise ValueError("Review must contain each selected article exactly once")
    if len({r.review_code for r in review.articles}) != len(review.articles):
        raise ValueError("Article review codes must be unique")
    conditions = {c.name for c in result.conditions}
    if set(review.claim_inventory_checked) != conditions:
        raise ValueError("Claim inventory flags must match the evaluation conditions")
    pairs = {(c.name, r.article.id) for c in result.conditions for r in c.report.recommendations}
    ids = [c.claim_id for c in review.claims]
    if len(ids) != len(set(ids)):
        raise ValueError("Claim IDs must be unique")
    for claim in review.claims:
        if (claim.condition, claim.article_id) not in pairs:
            raise ValueError("Claim refers to an unselected article or unknown condition")
        if claim.verdict == "supported":
            passage = " ".join(claim.source_passage.split())
            if not passage or passage not in " ".join(articles[claim.article_id].text.split()):
                raise ValueError(
                    f"Supported claim {claim.claim_id} needs an exact passage from its saved source"
                )
        elif claim.verdict != "pending" and not claim.note.strip():
            raise ValueError(f"Reviewed claim {claim.claim_id} needs a review note")
    for row in review.metadata:
        if any(
            getattr(row, name).verdict != "pending" and not getattr(row, name).note.strip()
            for name in METADATA_FIELDS
        ):
            raise ValueError("Reviewed metadata checks need a source/review note")


def calculate_metrics(result: EvaluationResult, review: EvaluationReview) -> dict:
    validate_review(result, review)
    ratings = {r.article_id: r for r in review.articles}
    metadata = {r.article_id: r for r in review.metadata}
    metrics = {"evaluation_id": str(result.evaluation_id), "conditions": {}, "comparisons": {}}
    for condition in result.conditions:
        selected = {r.article.id for r in condition.report.recommendations}
        quality = {}
        for name in METRICS:
            values = [
                getattr(ratings[id], name)
                for id in selected
                if getattr(ratings[id], name) is not None
            ]
            quality[name] = {
                "mean": mean(values) if values else None,
                "rated": len(values),
                "selected": len(selected),
            }
        checks = Counter(
            getattr(metadata[id], field).verdict for id in selected for field in METADATA_FIELDS
        )
        claims = [c for c in review.claims if c.condition == condition.name]
        verdicts = Counter(c.verdict for c in claims)
        reviewed_factual = sum(verdicts[v] for v in ("supported", "contradicted", "unverifiable"))
        inventory_checked = review.claim_inventory_checked[condition.name]
        claim_complete = bool(claims) and inventory_checked and not verdicts["pending"]
        usage = condition.report.model_usage
        tokens = {
            field: sum((u.get("usage") or {}).get(field, 0) for u in usage)
            for field in ("input_tokens", "output_tokens")
        }
        metrics["conditions"][condition.name] = {
            "status": condition.report.status,
            "selected_slots": len(selected),
            "requested_slots": 3,
            "ratings": quality,
            "metadata": {
                "counts": dict(checks),
                "field_checks": 4 * len(selected),
                "verified_rate": checks["verified"] / (4 * len(selected))
                if selected and not checks["pending"]
                else None,
            },
            "claims": {
                "counts": dict(verdicts),
                "inventory_checked": inventory_checked,
                "reviewed_factual": reviewed_factual,
                "supported_rate": verdicts["supported"] / reviewed_factual
                if claim_complete and reviewed_factual
                else None,
            },
            "elapsed_seconds": condition.elapsed_seconds,
            "model_calls": len(usage),
            "tokens": tokens,
            "token_usage_complete": all("usage" in u for u in usage),
        }
    conditions = metrics["conditions"]
    for left, right in (("generic", "current"), ("current", "corrected")):
        if left not in conditions or right not in conditions:
            continue
        deltas = {}
        for name in METRICS:
            a, b = (conditions[key]["ratings"][name] for key in (left, right))
            deltas[name] = (
                b["mean"] - a["mean"]
                if (
                    a["mean"] is not None
                    and b["mean"] is not None
                    and a["rated"] == a["selected"]
                    and b["rated"] == b["selected"]
                )
                else None
            )
        a, b = (
            {r.article.id for r in c.report.recommendations}
            for key in (left, right)
            for c in result.conditions
            if c.name == key
        )
        metrics["comparisons"][f"{right}_minus_{left}"] = {
            "rating_deltas": deltas,
            "shared_article_ids": sorted(a & b),
        }
    metrics["completion"] = {
        "completed_conditions": sum(c.report.status == "complete" for c in result.conditions),
        "attempted_conditions": len(result.conditions),
    }
    return metrics


def render_review_guide(result, review):
    articles = selected_articles(result)
    lines = [
        "# Human review worksheet",
        "",
        "Edit review.json. Rate each unique article once against your current reading needs:",
        "",
        "- Relevance, difficulty fit, practical value: 1 poor, 3 mixed, 5 excellent. Leave null if unrated.",
        "- Check each metadata field; add a source/review note. Unverified is distinct from incorrect.",
        "- Claims are sentence drafts, not guaranteed atomic or complete. Split compound claims and add missing claims.",
        "- Mark supported, contradicted, unverifiable, or not_factual. Supported needs an exact saved-source passage; other labels need notes.",
        "- Set claim_inventory_checked for a condition only after checking the entire brief. No support percentage is reported before that.",
        "",
        "Condition labels are omitted below for usefulness ratings. Raw results and claim reviews expose them; this is not a double-blind study.",
        "",
    ]
    for row in review.articles:
        article = articles[row.article_id]
        lines.extend(
            [
                f"## {row.review_code}: {article.title}",
                "",
                f"Article ID: {article.id}",
                "",
                f"{article.publisher} · {article.published_at.date()}",
                "",
                article.url,
                "",
                "Read the saved full source in snapshot.json (articles[].text).",
                "",
            ]
        )
    return "\n".join(lines)


def render_evaluation_report(snapshot, result, review, metrics):
    def fmt(value):
        return "Not measured" if value is None else f"{value:.2f}"

    lines = [
        "# Agent evaluation — initial evidence",
        "",
        f"Evaluation: {result.evaluation_id}",
        f"Snapshot: {result.snapshot_id}",
        f"Eligibility frozen as of: {snapshot.eligibility_as_of.isoformat()}",
        f"Model: {result.model}",
        f"Editorial code: {result.code_fingerprint}",
        f"Human rating target: {review.rating_target_profile.depth}; interests: {', '.join(review.rating_target_profile.interests)}.",
        "",
        "The corpus, reading budget, selection policy, and summary profile are fixed. Only ranking preferences vary.",
        "This is a small, single-user descriptive comparison. Outputs may vary between replays; no statistical or causal claim is made.",
        f"Editorial completion: {metrics['completion']['completed_conditions']}/{metrics['completion']['attempted_conditions']} attempted conditions. Timings exclude discovery and delivery.",
        "",
        "| Condition | Outcome | Slots | Relevance | Difficulty fit | Practical value | Rated articles* | Seconds |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, row in metrics["conditions"].items():
        ratings = row["ratings"]
        complete = sum(
            all(getattr(r, metric) is not None for metric in METRICS)
            for r in review.articles
            if r.article_id
            in {
                rec.article.id
                for c in result.conditions
                if c.name == name
                for rec in c.report.recommendations
            }
        )
        lines.append(
            f"| {name} | {row['status']} | {row['selected_slots']}/3 | "
            f"{fmt(ratings['relevance']['mean'])} | {fmt(ratings['difficulty_fit']['mean'])} | "
            f"{fmt(ratings['practical_value']['mean'])} | {complete}/{row['selected_slots']} | {row['elapsed_seconds']:.2f} |"
        )
    lines.extend(
        [
            "",
            "*Rated articles have all three human scores. Partial averages, where present, use only available ratings; per-metric counts are in metrics.json.",
            "",
        ]
    )
    for condition in result.conditions:
        row = metrics["conditions"][condition.name]
        lines.extend(
            [
                f"## {condition.name}",
                "",
                f"Human claim-support rate: {fmt(row['claims']['supported_rate'])}. Labels: {row['claims']['counts']}.",
                f"Human metadata verification rate: {fmt(row['metadata']['verified_rate'])}. Labels: {row['metadata']['counts']}.",
                f"Model calls: {row['model_calls']}; returned tokens: {row['tokens']}; usage complete: {row['token_usage_complete']}.",
                "",
            ]
        )
        lines.extend(
            f"- {r.article.title} — {r.article.url}" for r in condition.report.recommendations
        )
        lines.extend(
            [
                "",
                f"Diagnostics: {len(condition.report.events)} recorded events; inspect result.json for rejections and failures.",
                "",
            ]
        )
    lines.extend(["## Comparisons", ""])
    for pair, comparison in metrics["comparisons"].items():
        lines.append(
            f"- {pair}: "
            + ", ".join(
                f"{name} {fmt(value)}" for name, value in comparison["rating_deltas"].items()
            )
            + f". Shared articles: {len(comparison['shared_article_ids'])}."
        )
    if "corrected" not in metrics["conditions"]:
        lines.extend(
            [
                "",
                "Preference-correction effect and persistence: not measured in this replay. Add a corrected-profile condition after an explicit correction and restart check.",
            ]
        )
    lines.extend(
        [
            "",
            "Conversational preference extraction and restart persistence are not exercised by this file-based comparison.",
            "Operational results here cover evaluation attempts only. Telegram delivery and software regression tests are separate checks. Monetary cost is not estimated without configured dated prices.",
            "",
            "No production profile, recommendation history, or Telegram delivery was changed. Human support labels require source interpretation; literal excerpt presence does not establish entailment.",
            "",
        ]
    )
    return "\n".join(lines)
