"""Bounded RSS discovery and article retrieval, without model dependencies."""

import hashlib
import json
import math
import re
from collections import deque
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx
import trafilatura
from lxml import etree
from lxml import html as lxml_html

from .config import Limits
from .models import Article, Candidate, Source


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def allowed_url(url: str, domains: list[str]) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
            and any(host == d or host.endswith("." + d) for d in domains)
        )
    except ValueError:
        return False


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def recent(date: datetime, now: datetime, days: int) -> bool:
    return now - timedelta(days=days) <= date <= now


def event(events: list[dict], stage: str, subject: str, reason: str) -> None:
    events.append({"stage": stage, "subject": subject, "reason": reason})


def listing_candidates(html: str, base_url: str, source: Source) -> list[Candidate]:
    tree = lxml_html.fromstring(html)
    candidates = []
    seen = set()
    for anchor in tree.xpath("//a[@href]"):
        url = canonical_url(urljoin(base_url, anchor.get("href")))
        title = " ".join(anchor.text_content().split())
        # Some cards use an empty overlay anchor beside the article heading.
        if len(title) < 15:
            parent = anchor.getparent()
            for _ in range(3):
                if parent is None:
                    break
                headings = parent.xpath(".//h2 | .//h3 | .//h4")
                if len(headings) == 1:
                    title = " ".join(headings[0].text_content().split())
                    break
                parent = parent.getparent()
        if (
            not allowed_url(url, source.domains)
            or url in seen
            or len(title) < 15
            or not any(urlsplit(url).path.startswith(p) for p in source.article_path_prefixes)
        ):
            continue
        seen.add(url)
        candidates.append(
            Candidate(
                id=hashlib.sha256(url.encode()).hexdigest()[:16],
                title=title[:500],
                url=url,
                publisher=source.publisher,
                source_name=source.name,
                published_at=None,
                date_origin="page_published",
            )
        )
    return candidates


def page_publication_date(html: str) -> datetime | None:
    """Read explicit original publication metadata; never infer freshness from body text."""
    tree = lxml_html.fromstring(html)
    for meta in tree.xpath("//meta[@content]"):
        label = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if label in {"article:published_time", "datepublished", "date", "pubdate", "publishdate"}:
            date = parse_date(meta.get("content"))
            if date:
                return date

    def json_date(value):
        if isinstance(value, dict):
            original = value.get("datePublished")
            if isinstance(original, str) and (date := parse_date(original)):
                return date
            for child in value.values():
                if date := json_date(child):
                    return date
        elif isinstance(value, list):
            for child in value:
                if date := json_date(child):
                    return date
        return None

    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            if date := json_date(json.loads(script.text or "")):
                return date
        except (ValueError, RecursionError):
            continue
    return None


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, limits: Limits):
        self.client = client
        self.limits = limits

    async def get(self, url: str, domains: list[str]) -> tuple[str, str]:
        for _ in range(5):
            if not allowed_url(url, domains):
                raise ValueError("URL or redirect outside configured HTTPS publisher domains")
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect without a location")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(
                    t in content_type for t in ("html", "xml", "rss", "atom", "text/plain")
                ):
                    raise ValueError("Unsupported content type")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.limits.max_response_bytes:
                        raise ValueError("Response exceeds size limit")
                return canonical_url(str(response.url)), data.decode(
                    response.encoding or "utf-8", errors="replace"
                )
        raise ValueError("Too many redirects")


