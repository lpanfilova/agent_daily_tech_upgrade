import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from daily_tech.config import Limits
from daily_tech.editorial import rank, validate_evidence
from daily_tech.llm import ModelError, OpenAIModel
from daily_tech.models import (
    Article,
    Candidate,
    Insight,
    Profile,
    Rating,
    Ratings,
    RunReport,
    Source,
    Summary,
)
from daily_tech.pipeline import render_brief, run_pipeline, save_report
from daily_tech.retrieval import (
    Fetcher,
    allowed_url,
    canonical_url,
    discover,
    listing_candidates,
    page_publication_date,
    verify,
)

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
PROFILE = Profile(interests=["agent development"], depth="novice")
TEXT = (
    "An agent harness manages tools and tracks execution state. "
    "The tutorial explains retries and provides a small working example. "
) * 30


def source(name="Lab", domain="lab.example"):
    return Source(
        name=name,
        publisher=name,
        homepage=f"https://{domain}/",
        domains=[domain],
        feed_url=f"https://{domain}/rss",
    )


def candidate(id="a", publisher="Lab"):
    return Candidate(
        id=id,
        title=f"Building agents {id}",
        url=f"https://lab.example/{id}",
        publisher=publisher,
        source_name="Lab",
        published_at=NOW,
        date_origin="feed_published",
    )


def article(id="a", publisher="Lab", minutes=5):
    return Article(
        **candidate(id, publisher).model_dump(),
        text=TEXT,
        word_count=600,
        reading_minutes=minutes,
        content_hash=id,
        retrieved_at=NOW,
    )


def summary(evidence="An agent harness manages tools and tracks execution state."):
    return Summary(
        summary="This tutorial explains how an agent harness manages tools and execution.",
        insights=[
            Insight(claim="A harness tracks state.", evidence=evidence),
            Insight(
                claim="Examples help with implementation.",
                evidence="The tutorial explains retries and provides a small working example.",
            ),
        ],
        why_it_matters="It introduces practical concepts for a novice building agents.",
    )


class FakeModel:
    def __init__(self, ratings=None, bad_summary=False):
        self.usage = []
        self.ratings = ratings
        self.bad_summary = bad_summary

    async def generate(self, instructions, data, schema):
        self.usage.append({"test_call": schema.__name__})
        if schema is Ratings:
            return self.ratings or Ratings(
                ratings=[
                    Rating(
                        article_id=a["article_id"],
                        relevance=5,
                        quality=4,
                        topic=a["article_id"],
                        reason="Practical tutorial",
                    )
                    for a in data["articles"]
                ]
            )
        return (
            summary("These words are fabricated and absent from the source.")
            if self.bad_summary
            else summary()
        )


def test_url_deduplication_preserves_meaningful_query():
    assert (
        canonical_url("https://lab.example/a/?utm_source=rss&x=2#top")
        == "https://lab.example/a?x=2"
    )
    assert canonical_url("https://lab.example/a?x=3") != canonical_url("https://lab.example/a?x=2")


@pytest.mark.parametrize(
    "url",
    [
        "https://lab.example.evil.test/a",
        "http://lab.example/a",
        "https://user:pass@lab.example/a",
        "https://lab.example:8080/a",
        "file:///etc/passwd",
    ],
)
def test_rejects_untrusted_urls(url):
    assert not allowed_url(url, ["lab.example"])


def test_redirect_is_validated_before_following():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="outside configured"):
                await Fetcher(client, Limits()).get("https://lab.example/a", ["lab.example"])

    asyncio.run(run())
    assert len(requests) == 1


def test_discovery_filters_stale_future_missing_dates_and_tracking_duplicates():
    entries = [
        ("valid", NOW.isoformat(), "a?utm_source=rss"),
        ("duplicate", NOW.isoformat(), "a"),
        ("old", (NOW - timedelta(days=10)).isoformat(), "old"),
        ("future", (NOW + timedelta(days=1)).isoformat(), "future"),
        ("undated", "", "undated"),
    ]
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(
            f'<entry><title>{title}</title><link href="https://lab.example/{slug}"/>'
            f"<published>{date}</published></entry>"
            for title, date, slug in entries
        )
        + "</feed>"
    )

    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=xml))
        async with httpx.AsyncClient(transport=transport) as client:
            return await discover([source()], Fetcher(client, Limits()), NOW, [])

    result = asyncio.run(run())
    assert len(result) == 1
    assert result[0].title == "valid"


