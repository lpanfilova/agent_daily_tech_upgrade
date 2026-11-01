# Daily Tech Upgrade Agent

Known source limitations and follow-up work are tracked in [ISSUES.md](ISSUES.md).

A personal technical-reading companion. The application runs in the terminal:
discover articles → retrieve and validate full text → rank for your profile → summarize →
save a brief and an inspectable run report. Saved briefs can be delivered to your private
Telegram chat, with a local listener for basic commands.

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

## Telegram delivery and local commands

Set these values locally in your ignored `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=YOUR_BOTFATHER_TOKEN
TELEGRAM_CHAT_ID=YOUR_VERIFIED_PRIVATE_CHAT_ID
```

Use the private chat ID verified from your own `/start` message. The listener only responds
when both the private chat and sender match that ID; groups, other users, and unsupported
update types are ignored. Tokens and chat IDs are not included in `.env.example` values.

Apply the delivery migration, check the bot identity, and send your latest saved brief:

```sh
.venv/bin/daily-tech --db-migrate
.venv/bin/daily-tech --telegram-check
.venv/bin/daily-tech --send-brief
```

`--telegram-check` is read-only and needs only the token. Sending and listening require
PostgreSQL, the token, and the verified chat ID. These commands do not call OpenAI.
`--send-brief RUN_UUID` selects a particular saved brief. By default, delivery selects the
newest complete or partial run with recommendations, ordered by its original start time.
It sends an overview followed by each article's title, date, publisher, link, summary,
technical insights, and relevance explanation. Long text is split below Telegram's message
limit; plain text avoids markup parsing errors and link previews are disabled.

Start the local listener in a terminal and leave that terminal and Docker running:

```sh
.venv/bin/daily-tech --telegram-listen
```

Then try these commands in your Telegram chat:

- `/start`: welcome and available commands.
- `/brief`: deliver the latest saved brief, or report that it is already in the chat.
- `/profile`: display the stored reading profile.

Stop the listener with Ctrl+C. `--telegram-poll-once` processes one pending batch and exits,
which is useful for a smoke test. Polling cursors and command-response plans survive restart.
Only one listener can own the cursor at a time. An existing webhook stops polling with a
clear error; the application does not delete the webhook or drop pending updates.

`/brief` reads existing recommendations and does not generate new ones. To produce a fresh
selection, run `.venv/bin/daily-tech` separately, then use `/brief` or `--send-brief` to deliver
it. Research therefore stays outside the command handler. Automatic scheduling and profile
editing through conversation are not implemented in this increment.

Delivery is recorded per bot, private chat, brief, and message part. Repeating `--send-brief`
or `/brief` does not resend a confirmed brief. After an explicit Telegram rejection, a later
request can retry the unsent part while skipping confirmed parts. Inspect the latest records:

```sh
.venv/bin/daily-tech --telegram-deliveries
```

Telegram has no application-supplied idempotency key for `sendMessage`. A timeout, malformed
confirmation, or crash between sending and saving the receipt can leave delivery uncertain.
The app records that state and holds the affected delivery without automatic resend. Check
the private chat and delivery records before resolving it; there is no force-resend command.
This conservative handling prevents automatic duplicates but does not guarantee exactly-once
delivery or automatic completion after an uncertain result. HTTP request URLs containing bot
tokens are kept out of the app's logs and error messages.

## Demo evaluation: frozen inputs and human review

Create an evaluation snapshot before changing preference behavior:

```sh
.venv/bin/daily-tech --eval-freeze
```

This reads the latest stored brief's full retrieved article pool and the current stored
profile. It writes a versioned `snapshot.json` below the ignored `output/evaluations/`
directory. It does not generate content, send Telegram messages, or change the database.
To freeze a particular run, pass its UUID; to work without PostgreSQL, pass a saved `run.json`
file instead. File-based export uses that report's profile. An explicit `--profile FILE`
overrides the evaluation snapshot only, without updating the production profile.

The snapshot records article bodies/hashes and metadata, an eligibility reference time,
profile, replay model, replay limits, and an editorial-code fingerprint. It includes every
accepted retrieved article in the source run, not only the selected three. Inaccessible or
rejected discovery candidates are not part of this corpus. Historical runs did not capture
every generation setting; replay settings are captured at export, not reconstructed as
verified historical settings.

Use the printed snapshot path to run a controlled comparison:

```sh
.venv/bin/daily-tech --eval-replay PATH_TO_SNAPSHOT_JSON
```

This calls OpenAI for generic and current-profile ranking using the same frozen article
pool. It shares production selection/summarization code and keeps quality floors, diversity,
reading budget, model, and summary audience/style fixed. The generic condition ranks for
broad practical technical usefulness without a personal profile. Only ranking preferences
vary; summaries remain adapted to the frozen profile so selection can be compared fairly.
No articles are rediscovered or re-filtered against today's date.

