import json
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Profile, Source


class Limits(BaseModel):
    days: int = Field(default=7, ge=1, le=365)
    per_source: int = Field(default=5, ge=1, le=20)
    max_fetches: int = Field(default=30, ge=1, le=100)
    max_articles: int = Field(default=18, ge=1, le=30)
    max_model_calls: int = Field(default=7, ge=2, le=15)
    max_output_tokens: int = Field(default=4000, ge=500, le=8000)
    max_reading_minutes: int = Field(default=20, ge=5, le=60)
    runtime_seconds: int = Field(default=300, ge=10, le=900)
    request_timeout: int = Field(default=15, ge=1, le=60)
    min_words: int = Field(default=250, ge=100, le=1000)
    max_text_chars: int = 20000
    max_response_bytes: int = 5_000_000


def load_profile(path: Path) -> Profile:
    return Profile.model_validate_json(path.read_text())


def load_sources(path: Path) -> list[Source]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("Sources configuration must be a JSON list")
    return [Source.model_validate(item) for item in data]
