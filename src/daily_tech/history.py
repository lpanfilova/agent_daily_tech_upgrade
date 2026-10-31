"""Identities used to avoid recommending the same article across runs."""

import re
from dataclasses import dataclass, field


def title_key(title: str) -> str:
    return re.sub(r"\W+", "", title.casefold())


@dataclass
class RecommendationHistory:
    urls: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)

    def contains(self, url: str, title: str, content_hash: str | None = None) -> bool:
        return (
            url in self.urls
            or title_key(title) in self.titles
            or (content_hash is not None and content_hash in self.hashes)
        )
