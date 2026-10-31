from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(StrictModel):
    interests: list[str] = Field(min_length=1, max_length=15)
    depth: str = "intermediate"
    style: str = "Practical explanations, implementation details, and technical tradeoffs"
    goals: list[str] = Field(default_factory=list)


class Source(StrictModel):
    name: str
    publisher: str
    homepage: str
    domains: list[str]
    feed_url: str | None = None
    article_path_prefixes: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str = ""


class Candidate(StrictModel):
    id: str
    title: str
    url: str
    publisher: str
    source_name: str
    published_at: datetime | None
    date_origin: Literal["feed_published", "page_published"]
    description: str = ""


class Article(Candidate):
    published_at: datetime
    text: str
    word_count: int
    reading_minutes: int
    content_hash: str
    retrieved_at: datetime


class Rating(StrictModel):
    article_id: str
    relevance: int = Field(ge=0, le=5)
    quality: int = Field(ge=0, le=5)
    topic: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class Ratings(StrictModel):
    ratings: list[Rating]


class Insight(StrictModel):
    claim: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=180)


class Summary(StrictModel):
    summary: str = Field(min_length=40, max_length=1000)
    insights: list[Insight] = Field(min_length=2, max_length=2)
    why_it_matters: str = Field(min_length=20, max_length=500)


class Recommendation(StrictModel):
    article: Article
    rating: Rating
    summary: Summary


class RunReport(StrictModel):
    started_at: datetime
    status: Literal["discovery_only", "complete", "partial", "failed"]
    profile: Profile
    days: int
    candidates: list[Candidate] = Field(default_factory=list)
    articles: list[Article] = Field(default_factory=list)
    ratings: list[Rating] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    model_usage: list[dict] = Field(default_factory=list)
