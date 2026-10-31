import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .config import Limits, load_profile, load_sources
from .llm import OpenAIModel
from .models import RunReport
from .pipeline import run_pipeline, save_report
from .retrieval import Fetcher


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Generate a source-grounded technical brief")
    result.add_argument(
        "--discover-only", action="store_true", help="Fetch articles without an API key"
    )
    result.add_argument("--profile", type=Path, default=Path("config/profile.example.json"))
    result.add_argument("--sources", type=Path, default=Path("config/sources.json"))
    result.add_argument("--output", type=Path, default=Path("output"))
    result.add_argument("--days", type=int, default=7, help="Publication lookback; default: 7 days")
    result.add_argument("--runtime-seconds", type=int, default=300)
    result.add_argument("--model", help="Overrides OPENAI_MODEL")
    return result


async def execute(args: argparse.Namespace) -> int:
    limits = Limits(days=args.days, runtime_seconds=args.runtime_seconds)
    profile, sources = load_profile(args.profile), load_sources(args.sources)
    model_name = args.model or os.getenv("OPENAI_MODEL", "")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not args.discover_only and (not api_key or not model_name):
        raise ValueError("Set OPENAI_API_KEY and OPENAI_MODEL in .env, or use --discover-only")
    report = RunReport(
        started_at=datetime.now(UTC),
        status="discovery_only" if args.discover_only else "failed",
        profile=profile,
        days=limits.days,
    )
    print(f"Fetching trusted sources from the last {limits.days} days...", flush=True)
    async with httpx.AsyncClient(
        timeout=limits.request_timeout,
        headers={"User-Agent": "DailyTechUpgrade/0.1 (personal RSS reader)"},
    ) as client:
        fetcher = Fetcher(client, limits)
        model = None if args.discover_only else OpenAIModel(client, api_key, model_name, limits)
        await run_pipeline(report, sources, fetcher, model, limits)
    output = save_report(report, args.output)
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
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
