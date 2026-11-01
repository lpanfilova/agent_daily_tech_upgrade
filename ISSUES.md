# Open issues

## ACM-001: ACM Queue rejects automated retrieval

- **Status:** Open
- **Recorded:** 2026-10-31
- **Priority:** Low; does not block the first brief generator.
- **Affected source:** ACM Queue — https://queue.acm.org/

### Observed behavior

Local discovery returned HTTP `403 Forbidden` for the ACM Queue homepage. The previously
checked feed URLs returned `410 Gone` and `404 Not Found`. Other publishers continued
successfully; the generator recorded the source failure and skipped ACM.

Local reproduction: run `.venv/bin/daily-tech --discover-only`. The reported run is
`output/20261031T203926292704Z/run.json` (generated locally and excluded from Git).

### Current handling

Leave ACM configured and skip inaccessible content. The retriever supports public pages
and does not implement institutional login. A 403 alone does not establish whether
credentials or a subscription would resolve access.

### Follow-up

- Investigate an officially supported public feed, API, or other permitted retrieval method.
- Verify discovery and full article retrieval from the deployment environment.
- If reliable access is unavailable, disable the source with a documented reason.

### Completion criteria

Either ACM supplies discoverable articles with readable content and valid metadata through
a supported access method, or the source is explicitly disabled with its limitation documented.

## EDITORIAL-001: Improve novice relevance and claim-to-evidence support

- **Status:** Open
- **Recorded:** 2026-10-31
- **Priority:** Address before treating brief quality as accepted.
- **Evidence:** First completed live brief at `output/20261031T210244836674Z/brief.md`
  and its accompanying `run.json` (local, excluded from Git).

### Observed behavior

The generated brief selected a continual-learning research article for a novice interested
in agent harnesses and practical agent development. Its relevance explanation overstates
how directly a beginner can apply the research. Some insight evidence excerpts contain
titles, author information, or other text that does not directly substantiate the claim.
Exact excerpt presence alone does not establish factual support.

### Follow-up and completion criteria

- Distinguish approachable, actionable agent tutorials from adjacent model-training research
  in ranking. Explain prerequisites and label stretch reading explicitly if included.
- Improve evidence selection and check that the associated claim follows from its evidence
  and surrounding source context. Avoid title/byline snippets as technical evidence.
- Review the three summaries against their full source text, including numerical claims and
  relevance explanations, and obtain user feedback on usefulness and difficulty.

### Live-run fixes

Ranking now uses a required assessment field for each candidate, with original IDs attached
by the application. This resolves the first live run's omitted/incorrect-ID rejection.
Summary schemas now constrain evidence to short excerpts constructed from source text,
resolving failures caused by model-generated excerpts outside the word-count limit.
Regression tests cover both constraints; semantic support remains the open work above.

## MODEL-001: Investigate article-specific summary HTTP 400

- **Status:** Open
- **Recorded:** 2026-10-31
- **Evidence:** Database-backed run `6c618bac-15e1-4c9d-9a43-37b73cccb67b`,
  local report `output/20261031T215052019611Z/run.json`.

The summary request for the Hugging Face article `https://huggingface.co/blog/mistral-large4`
returned HTTP 400. Other requests succeeded with the same model and credentials. The run
recorded the rejection and completed with three alternative recommendations.

The same article-specific rejection recurred in the `current` condition of evaluation
`64c76ea0-8b1d-4b14-8d61-4c90885f29a1` (frozen snapshot `9e531cb387895c62`).
The condition still produced three articles. Its report flags token usage as incomplete
because the rejected call did not return usage; the failed attempt remains in `result.json`.

Follow up by recording sanitized provider error codes/parameters and checking the generated
article-specific response schema. The current generic error does not establish the cause.
Do not log API keys or authorization headers. Close after a regression check and a successful
request for the affected article or a documented, actionable exclusion reason.
