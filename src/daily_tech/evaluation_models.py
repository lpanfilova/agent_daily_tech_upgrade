"""Versioned local evaluation artifacts; human judgments default to pending."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field

from .config import Limits
from .models import Article, Profile, RunReport, StrictModel

ConditionName = Literal["generic", "current", "corrected"]
Score = Annotated[int, Field(strict=True, ge=1, le=5)]


class EvaluationSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    snapshot_id: str
    frozen_at: datetime
    eligibility_as_of: datetime
    source_run_id: UUID
    source_origin: str
    profile: Profile
    model: str
    limits: Limits
    code_fingerprint: str
    articles: list[Article] = Field(min_length=1, max_length=30)
    note: str = (
        "Replay settings are captured at export. The source run did not record all "
        "historical settings. This corpus contains accepted retrieved articles, "
        "not inaccessible or rejected discovery candidates."
    )


class EvaluationCondition(StrictModel):
    name: ConditionName
    ranking_profile: Profile | None
    report: RunReport
    elapsed_seconds: float = 0


class EvaluationResult(StrictModel):
    schema_version: Literal[1] = 1
    evaluation_id: UUID = Field(default_factory=uuid4)
    result_digest: str = ""
    snapshot_id: str
    model: str
    code_fingerprint: str
    started_at: datetime
    completed_at: datetime | None = None
    status: Literal["running", "complete", "partial", "interrupted"] = "running"
    summary_profile: Profile
    conditions: list[EvaluationCondition] = Field(default_factory=list)


class ArticleReview(StrictModel):
    article_id: str
    review_code: str
    relevance: Score | None = None
    difficulty_fit: Score | None = None
    practical_value: Score | None = None
    notes: str = ""


class FieldReview(StrictModel):
    verdict: Literal["pending", "verified", "incorrect", "unverified"] = "pending"
    note: str = ""


class MetadataReview(StrictModel):
    article_id: str
    title: FieldReview = Field(default_factory=FieldReview)
    publisher: FieldReview = Field(default_factory=FieldReview)
    original_url: FieldReview = Field(default_factory=FieldReview)
    publication_date: FieldReview = Field(default_factory=FieldReview)


class ClaimReview(StrictModel):
    claim_id: str
    condition: ConditionName
    article_id: str
    section: Literal["summary", "insight", "relevance"]
    text: str = Field(min_length=1)
    verdict: Literal["pending", "supported", "contradicted", "unverifiable", "not_factual"] = (
        "pending"
    )
    source_passage: str = ""
    note: str = ""


class EvaluationReview(StrictModel):
    schema_version: Literal[1] = 1
    evaluation_id: UUID
    snapshot_id: str
    reviewer: str | None = None
    rating_target_profile: Profile
    articles: list[ArticleReview]
    metadata: list[MetadataReview]
    claim_inventory_checked: dict[ConditionName, bool]
    claims: list[ClaimReview]
