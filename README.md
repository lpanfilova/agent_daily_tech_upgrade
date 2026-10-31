# Daily Tech Upgrade Agent

Known source limitations and follow-up work are tracked in [ISSUES.md](ISSUES.md).

A personal technical-reading companion. The application runs in the terminal:
discover articles → retrieve and validate full text → rank for your profile → summarize →
save a brief and an inspectable run report.

The initial profile prioritizes **AI agent harness creation, practical agent use, and agent
development at novice level**. Harness creation means the tools, state, execution loop,
evaluation, and controls around an agent. The example JSON seeds the database profile on
the first run. Later runs load the stored profile; editing the example does not overwrite it.
Use `--import-profile config/profile.json` to save an intentional correction.

## Run locally

Python 3.11 or later is required. Run commands from the repository root.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Inspect real articles without a model key or a database:

```sh
.venv/bin/daily-tech --discover-only --no-db
```

For a generated brief, copy `.env.example` to `.env` if `.env` does not already exist.
Add your API key locally:

```dotenv
OPENAI_API_KEY=your-key-here
OPENAI_MODEL=gpt-4.1-mini
POSTGRES_PASSWORD=YOUR_CHOSEN_PASSWORD
DATABASE_URL=postgresql://daily_tech:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/daily_tech
```

Start the database (see Docker setup below), initialize the schema, and run:

```sh
docker compose up -d --wait db
.venv/bin/daily-tech --db-migrate
.venv/bin/daily-tech
```

