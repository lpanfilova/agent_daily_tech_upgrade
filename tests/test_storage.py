"""Real PostgreSQL integration tests, isolated from application tables by schema."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_brief import FakeModel, article, summary

from daily_tech.cli import execute, parser
from daily_tech.config import Limits
from daily_tech.models import Candidate, Profile, Rating, Recommendation, RunReport
from daily_tech.pipeline import render_brief, run_pipeline
from daily_tech.storage import Repository, StorageError


@pytest.fixture
def database():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL integration tests")
    schema = "daily_tech_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield dsn, {"options": f"-csearch_path={schema}"}
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def repository(database):
    dsn, options = database
    return Repository(dsn, **options)


def report():
    return RunReport(
        started_at=datetime.now(UTC),
        status="failed",
        days=7,
        profile=Profile(interests=["agent harnesses"], depth="novice"),
    )


def test_migrations_are_repeatable_and_detect_changed_checksum(database):
    with repository(database) as repo:
        with pytest.raises(StorageError, match="not initialized"):
            repo.require_migrations()
        assert repo.migrate() == [
            "001_initial.sql",
            "002_telegram.sql",
            "003_preferences_feedback.sql",
        ]
        assert repo.migrate() == []
        repo.require_migrations()
        repo.connection.execute("UPDATE schema_migrations SET checksum = 'changed'")
        with pytest.raises(StorageError, match="modified"):
            repo.migrate()


def test_profile_correction_survives_new_connections_and_retains_revisions(database):
    initial = report().profile
    with repository(database) as repo:
        repo.migrate()
        repo.set_profile(initial, "initial_import")
    corrected = initial.model_copy(update={"depth": "intermediate"})
    with repository(database) as repo:
        assert repo.get_profile() == initial
        repo.set_profile(corrected, "explicit_import")
    with repository(database) as repo:
        assert repo.get_profile() == corrected
        rows = list(
            repo.connection.execute(
                "SELECT origin, payload FROM profile_revisions ORDER BY revision_id"
            )
        )
        assert [r["origin"] for r in rows] == ["initial_import", "explicit_import"]
        assert rows[0]["payload"]["depth"] == "novice"


def test_full_graph_restart_and_no_repeated_recommendations(database, monkeypatch):
    articles = [article(name, name.upper()) for name in "abcdef"]
    lookup = {a.id: a for a in articles}
    discovered = articles[:3]

    async def fake_discover(*args):
        return [
            Candidate.model_validate(a.model_dump(include=Candidate.model_fields.keys()))
            for a in discovered
        ]

    async def fake_verify(candidates, *args):
        return [lookup[a.id] for a in candidates]

    monkeypatch.setattr("daily_tech.pipeline.discover", fake_discover)
    monkeypatch.setattr("daily_tech.pipeline.verify", fake_verify)
    first = report()
    with repository(database) as repo:
        repo.migrate()
        repo.set_profile(first.profile, "initial_import")
        with repo.run_lock():
            assert repo.begin_run(first, False)
            asyncio.run(run_pipeline(first, [], None, FakeModel(), Limits(), repo.history()))
            repo.finish_run(first)
            repo.finish_run(first)  # A retry must not duplicate rows.
        assert first.status == "complete"
    discovered = articles
    second = report()
    with repository(database) as repo:
        second.profile = repo.get_profile()
        with repo.run_lock():
            assert repo.begin_run(second, False)
            asyncio.run(run_pipeline(second, [], None, FakeModel(), Limits(), repo.history()))
            repo.finish_run(second)
        assert second.status == "complete"
        assert {r.article.id for r in first.recommendations}.isdisjoint(
            r.article.id for r in second.recommendations
        )
        assert len(second.articles) == 3
        assert sum(e["stage"] == "history_excluded" for e in second.events) == 3
        assert len(repo.history().urls) == 6
        assert len(repo.list_runs()) == 2
        assert repo.get_brief(first.run_id) == render_brief(first)
        row = repo.connection.execute(
            "SELECT report FROM runs WHERE run_id = %s", (first.run_id,)
        ).fetchone()
        restored = RunReport.model_validate(row["report"])
        assert restored == first


def test_unexpected_failure_is_recorded_without_blocking_future_runs(database):
    run = report()
    with repository(database) as repo:
        repo.migrate()
        with repo.run_lock():
            repo.begin_run(run, False)
            repo.fail_run(run.run_id)
        assert repo.list_runs()[0]["status"] == "failed"
        row = repo.connection.execute(
            "SELECT report FROM runs WHERE run_id = %s", (run.run_id,)
        ).fetchone()
        assert RunReport.model_validate(row["report"]).completed_at is not None
    with repository(database) as repo, repo.run_lock():
        assert repo.begin_run(report(), False)


def test_overlapping_runs_are_rejected(database):
    with repository(database) as first, repository(database) as second, first.run_lock():
        with pytest.raises(StorageError, match="Another brief run"):
            with second.run_lock():
                pytest.fail("A second run should not acquire the lock")
    with repository(database) as repo, repo.run_lock():
        pass


def test_incomplete_discovery_does_not_add_recommendation_history(database):
    run = report()
    run.articles = [article()]
    run.status = "discovery_only"
    run.completed_at = datetime.now(UTC)
    with repository(database) as repo:
        repo.migrate()
        repo.begin_run(run, True)
        repo.finish_run(run)
        assert not repo.history().urls
        assert not repo.history().hashes


@pytest.mark.parametrize("allow_repeats", [False, True])
def test_cli_loads_saved_profile_and_respects_repeat_override(
    database, monkeypatch, tmp_path, allow_repeats
):
    previous = report()
    previous.status = "partial"
    previous.completed_at = datetime.now(UTC)
    item = article()
    previous.articles = [item]
    rating = Rating(
        article_id=item.id, relevance=5, quality=4, topic="harnesses", reason="tutorial"
    )
    previous.recommendations = [Recommendation(article=item, rating=rating, summary=summary())]
    with repository(database) as repo:
        repo.migrate()
        repo.set_profile(previous.profile, "initial_import")
        repo.begin_run(previous, False)
        repo.finish_run(previous)

    monkeypatch.setenv("DATABASE_URL", "test-fixture")
    monkeypatch.setattr("daily_tech.cli.Repository", lambda _: repository(database))
    monkeypatch.setattr(
        "daily_tech.cli.load_profile",
        lambda _: pytest.fail("Stored profile should be authoritative"),
    )

    async def inspect_pipeline(run, sources, fetcher, model, limits, history):
        assert run.profile == previous.profile
        assert model is None
        assert (history is None) == allow_repeats
        if history is not None:
            assert history.contains(item.url, item.title, item.content_hash)
        run.completed_at = datetime.now(UTC)
        return run

    monkeypatch.setattr("daily_tech.cli.run_pipeline", inspect_pipeline)
    flags = ["--discover-only", "--output", str(tmp_path)]
    if allow_repeats:
        flags.append("--allow-repeats")
    assert asyncio.run(execute(parser().parse_args(flags))) == 2


def test_save_failure_rolls_back_the_entire_run_snapshot(database):
    run = report()
    run.articles = [article()]
    run.completed_at = datetime.now(UTC)
    with repository(database) as repo:
        repo.migrate()
        with pytest.raises(StorageError, match="unregistered"):
            repo.finish_run(run)
        assert (
            repo.connection.execute("SELECT count(*) AS count FROM articles").fetchone()["count"]
            == 0
        )