def test_verification_rejects_short_paywalled_duplicate_and_date_conflict(monkeypatch):
    docs = {
        "a": SimpleNamespace(text=TEXT, date="2026-10-31"),
        "b": SimpleNamespace(text=TEXT, date="2026-10-31"),
        "short": SimpleNamespace(text="A preview.", date=None),
        "date": SimpleNamespace(text=TEXT + " date", date="2026-10-01"),
        "paid": SimpleNamespace(text=TEXT + " paid", date=None),
    }
    monkeypatch.setattr(
        "daily_tech.retrieval.trafilatura.bare_extraction",
        lambda html, **kwargs: docs[kwargs["url"].rsplit("/", 1)[-1]],
    )

    async def run():
        def handler(request):
            slug = request.url.path.strip("/")
            date = docs[slug].date
            body = f'<html><head><meta property="article:published_time" content="{date or ""}"></head>'
            body += (
                '<script type="application/ld+json">{"isAccessibleForFree":false}</script>'
                if slug == "paid"
                else ""
            ) + "<body>Article</body></html>"
            return httpx.Response(200, text=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = []
            articles = await verify(
                [candidate(s) for s in docs], [source()], Fetcher(client, Limits()), NOW, events
            )
            return articles, events

    articles, events = asyncio.run(run())
    assert [a.id for a in articles] == ["a"]
    assert len(events) == 4


def test_evidence_must_exist_in_article():
    validate_evidence(summary(), article())
    with pytest.raises(ValueError, match="does not appear"):
        validate_evidence(
            summary("An invented claim without any support in the article."), article()
        )


def test_rank_rejects_invented_ids():
    model = FakeModel(
        Ratings(
            ratings=[
                Rating(article_id="unknown", relevance=5, quality=5, topic="agents", reason="test")
            ]
        )
    )
    with pytest.raises(ModelError, match="article IDs"):
        asyncio.run(rank([article()], PROFILE, model))


def prepare_pipeline(monkeypatch, articles):
    async def fake_discover(*args):
        return [candidate(a.id) for a in articles]

    async def fake_verify(*args):
        return articles

    monkeypatch.setattr("daily_tech.pipeline.discover", fake_discover)
    monkeypatch.setattr("daily_tech.pipeline.verify", fake_verify)


def run_test_pipeline(model):
    report = RunReport(started_at=NOW, status="failed", profile=PROFILE, days=7)
    return asyncio.run(run_pipeline(report, [], None, model, Limits()))


def test_complete_graph_and_evidence_saved(monkeypatch, tmp_path):
    prepare_pipeline(monkeypatch, [article("a", "A"), article("b", "B"), article("c", "C")])
    report = run_test_pipeline(FakeModel())
    assert report.status == "complete"
    assert len(report.recommendations) == 3
    output = save_report(report, tmp_path)
    restored = RunReport.model_validate_json((output / "run.json").read_text())
    assert restored.recommendations[0].summary.insights[0].evidence
    assert "Original article" in (output / "brief.md").read_text()
    assert len(report.model_usage) == 4


def test_selection_finds_three_articles_instead_of_greedily_choosing_longest(monkeypatch):
    prepare_pipeline(
        monkeypatch,
        [article("a", "A", 12), article("b", "A", 3), article("c", "C", 12), article("d", "D", 5)],
    )
    report = run_test_pipeline(FakeModel())
    assert report.status == "complete"
    assert [r.article.id for r in report.recommendations] == ["b", "c", "d"]
    assert sum(r.article.reading_minutes for r in report.recommendations) == 20


def test_partial_brief_when_no_feasible_triple_exists(monkeypatch):
    prepare_pipeline(
        monkeypatch, [article("a", "A", 8), article("b", "B", 8), article("c", "C", 8)]
    )
    report = run_test_pipeline(FakeModel())
    assert report.status == "partial"
    assert len(report.recommendations) == 2
    assert "Selected 2/3" in render_brief(report)


def test_listing_discovery_and_explicit_date_metadata():
    lab = source()
    lab.feed_url = None
    lab.article_path_prefixes = ["/articles/"]
    html = """<html><head><script type="application/ld+json">
        {"@graph":[{"@type":"Article","datePublished":"2026-10-30",
                     "dateModified":"2026-10-31"}]}</script></head><body>
        <a href="/articles/tools">A beginner guide to agent tools</a>
        <a href="/articles/tools?utm_source=rss">A duplicate link to agent tools</a>
        <a href="https://evil.test/articles/wrong">An external article link</a>
        <a href="/about">Information about this publisher</a>
        <div><h2>Using a tool loop for your first agent</h2><a href="/articles/loop"></a></div>
        </body></html>"""
    candidates = listing_candidates(html, lab.homepage, lab)
    assert len(candidates) == 2
    assert candidates[1].title == "Using a tool loop for your first agent"
    assert candidates[0].published_at is None
    assert page_publication_date(html).date().isoformat() == "2026-10-30"
    assert (
        page_publication_date(
            '<html><meta property="article:modified_time" content="2026-10-31"></html>'
        )
        is None
    )


def test_unsupported_evidence_is_not_published(monkeypatch):
    prepare_pipeline(monkeypatch, [article()])
    report = run_test_pipeline(FakeModel(bad_summary=True))
    assert report.status == "partial"
    assert report.recommendations == []
    assert report.events[0]["stage"] == "summary_rejected"


def test_model_failure_still_returns_diagnostics(monkeypatch):
    prepare_pipeline(monkeypatch, [article()])

    class FailingModel(FakeModel):
        async def generate(self, *args):
            raise ModelError("API unavailable")

    report = run_test_pipeline(FailingModel())
    assert report.status == "failed"
    assert report.articles
    assert report.events[-1]["reason"] == "API unavailable"


def test_openai_adapter_request_schema_usage_and_budget():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        assert payload["text"]["format"]["schema"]["additionalProperties"] is False
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": summary().model_dump_json()}],
                    }
                ],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIModel(client, "test-key", "test-model", Limits(max_model_calls=2))
            await model.generate("test", {}, Summary)
            await model.generate("test", {}, Summary)
            with pytest.raises(ModelError, match="budget exhausted"):
                await model.generate("test", {}, Summary)
            assert model.usage[0]["usage"]["input_tokens"] == 10

    asyncio.run(run())


