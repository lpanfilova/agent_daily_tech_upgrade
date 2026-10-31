"""PostgreSQL storage with short transactions and versioned SQL migrations."""

import hashlib
from contextlib import contextmanager
from importlib.resources import files
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .history import RecommendationHistory, title_key
from .models import Profile, RunReport
from .pipeline import render_brief
from .retrieval import canonical_url

MIGRATION_LOCK = 72419610
RUN_LOCK = 72419611


class StorageError(RuntimeError):
    pass


def migrations():
    return sorted(files("daily_tech").joinpath("migrations").iterdir(), key=lambda p: p.name)


class Repository:
    def __init__(self, database_url: str, **connect_options):
        try:
            self.connection = psycopg.connect(
                database_url,
                autocommit=True,
                connect_timeout=5,
                row_factory=dict_row,
                **connect_options,
            )
        except psycopg.Error as exc:
            # Never print DSNs or authentication details in CLI errors.
            raise StorageError(
                "Cannot connect to PostgreSQL; check DATABASE_URL and the running database"
            ) from exc

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def migrate(self) -> list[str]:
        applied = []
        with self.connection.transaction():
            self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            self.connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())""")
            existing = {
                r["version"]: r["checksum"]
                for r in self.connection.execute("SELECT version, checksum FROM schema_migrations")
            }
            expected_names = {m.name for m in migrations() if m.suffix == ".sql"}
            if existing.keys() - expected_names:
                raise StorageError("Database has migrations unknown to this application")
            for migration in migrations():
                if migration.suffix != ".sql":
                    continue
                sql_text = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql_text.encode()).hexdigest()
                if migration.name in existing:
                    if existing[migration.name] != checksum:
                        raise StorageError(f"Applied migration was modified: {migration.name}")
                    continue
                self.connection.execute(sql_text)
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                    (migration.name, checksum),
                )
                applied.append(migration.name)
        return applied

    def require_migrations(self):
        if not self.connection.execute(
            "SELECT to_regclass('schema_migrations') AS name"
        ).fetchone()["name"]:
            raise StorageError("Database is not initialized; run daily-tech --db-migrate")
        applied = {
            r["version"]: r["checksum"]
            for r in self.connection.execute("SELECT version, checksum FROM schema_migrations")
        }
        expected = {
            m.name: hashlib.sha256(m.read_bytes()).hexdigest()
            for m in migrations()
            if m.suffix == ".sql"
        }
        if applied != expected:
            raise StorageError(
                "Database migrations differ from this app; run daily-tech --db-migrate"
            )

    @contextmanager
    def run_lock(self):
        if not self.connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (RUN_LOCK,)
        ).fetchone()["acquired"]:
            raise StorageError("Another brief run is active; wait for it to finish")
        try:
            yield
        finally:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))

    def get_profile(self) -> Profile | None:
        row = self.connection.execute(
            "SELECT payload FROM profiles WHERE profile_id = 'default'"
        ).fetchone()
        return Profile.model_validate(row["payload"]) if row else None

    def set_profile(self, profile: Profile, origin: str):
        payload = Jsonb(profile.model_dump(mode="json"))
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO profiles(profile_id, payload) VALUES ('default', %s)
                ON CONFLICT (profile_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()""",
                (payload,),
            )
            self.connection.execute(
                """INSERT INTO profile_revisions(profile_id, payload, origin)
                VALUES ('default', %s, %s)""",
                (payload, origin),
            )

    def history(self) -> RecommendationHistory:
        rows = list(
            self.connection.execute(
                "SELECT article_url, title_key, content_hash FROM recommendations"
            )
        )
        return RecommendationHistory(
            {r["article_url"] for r in rows},
            {r["title_key"] for r in rows},
            {r["content_hash"] for r in rows},
        )

    def begin_run(self, report: RunReport, discovery_only: bool) -> bool:
        return (
            self.connection.execute(
                """INSERT INTO runs
            (run_id, started_at, status, discovery_only, allow_repeats, profile_snapshot, report)
            VALUES (%s, %s, 'running', %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING""",
                (
                    report.run_id,
                    report.started_at,
                    discovery_only,
                    report.allow_repeats,
                    Jsonb(report.profile.model_dump(mode="json")),
                    Jsonb(report.model_dump(mode="json")),
                ),
            ).rowcount
            == 1
        )

    def finish_run(self, report: RunReport):
        ratings = {r.article_id: r for r in report.ratings}
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT status, report FROM runs WHERE run_id = %s FOR UPDATE", (report.run_id,)
            ).fetchone()
            if not row:
                raise StorageError("Cannot finish an unregistered run")
            if row["status"] != "running":
                if row["report"] == report.model_dump(mode="json"):
                    return
                raise StorageError("Run is already finalized; its snapshot cannot be replaced")
            for article in report.articles:
                url = canonical_url(article.url)
                self.connection.execute(
                    """INSERT INTO articles
                    (url, title_key, content_hash, publisher, published_at, payload)
                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO UPDATE SET
                    title_key = EXCLUDED.title_key, content_hash = EXCLUDED.content_hash,
                    publisher = EXCLUDED.publisher, published_at = EXCLUDED.published_at,
                    payload = EXCLUDED.payload, last_seen_at = now()""",
                    (
                        url,
                        title_key(article.title),
                        article.content_hash,
                        article.publisher,
                        article.published_at,
                        Jsonb(article.model_dump(mode="json")),
                    ),
                )
                rating = ratings.get(article.id)
                self.connection.execute(
                    """INSERT INTO run_articles(run_id, article_url, rating)
                    VALUES (%s, %s, %s) ON CONFLICT (run_id, article_url) DO UPDATE SET rating = EXCLUDED.rating""",
                    (report.run_id, url, Jsonb(rating.model_dump()) if rating else None),
                )
            for position, rec in enumerate(report.recommendations, 1):
                self.connection.execute(
                    """INSERT INTO recommendations
                    (run_id, article_url, position, content_hash, title_key, summary, rating)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, article_url) DO NOTHING""",
                    (
                        report.run_id,
                        canonical_url(rec.article.url),
                        position,
                        rec.article.content_hash,
                        title_key(rec.article.title),
                        Jsonb(rec.summary.model_dump()),
                        Jsonb(rec.rating.model_dump()),
                    ),
                )
            updated = self.connection.execute(
                """UPDATE runs SET status = %s, finished_at = %s,
                report = %s, brief = %s WHERE run_id = %s""",
                (
                    report.status,
                    report.completed_at,
                    Jsonb(report.model_dump(mode="json")),
                    render_brief(report),
                    report.run_id,
                ),
            )
            if updated.rowcount != 1:
                raise StorageError("Cannot finish an unregistered run")

    def fail_run(self, run_id: UUID):
        self.connection.execute(
            """UPDATE runs SET status = 'failed', finished_at = now(),
            error = 'Run interrupted or failed unexpectedly',
            report = jsonb_set(jsonb_set(report, '{status}', '"failed"'::jsonb),
                               '{completed_at}', to_jsonb(now()))
            WHERE run_id = %s AND status = 'running'""",
            (run_id,),
        )

    def list_runs(self) -> list[dict]:
        return list(
            self.connection.execute("""SELECT r.run_id, r.started_at, r.status,
            r.discovery_only, count(c.article_url) AS recommendations FROM runs r
            LEFT JOIN recommendations c USING(run_id) GROUP BY r.run_id
            ORDER BY r.started_at DESC LIMIT 20""")
        )

    def get_brief(self, run_id: UUID) -> str:
        row = self.connection.execute(
            "SELECT brief FROM runs WHERE run_id = %s", (run_id,)
        ).fetchone()
        if not row or row["brief"] is None:
            raise StorageError("No completed brief found for that run ID")
        return row["brief"]
