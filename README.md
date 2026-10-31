# Daily Tech Upgrade Agent

Known source limitations and follow-up work are tracked in [ISSUES.md](ISSUES.md).

A personal technical-reading companion. This first increment runs in the terminal:
discover articles → retrieve and validate full text → rank for your profile → summarize →
save a brief and an inspectable run report.

The initial profile prioritizes **AI agent harness creation, practical agent use, and agent
development at novice level**. Harness creation means the tools, state, execution loop,
evaluation, and controls around an agent. Edit `config/profile.example.json`, or copy it to
the ignored `config/profile.json` and pass `--profile config/profile.json`.

## Run locally

Python 3.11 or later is required. Run commands from the repository root.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Inspect real articles without a model key or model charges:

```sh
.venv/bin/daily-tech --discover-only
```

For a generated brief, copy `.env.example` to `.env` if `.env` does not already exist.
Add your API key locally:

```dotenv
OPENAI_API_KEY=your-key-here
OPENAI_MODEL=gpt-4.1-mini
```

Then run:

```sh
.venv/bin/daily-tech
```

The model can be changed in `.env` or with `--model`. It must be available to your API account
and support [Responses structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The initial adapter follows that API; retrieval does not use provider search tools.

Every run writes a separate directory under the ignored `output/` directory:

- `brief.md`: the reading brief, or an article list for discovery-only runs.
- `run.json`: profile, candidate metadata, retrieved text, ratings, recommendations,
  exact evidence excerpts, source failures, rejection reasons, and model token usage.

Exit codes: `0` for a complete brief or successful discovery, `2` for a partial brief or
discovery with no readable articles, and `1` for a configuration or run failure.

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
text. Excerpts are retained for review in `run.json`. This checks evidence presence;
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

## Boundaries of this increment

`retrieval.py` owns discovery and verification; `editorial.py` owns ranking, selection,
and summarization; `llm.py` exposes a provider-neutral interface with an OpenAI adapter;
`pipeline.py` connects them through LangGraph; `cli.py` handles configuration and execution.
There is no database, cross-run recommendation history, Telegram integration, scheduling,
web search, or conversational memory yet. Run reports are local artifacts, not the future
persistent user-memory implementation.

Next increment: persist profiles, articles, recommendations, and run history in PostgreSQL,
then prevent previously recommended articles from recurring across runs.