def test_actual_html_extraction_for_listing_article():
    paragraphs = [
        "An agent harness is the program that runs a language model together with useful tools. "
        "A beginner can start with a single function that accepts a question and returns a result. "
        "The harness records each step so the developer can understand what happened during execution.",
        "Tools provide carefully defined actions such as searching a document or performing a calculation. "
        "Each tool has a name and a description of its inputs. The program validates those inputs before "
        "calling the function and gives the result back to the model for the next decision.",
        "State stores the information needed between steps of the workflow. For a simple reading assistant, "
        "this might include the original question and a list of retrieved documents. Keeping this state "
        "explicit helps a developer inspect failures without trying to reconstruct the entire conversation.",
        "A stopping rule limits how many tool calls the agent can make. Without a limit, repeated failures "
        "may lead to unnecessary work. A small evaluation set of realistic questions helps the developer "
        "check whether a change improves results before adding more complicated behavior to the application.",
    ]
    html = (
        "<html><head><title>A beginner guide to agent harnesses</title>"
        '<meta property="article:published_time" content="2026-10-30">'
        "</head><body><main><article><h1>A beginner guide to agent harnesses</h1>"
        + "".join(f"<p>{p}</p>" for p in paragraphs)
        + "</article></main></body></html>"
    )
    item = candidate()
    item.published_at = None
    item.date_origin = "page_published"

    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=html))
        async with httpx.AsyncClient(transport=transport) as client:
            events = []
            articles = await verify(
                [item], [source()], Fetcher(client, Limits(min_words=100)), NOW, events
            )
            assert not events
            return articles

    articles = asyncio.run(run())
    assert len(articles) == 1
    assert articles[0].published_at.date().isoformat() == "2026-10-30"
    assert articles[0].title == "A beginner guide to agent harnesses"
    assert "State stores" in articles[0].text


def test_openai_error_does_not_expose_key():
    async def run():
        transport = httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": "unauthorized"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            model = OpenAIModel(client, "secret-test-key", "test-model", Limits())
            with pytest.raises(ModelError) as error:
                await model.generate("test", {}, Summary)
            assert "secret-test-key" not in str(error.value)
            assert model.usage == [{"call": 1, "status": 401}]

    asyncio.run(run())