Each condition has its own bounded model-call/runtime budget from the snapshot. With default
limits, two conditions permit at most 14 model calls total, or 21 for three conditions.
Successful three-article briefs normally use four calls per condition. Each replay gets a new
attempt directory and preserves failures and partial results. A changed editorial-code
fingerprint requires a new snapshot; compare conditions generated with the same revision.

The attempt directory contains:

- `snapshot.json`, `result.json`: the inputs, profiles, outputs, events, durations, and returned
  usage. Hash checks detect accidental edits to frozen inputs and generated results.
- `generic_brief.md`, `current_brief.md`: evaluation-only briefs; neither is sent or recorded
  as a production recommendation.
- `review_guide.md`, `review.json`: a condition-label-free article worksheet and editable human
  ratings, metadata checks, and draft claim inventory. Raw results/claim labels expose the
  conditions, so this is not a double-blind study.
- `metrics.json`, `report.md`: calculated measures and a demo-ready explanation, initially
  showing **Not measured** wherever human review is missing.

Edit only `review.json` for review. Rate each unique selected article once from 1–5 on relevance,
difficulty fit, and practical value (1 poor, 3 mixed, 5 excellent). Shared articles reuse their
ratings across conditions. Null means unrated, not zero. The report shows each condition's
selected slots out of three and available rating counts; partial averages are explicitly
labelled. Rating deltas are withheld until both conditions have complete ratings for that
dimension. A descriptive difference is not proof of causal improvement.

For accuracy review, check the full saved source text, metadata, summaries, technical insights,
and relevance explanations. Draft claim rows use sentence splitting and may need to be split,
added, or marked non-factual. Supported claims require a matching saved-source passage; that
mechanical check alone does not establish semantic support. Contradicted/unverifiable/non-factual
labels require review notes. Set `claim_inventory_checked` only after reviewing the complete
condition. The support rate is supported divided by all reviewed factual claims, including
contradicted and unverifiable claims in the denominator. It remains unmeasured while the inventory
is unchecked or any claim remains pending. Metadata verification is verified divided by all
field checks, with incorrect and unverified fields reported separately; the rate is withheld
until those checks are reviewed.

Recalculate after reviewing, using the attempt directory printed by the replay command:

```sh
.venv/bin/daily-tech --eval-report PATH_TO_EVALUATION_ATTEMPT_DIRECTORY
```

Report calculation is local and requires neither a model key nor PostgreSQL. It never rewrites
model outputs or fills human labels automatically. Latency measures editorial replay only,
excluding discovery and delivery. Reported tokens are those returned by the provider; missing
usage is flagged and no monetary estimate is invented. Regression-test results and production
delivery reliability remain separate evidence.

A future corrected-profile comparison can be replayed with an explicit JSON profile:

```sh
.venv/bin/daily-tech --eval-replay PATH_TO_SNAPSHOT_JSON --eval-corrected-profile config/profile.json
```

This adds a third condition in a new attempt and does not update the real profile. It does not
prove conversational extraction or restart persistence; those checks will accompany the feedback
increment. Until measured, the report labels the correction effect as unmeasured. One user and
one corpus provide initial descriptive evidence, not a general benchmark or statistical claim.
The current stage captures the baseline before adding Telegram feedback and preference updates.

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
history, concurrency protection, idempotent saves, and failure records. Telegram tests mock all
API calls while testing the real database outbox, command cursor, authorization checks,
partial retries, and uncertain/crashed-send behavior. The database role must
be allowed to create schemas.

## Boundaries of this increment

`retrieval.py` owns discovery and verification; `editorial.py` owns ranking, selection,
and summarization; `llm.py` exposes a provider-neutral interface with an OpenAI adapter;
`pipeline.py` connects them through LangGraph; `cli.py` handles configuration and execution.
`storage.py` owns PostgreSQL access and migrations; `history.py` defines article identities
for repeat filtering. `telegram.py` owns transport/rendering; `telegram_service.py` handles
delivery and command dispatch; `telegram_storage.py` owns the outbox and polling state.
Profile revision history records explicit JSON imports. Conversational memory extraction,
feedback learning, deployed webhooks, scheduling, and web search are still future increments.
Database run snapshots are separate from any future LangGraph checkpoints.

`evaluation*.py` provides isolated corpus export, editorial replay, and human-review metrics;
these artifacts never enter production recommendation history or delivery.

Next increment: explicit feedback and conversational preference updates, evaluated against the
frozen baseline, then hosted webhooks and scheduled daily delivery.