The model can be changed in `.env` or with `--model`. It must be available to your API account
and support [Responses structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The initial adapter follows that API; retrieval does not use provider search tools.

Every run writes a separate directory under the ignored `output/` directory:

- `brief.md`: the reading brief, or an article list for discovery-only runs.
- `run.json`: profile, candidate metadata, retrieved text, ratings, recommendations,
  exact evidence excerpts, source failures, rejection reasons, model token usage, and a run UUID.

By default, the same run is stored in PostgreSQL. `--no-db` explicitly opts into the earlier
file-only behavior. A missing or unavailable database produces an error rather than silently
discarding persistence. `--discover-only` also stores its run and articles when using a database,
but does not mark articles as recommended.

Exit codes: `0` for a complete brief or successful discovery, `2` for a partial brief or
discovery with no readable articles, and `1` for a configuration or run failure.

## Local PostgreSQL with Docker

PostgreSQL stores the authoritative profile, profile revision history, article records,
briefs, recommendations, run status, diagnostics, and usage. JSONB snapshots retain the
profile and article contents used for each run, even when later runs update those records.

On this Apple silicon Mac, install and start
[Docker Desktop for Mac with Apple silicon](https://docs.docker.com/desktop/setup/install/mac-install/).
Docker Desktop includes Docker Compose. Once Docker is running:

```sh
docker compose up -d --wait db
docker compose ps
docker compose exec db psql -U daily_tech -d daily_tech -c 'SELECT version();'
```

`compose.yaml` starts PostgreSQL 18 with a health check and a named volume. The port is
bound to `127.0.0.1:5432` for the Python app running on your laptop. The volume mount follows
the [PostgreSQL 18 image layout](https://hub.docker.com/_/postgres).

The database and user are both `daily_tech`. Before the first startup, choose a password
in your ignored `.env` and put its URL-encoded form in `DATABASE_URL`. Compose requires
`POSTGRES_PASSWORD` and has no fallback password. `.env.example` contains placeholders only;
preserve your existing OpenAI key when configuring your local file. Changing the environment
password after the volume is initialized does not change the database role's password;
existing databases must continue using their current credentials until explicitly changed
in PostgreSQL.

To stop the database while keeping its data:

```sh
docker compose down
```

The named volume survives stopping or recreating the container. `docker compose down -v`
deletes that database volume and its data.

## Profiles, history, and migrations

```sh
.venv/bin/daily-tech --db-migrate
.venv/bin/daily-tech --show-profile
.venv/bin/daily-tech --import-profile config/profile.example.json
.venv/bin/daily-tech --history
```

Copy the example profile to the ignored `config/profile.json`, edit it, and import that file
to preserve the original example. `--profile FILE` explicitly saves that profile before a run;
with `--no-db`, it applies only to that invocation. Imports keep earlier profile revisions.
Profile commands and migration commands do not call OpenAI.

`--history` prints the latest 20 run UUIDs, timestamps, statuses, and recommendation counts.
Use `--show-run YOUR_RUN_UUID` to print the database copy of a completed brief.

Import a previous file-only brief so its recommendations count as history:

```sh
.venv/bin/daily-tech --import-run output/20261031T210705178692Z/run.json
```

Use a path that exists on your laptop. Imports do not call OpenAI or replace the current profile.
For older reports without a UUID, the importer derives one from the file contents; importing
the unchanged file twice does not duplicate it. A missing completion time is recorded as the
import time. Earlier output directories are not imported automatically.

Normal runs exclude previously recommended canonical URLs and normalized titles before
retrieval, then also check redirected URLs and exact normalized-body hashes after retrieval.
Previously discovered but unselected articles remain eligible. Exclusions are recorded in the
run report. A repeat-free run may have fewer than three acceptable recommendations; it does
not relax quality criteria to fill the brief. To allow repeats deliberately for testing:

```sh
.venv/bin/daily-tech --allow-repeats
```

SQL migrations live in `src/daily_tech/migrations/` and are packaged with the app. The migration
ledger checks hashes and applies pending scripts transactionally. Add a new numbered migration
instead of editing one already applied. Application runs check that migrations are current.
A PostgreSQL advisory lock prevents overlapping persistent runs from selecting the same history
simultaneously. No transaction is held open during research or model calls. Unexpected Python
failures/interruption mark a registered run failed; a hard process kill can leave its status
`running` for later operational reconciliation. Completed run snapshots are immutable.

## Sources and editorial behavior

`config/sources.json` includes OpenAI, Anthropic, Microsoft, Google Research/DeepMind,
AWS, IBM, LangChain/LangGraph, IEEE Spectrum, ACM Queue, and Hugging Face.
RSS/Atom is used when configured. Anthropic, IBM, LangChain, and ACM use bounded link
discovery from their official listings; a listing link must have a recent explicit
publication date on its article page before it can be accepted.

Source availability varies. The initial live check found accessible feeds for OpenAI,
Microsoft, Google, AWS, IEEE, and Hugging Face. OpenAI article pages returned HTTP 403
from this environment even though its feed was accessible. Such pages are skipped.
Feed and listing failures are recorded and do not stop other publishers.

The default lookback is seven days. Broaden it deliberately when there are too few
recent beginner-friendly articles:

```sh
.venv/bin/daily-tech --days 30
```

Selection requires minimum model-rated relevance and technical quality, distinct publisher
and topic labels, and at most 20 estimated minutes of original-article reading total.
The selector looks for a feasible set of three before settling for fewer. Fifteen minutes
is a target, not a minimum; estimates use 220 words/minute and may understate novice/code
reading time. Topic diversity is based on model labels and is not semantic deduplication.

Metadata and content checks include HTTPS publisher-domain checks on every redirect,
original publication dates, access status, readable body length, and duplicate URLs,
normalized titles, and exact text hashes. Tracking parameters and URL fragments are
removed while meaningful query parameters are preserved. When both feed and explicit
page publication dates exist, their calendar dates must agree. `dateModified` alone is
never used to make an old article fresh. Paywalls marked in structured metadata, short
previews, inaccessible content, and articles exceeding the full-text input limit are skipped.

Each generated insight must include a short excerpt that actually occurs in the retrieved
text. Code constructs allowed excerpts and the response schema constrains the model to
choose from them. Ranking likewise requires one assessment per supplied candidate, with
the application attaching the original article IDs. Excerpts are retained for review in
`run.json`. This checks evidence presence;
it does **not** establish that every summary claim follows from that evidence. Human review
of the first generated brief is still part of acceptance. Credible publishers can also
publish marketing, sponsored material, or advanced content; ranking filters these by prompt
and scores rather than treating the publisher as sufficient evidence of quality.

## Execution limits

Defaults are in `src/daily_tech/config.py`:

- Five candidates per source; 30 attempted article fetches; 18 accepted articles.
- A 5 MB response limit and 20,000 characters of full extracted text per article.
- Seven model calls maximum, including failed attempts; 4,000 output tokens per call.
- Fifteen-second retrieval timeouts, 60-second model timeouts, and a 300-second workflow timeout.

The usual successful run uses one ranking call and three summary calls. Failed summaries
can be replaced within the remaining call budget. There are no automatic HTTP retries
yet. Cancellation cannot guarantee that a model request already sent will incur no charge.
Limits bound work and token exposure; this increment has **no hard dollar spending cap**.
Model token usage is recorded when returned by the provider. Profile data and accepted
article text are sent to OpenAI for ranking/summarization; discovery-only sends neither
to a model. OpenAI requests use `store: false`.

## Development and verification

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

Tests use synthetic content and mocked network/model responses, with no API charges.
They cover the workflow, redirects, dates, duplicate handling, editorial selection,
evidence rejection, persisted run reports, and model failures/budgets. Live discovery is
a separate smoke check. A real model-generated brief still requires a configured key.

Database integration tests skip unless `TEST_DATABASE_URL` is set. With the local database
running, this command reads the connection URL from your ignored `.env` without printing it:

```sh
.venv/bin/python -c 'import os, pytest; from dotenv import load_dotenv; load_dotenv(); os.environ["TEST_DATABASE_URL"] = os.environ["DATABASE_URL"]; raise SystemExit(pytest.main(["-q"]))'
```

These tests create and clean up their own randomly named schemas, leaving application tables
untouched. They verify migrations, restart persistence, profile corrections, recommendation
history, concurrency protection, idempotent saves, and failure records. The database role must
be allowed to create schemas.

## Boundaries of this increment

`retrieval.py` owns discovery and verification; `editorial.py` owns ranking, selection,
and summarization; `llm.py` exposes a provider-neutral interface with an OpenAI adapter;
`pipeline.py` connects them through LangGraph; `cli.py` handles configuration and execution.
`storage.py` owns PostgreSQL access and migrations; `history.py` defines article identities
for repeat filtering. Profile revision history records explicit JSON imports. Conversational
memory extraction, feedback learning, Telegram integration, scheduling, and web search are
still future increments. Database run snapshots are separate from any future LangGraph checkpoints.

Next increment: Telegram delivery, then explicit feedback and conversational preference updates.
