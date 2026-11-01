import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import psycopg
from dotenv import load_dotenv

from .config import Limits, load_profile, load_sources
from .evaluation_cli import execute_evaluation
from .llm import OpenAIModel
from .models import RunReport
from .pipeline import run_pipeline, save_report
from .retrieval import Fetcher
from .storage import Repository, StorageError
from .telegram import TelegramClient, TelegramError, TelegramSettings
from .telegram_service import TelegramService
from .telegram_storage import TelegramStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Generate a source-grounded technical brief")
    result.add_argument(
        "--discover-only", action="store_true", help="Fetch articles without an API key"
    )
    result.add_argument(
        "--profile", type=Path, help="Explicitly replace the stored profile from JSON"
    )
    result.add_argument("--sources", type=Path, default=Path("config/sources.json"))
    result.add_argument("--output", type=Path, default=Path("output"))
    result.add_argument("--days", type=int, default=7, help="Publication lookback; default: 7 days")
    result.add_argument("--runtime-seconds", type=int, default=300)
    result.add_argument("--model", help="Overrides OPENAI_MODEL")
    result.add_argument(
        "--eval-corrected-profile",
        type=Path,
        help="Evaluation-only corrected JSON profile; never updates the real profile",
    )
    result.add_argument("--no-db", action="store_true", help="Run in file-only mode")
    result.add_argument(
        "--allow-repeats",
        action="store_true",
        help="Allow previously recommended articles for testing",
    )
    actions = result.add_mutually_exclusive_group()
    actions.add_argument(
        "--db-migrate", action="store_true", help="Apply pending database migrations"
    )
    actions.add_argument("--show-profile", action="store_true", help="Print the stored profile")
    actions.add_argument(
        "--import-profile", type=Path, help="Replace the stored profile from a JSON file"
    )
    actions.add_argument("--history", action="store_true", help="Show the latest 20 runs")
    actions.add_argument("--show-run", type=UUID, help="Print a stored brief by run ID")
    actions.add_argument("--import-run", type=Path, help="Import an earlier run.json, idempotently")
    actions.add_argument(
        "--telegram-check", action="store_true", help="Verify bot identity without sending messages"
    )
    actions.add_argument(
        "--telegram-listen", action="store_true", help="Listen locally for Telegram commands"
    )
    actions.add_argument(
        "--telegram-poll-once",
        action="store_true",
        help="Process one batch of pending Telegram updates",
    )
    actions.add_argument(
        "--send-brief",
        nargs="?",
        const="latest",
        metavar="RUN_UUID",
        help="Deliver a stored brief; defaults to latest",
    )
    actions.add_argument(
        "--telegram-deliveries", action="store_true", help="Show recent Telegram delivery statuses"
    )
    actions.add_argument(
        "--eval-freeze",
        nargs="?",
        const="latest",
        metavar="RUN_JSON_OR_UUID",
        help="Freeze retrieved articles/profile; defaults to latest saved brief",
    )
    actions.add_argument(
        "--eval-replay",
        type=Path,
        metavar="SNAPSHOT_JSON",
        help="Compare generic/current ranking on frozen inputs using OpenAI",
    )
    actions.add_argument(
        "--eval-report",
        type=Path,
        metavar="EVALUATION_DIR",
        help="Recalculate evaluation report from saved human reviews",
    )
    return result


async def execute(args: argparse.Namespace) -> int:
    if args.eval_corrected_profile and not args.eval_replay:
        raise ValueError("--eval-corrected-profile requires --eval-replay")
    if args.eval_freeze or args.eval_replay or args.eval_report:
        return await execute_evaluation(args)
    if args.telegram_check:
        settings = TelegramSettings.from_env(require_chat=False)
        async with httpx.AsyncClient() as http:
            bot = await TelegramClient(http, settings).get_me()
        print(f"Connected to @{bot.get('username', '')} (bot ID {bot['id']}). No message sent.")
        return 0
    telegram_action = (
        args.telegram_listen
        or args.telegram_poll_once
        or args.send_brief
        or args.telegram_deliveries
    )
    db_action = (
        args.db_migrate
        or args.show_profile
        or args.import_profile
        or args.history
        or args.show_run
        or args.import_run
        or telegram_action
    )
    if args.no_db:
        if db_action:
            raise ValueError("Database commands cannot be used with --no-db")
        return await execute_run(args)
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise ValueError("Set DATABASE_URL in .env, or use --no-db for file-only mode")
    with Repository(database_url) as repo:
        if args.db_migrate:
            applied = repo.migrate()
            print(
                "Applied: " + ", ".join(applied) if applied else "Database migrations are current."
            )
            return 0
        repo.require_migrations()
        if telegram_action:
            return await execute_telegram(args, repo)
        if args.show_profile:
            profile = repo.get_profile()
            print(profile.model_dump_json(indent=2) if profile else "No profile stored yet.")
            return 0
        if args.history:
            for run in repo.list_runs():
                print(
                    f"{run['run_id']}  {run['started_at'].isoformat()}  {run['status']}  "
                    f"{run['recommendations']} recommendations"
                )
            return 0
        if args.show_run:
            print(repo.get_brief(args.show_run))
            return 0
        with repo.run_lock():
            if args.import_profile:
                repo.set_profile(load_profile(args.import_profile), "explicit_import")
                print("Profile imported and saved.")
                return 0
            if args.import_run:
                data = json.loads(args.import_run.read_text(encoding="utf-8"))
                if "run_id" not in data:
                    data["run_id"] = str(
                        uuid5(
                            NAMESPACE_URL, hashlib.sha256(args.import_run.read_bytes()).hexdigest()
                        )
                    )
                report = RunReport.model_validate(data)
                if report.completed_at is None:
                    report.completed_at = datetime.now(UTC)
                with repo.connection.transaction():
                    if repo.begin_run(report, report.status == "discovery_only"):
                        repo.finish_run(report)
                        print(f"Imported run {report.run_id}.")
                    else:
                        print(f"Run {report.run_id} is already stored.")
                return 0
            return await execute_run(args, repo)