async def discover(
    sources: list[Source], fetcher: Fetcher, now: datetime, events: list[dict]
) -> list[Candidate]:
    queues = []
    seen: set[str] = set()
    for source in sources:
        if not source.enabled or (not source.feed_url and not source.article_path_prefixes):
            event(events, "source", source.name, source.note or "Disabled or no feed configured")
            continue
        try:
            if not source.feed_url:
                base, html = await fetcher.get(source.homepage, source.domains)
                candidates = [
                    c for c in listing_candidates(html, base, source) if c.url not in seen
                ]
                selected = candidates[: fetcher.limits.per_source]
                seen.update(c.url for c in selected)
                queues.append(deque(selected))
                event(
                    events,
                    "source",
                    source.name,
                    f"{len(candidates)} listing links; publication dates checked on article pages",
                )
                continue
            _, xml = await fetcher.get(source.feed_url, source.domains)
            feed = feedparser.parse(xml)
            if not feed.entries:
                raise ValueError("No RSS/Atom entries found")
            candidates = []
            for entry in feed.entries:
                url = canonical_url(entry.get("link", ""))
                title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
                date = parse_date(entry.get("published"))
                # Updated timestamps are not treated as original publication dates.
                if not title or not date or not allowed_url(url, source.domains):
                    continue
                if not recent(date, now, fetcher.limits.days):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                candidates.append(
                    Candidate(
                        id=hashlib.sha256(url.encode()).hexdigest()[:16],
                        title=title,
                        url=url,
                        publisher=source.publisher,
                        source_name=source.name,
                        published_at=date,
                        date_origin="feed_published",
                        description=re.sub(r"<[^>]+>", " ", entry.get("summary", ""))[:1000],
                    )
                )
            candidates.sort(key=lambda c: c.published_at, reverse=True)
            queues.append(deque(candidates[: fetcher.limits.per_source]))
            event(
                events,
                "source",
                source.name,
                f"{len(feed.entries)} feed entries; {len(candidates)} eligible recent entries",
            )
        except (httpx.HTTPError, ValueError, etree.ParserError) as exc:
            event(events, "source_error", source.name, str(exc))
    # Interleave feeds so a large publisher cannot consume the whole retrieval budget.
    result = []
    while any(queues):
        for queue in queues:
            if queue:
                result.append(queue.popleft())
    return result


async def verify(
    candidates: list[Candidate],
    sources: list[Source],
    fetcher: Fetcher,
    now: datetime,
    events: list[dict],
) -> list[Article]:
    by_source = {source.name: source for source in sources}
    result: list[Article] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    seen_hashes: set[str] = set()
    for candidate in candidates[: fetcher.limits.max_fetches]:
        if len(result) >= fetcher.limits.max_articles:
            break
        try:
            url, html = await fetcher.get(candidate.url, by_source[candidate.source_name].domains)
            document = trafilatura.bare_extraction(
                html,
                url=url,
                with_metadata=True,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                date_extraction_params={"original_date": True},
            )
            if document is None or not document.text:
                raise ValueError("No readable article body")
            # Refuse explicit paywalls; do not summarize previews or bypass access controls.
            if re.search(r'"isAccessibleForFree"\s*:\s*(false|"false")', html, re.I):
                raise ValueError("Publisher marks content as paywalled")
            text = re.sub(r"[ \t]+", " ", document.text).strip()
            words = len(text.split())
            if words < fetcher.limits.min_words:
                raise ValueError(f"Insufficient article text ({words} words)")
            if len(text) > fetcher.limits.max_text_chars:
                raise ValueError("Article exceeds full-text model input budget")
            page_date = page_publication_date(html)
            published_at = candidate.published_at or page_date
            if not published_at or not recent(published_at, now, fetcher.limits.days):
                raise ValueError("No explicit recent publication date")
            if (
                page_date
                and candidate.published_at
                and page_date.date() != candidate.published_at.date()
            ):
                # Do not silently replace feed dates with ambiguous modified-page dates.
                raise ValueError("Publication date differs between feed and page")
            title = (
                document.title
                if candidate.date_origin == "page_published" and document.title
                else candidate.title
            )
            title_key = re.sub(r"\W+", "", title.casefold())
            digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
            if url in seen_urls or title_key in seen_titles or digest in seen_hashes:
                raise ValueError("Duplicate URL, title, or article body")
            seen_urls.add(url)
            seen_titles.add(title_key)
            seen_hashes.add(digest)
            fields = candidate.model_dump()
            fields["url"] = url
            fields["published_at"] = published_at
            fields["title"] = title
            result.append(
                Article(
                    **fields,
                    text=text,
                    word_count=words,
                    reading_minutes=max(1, math.ceil(words / 220)),
                    content_hash=digest,
                    retrieved_at=now,
                )
            )
        except (httpx.HTTPError, ValueError, etree.ParserError) as exc:
            event(events, "article_rejected", candidate.url, str(exc))
    return result