async def execute_telegram(args, repo):
    store = TelegramStore(repo)
    if args.telegram_deliveries:
        for delivery in store.list_deliveries():
            print(
                f"{delivery['delivery_id']}  {delivery['status']}  "
                f"{delivery['confirmed_parts']}/{delivery['parts']} confirmed parts  "
                f"run={delivery['run_id'] or 'command response'}"
            )
        return 0
    settings = TelegramSettings.from_env()
    async with httpx.AsyncClient() as http:
        client = TelegramClient(http, settings)
        bot = await client.get_me()
        service = TelegramService(client, store, bot)
        if args.send_brief:
            report = (
                repo.latest_report()
                if args.send_brief == "latest"
                else repo.get_report(UUID(args.send_brief))
            )
            if report is None:
                raise StorageError("No saved reading brief is ready to send")
            result = await service.send_brief(report)
            print(f"Telegram delivery {result.delivery_id}: {result.status}")
            if result.status == "uncertain":
                print("Delivery may already be visible in Telegram; held without automatic resend.")
            return 0 if result.status in ("sent", "already_sent") else 2
        await service.listen(once=args.telegram_poll_once)
    return 0


async def execute_run(args: argparse.Namespace, repo: Repository | None = None) -> int:
    limits = Limits(days=args.days, runtime_seconds=args.runtime_seconds)
    sources = load_sources(args.sources)
    model_name = args.model or os.getenv("OPENAI_MODEL", "")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not args.discover_only and (not api_key or not model_name):
        raise ValueError("Set OPENAI_API_KEY and OPENAI_MODEL in .env, or use --discover-only")
    profile = repo.get_profile() if repo and not args.profile else None
    if profile is None:
        profile = load_profile(args.profile or Path("config/profile.example.json"))
        if repo:
            repo.set_profile(profile, "explicit_import" if args.profile else "initial_import")
    history = repo.history() if repo and not args.allow_repeats else None
    report = RunReport(
        started_at=datetime.now(UTC),
        status="discovery_only" if args.discover_only else "failed",
        profile=profile,
        days=limits.days,
        allow_repeats=args.allow_repeats,
    )
    if repo:
        repo.begin_run(report, args.discover_only)
    print(f"Fetching trusted sources from the last {limits.days} days...", flush=True)
    try:
        async with httpx.AsyncClient(
            timeout=limits.request_timeout,
            headers={"User-Agent": "DailyTechUpgrade/0.1 (personal RSS reader)"},
        ) as client:
            fetcher = Fetcher(client, limits)
            model = None if args.discover_only else OpenAIModel(client, api_key, model_name, limits)
            await run_pipeline(report, sources, fetcher, model, limits, history)
        output = save_report(report, args.output)
        if repo:
            repo.finish_run(report)
    except BaseException:
        if repo:
            repo.fail_run(report.run_id)
        raise
    print(f"Run ID: {report.run_id} ({'PostgreSQL' if repo else 'file-only'})")
    print(
        f"{len(report.candidates)} candidates; {len(report.articles)} readable articles; "
        f"{len(report.recommendations)} recommendations. Status: {report.status}."
    )
    for item in report.events:
        if item["stage"] in {"source_error", "run_error", "summary_rejected"}:
            print(f"{item['stage']}: {item['subject']}: {item['reason']}", file=sys.stderr)
    print(f"Brief: {output / 'brief.md'}\nEvidence and diagnostics: {output / 'run.json'}")
    if report.status == "failed":
        return 1
    if report.status == "partial" or (args.discover_only and not report.articles):
        return 2
    return 0


def main() -> None:
    load_dotenv()
    args = parser().parse_args()
    try:
        code = asyncio.run(execute(args))
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        code = 1
    except StorageError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        code = 1
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        code = 1
    except psycopg.Error:
        print(
            "Database operation failed; verify connectivity and migrations. No file-only fallback was used.",
            file=sys.stderr,
        )
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
