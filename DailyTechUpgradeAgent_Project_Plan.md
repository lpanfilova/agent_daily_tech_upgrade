# DailyTechUpgradeAgent — Project Plan

## 1. Full project: vision and architecture

**Purpose:** A proactive, single-user AI coworker that independently delivers **three authoritative technical articles daily** and consistently improves its recommendations through **custom-built, persistent personalization**. It is a standalone application, separate from the career-coaching agent, designed to evolve into a portfolio-quality product. It does **not** assess whether the user has learned the material.

### Product and research principles

- **Experience:** Telegram-first: automated daily briefs, natural-language conversation, article feedback, and inspectable/correctable memory. No web dashboard or email requirement; other delivery channels can be added later.
- **Brief:** Three distinct articles, sized for **15–20 minutes**, each with title, publication date, publisher, original URL, source-grounded summary, technical insights, and relevance to the user's interests or career goals.
- **Sources and discovery:** Authoritative publishers (OpenAI, Anthropic, Microsoft, Google, AWS, IBM, LangGraph, IEEE, ACM, and other credible sources). Compare **curated RSS**, **LLM-directed search**, and **hybrid RSS + search** during evaluation; use **hybrid** for normal daily delivery, not three parallel daily pipelines.
- **Editorial controls:** Validate article metadata, access, credibility, and duplicates; try legitimate alternative versions when necessary and exclude articles that cannot be summarized reliably. Prefer the past **seven days**, allowing older exceptional material. Consider depth, originality, practical value, diversity, novelty, and personal relevance; maintain minimum quality standards regardless of preference weights.
- **Agent autonomy:** Permit search refinement, further investigation, and rejection of weak candidates, with configurable limits on searches, retries, runtime, and spending. Keep article retrieval and verification independent of the LLM provider.

### Custom personalization and memory policy

- **Representation:** A hybrid of structured attributes and natural-language memories, each with provenance, explicit/inferred status, confidence, and supporting evidence. Maintain global defaults with **topic-specific overrides** for interests, proficiency, depth, style, and goals.
- **Learning:** Begin with short, skippable onboarding. Explicit statements outweigh inferences; new ambiguous interests are tentative. Incorporate **explicit** likes, dislikes, reasons, and conversational corrections; strengthen or revise preferences cautiously. Clarify significant contradictions rather than silently overriding them.
- **Retrieval:** Consistently load a compact core profile; retrieve relevant contextual memories when needed. Preserve original conversations as evidence so interpretations can be checked or reprocessed.
- **Retention and control:** Consolidate history based on **capacity and importance, not age**; do not expire valid preferences merely because of inactivity. Support conversational inspection/correction and `/profile`, `/preferences`, `/forget` commands.
- **Ranking evolution:** Use weighted personalization with room for adjacent topics; progressively adapt preference weights and the exploration/familiarity balance from explicit feedback. **No passive behavior or link-click tracking** in V1; consider it later only with consent.

### Technical architecture

- **Runtime:** Python + FastAPI modular monolith, **LangGraph** for research and conversational/personalization workflows.
- **Persistence:** PostgreSQL + JSONB for profiles, memories/evidence, conversations, articles, feedback, recommendations, and delivery records. Keep LangGraph checkpoints distinct from long-term user memory; semantic retrieval (e.g., pgvector) can be added as history grows.
- **Model integration:** Provider-independent adapter for OpenAI, Gemini, or future LLMs; agent/business logic must not depend on one provider's proprietary search tools.
- **Hosting:** **Heroku**, Telegram webhooks for conversation, and **Heroku Scheduler** for proactive daily work. Keep research outside webhook requests and make delivery idempotent.
- **Module boundaries:** Separate Telegram transport, discovery, verification, ranking, summarization, preference policy, storage, and model integrations; allow more sophisticated memory or orchestration implementations later without rewriting the product.
- **Evaluation strategy:** Compare discovery methods under the same user profiles, ranking, and summarization conditions using human-rated relevance/quality, factual and metadata accuracy, diversity, novelty, latency, and API cost. Independently test memory persistence, corrections, and contradictions; compare personalized recommendations against a non-personalized baseline. Improve preference inference, conflict resolution, memory consolidation/retrieval, and editorial ranking incrementally based on these results.

## 2. V1: complete working flow to build today

1. **Onboarding and conversation:** Skippable setup for technical interests, experience, and article style; natural-language requests and feedback; working profile-management commands.
2. **Persistent preferences:** Store original messages, a basic structured profile, tentative free-text memories with evidence, and corrections. Extract explicit preferences from conversations and apply them after a new conversation or application restart.
3. **Daily research:** A scheduled graph collects articles from trusted RSS feeds, uses web search as a fallback, performs basic URL/date/content/duplicate checks, ranks candidates with one LLM provider, and prepares the three-article brief with verified references and personalized explanations.
4. **Proactive delivery and feedback:** Send the brief automatically through Telegram; support like/dislike buttons and free-text reasons. Save briefs and execution/delivery status; avoid duplicate sends and handle basic failures/retries.
5. **Operational foundation:** Working webhook and separate scheduled job, one replaceable model adapter, database migrations, environment-based secrets, bounded tool usage, and essential error/usage logs.
6. **Initial verification and demo evaluation:** Demonstrate a complete run and test conversation persistence, preference correction affecting the next brief, valid article metadata/URLs, and duplicate prevention. Produce the small, reproducible evaluation report described below to explain how agent effectiveness and accuracy are measured. Full three-strategy benchmarking and advanced personalization quality improvements follow V1.

## 3. V1 demo: measuring agent effectiveness and accuracy

**Demo objective:** Show how recommendation usefulness, factual support, personalization, and operational reliability are measured, with actual observations and inspectable evidence. This evaluates the agent's recommendations, not whether the user learned the material.

### Measurements

| Dimension | V1 measurement | Evidence and interpretation |
| --- | --- | --- |
| Recommendation usefulness | Explicit human ratings from 1–5 for relevance, difficulty fit, and practical value; report each dimension separately. | Store ratings per article and evaluation condition. Anchor 1 as a poor fit, 3 as a mixed fit, and 5 as an excellent fit. Show the number of rated articles and returned slots out of three, so a short brief cannot hide missing coverage. |
| Factual support | Supported factual claims divided by all reviewed factual claims in the final brief. | Identify factual claims in summaries, technical insights, and relevance explanations; review against the original full article and retain the supporting passage or a review note. Label supported, contradicted, or unverifiable/insufficiently supported. Unverifiable does not mean false. Exact excerpt presence alone is not proof of support. |
| Metadata accuracy | Verified field checks divided by all reviewed field checks for title, publisher, original URL, and publication date. | Record which fields were checked, discrepancies, and unverified fields. An inaccessible source is an access failure, not automatically a false factual claim. |
| Personalization effectiveness | Compare human-rated recommendations from a generic baseline, the current profile, and a profile after one explicit correction. | Use the same frozen article pool and generation conditions. Report descriptive score differences and whether the correction persisted and reached the next ranking request. Do not assume a change must be an improvement. |
| Operational reliability and efficiency | Completed runs divided by attempted runs; confirmed delivery outcomes, duplicate-send checks, latency, and token usage. | Use run and delivery records, retaining partial, failed, and uncertain outcomes. Automated regression results are separate evidence of tested software behavior, not a recommendation or factual-accuracy score. Report estimated monetary cost only if model prices and their effective date are available; otherwise report tokens. |

Article likes/dislikes and free-text reasons are ongoing product feedback. The 1–5 ratings and claim reviews above are a separate, explicit evaluation task; do not infer them from clicks, silence, or a single like/dislike.

### Small controlled comparison for today's build

1. **Freeze the inputs before changing personalization.** Export one existing run's retrieved candidate articles, including full extracted text, URLs, dates, retrieval time, and a content hash for each article. Keep all eligible candidates, not only the selected three. Save the current profile, model/configuration, and prompt version. Evaluate eligibility as of this snapshot rather than rediscovering articles later.
2. **Create evaluation-only runs.** Add a small command that replays the frozen pool for three conditions: a generic editorial baseline, the current profile, and the profile after an explicit conversational correction. Keep the model, editorial policy, quality floor, reading-time budget, diversity rules, and summarization settings fixed; vary the preference input to ranking. Use the same summary style when comparing selection quality. Record any unavoidable differences.
3. **Review the selections.** The user rates the baseline and personalized selections for relevance, difficulty fit, and practical value. Hide condition labels during rating where practical and flag articles shared across conditions. Collect ratings for the corrected-profile condition as well; report missing ratings as pending rather than inventing scores.
4. **Exercise the memory loop.** Give a correction in Telegram, for example: “Prioritize beginner implementation tutorials over model-training research.” Preserve the original message, record the interpreted preference, confirm what changed, restart the app, and verify that the corrected profile reaches the next evaluation ranking request.
5. **Audit the final brief.** Review the corrected-profile brief's metadata and factual claims against the saved full source text. Check numerical claims and any claims about how readily a novice can apply the material. Address observed failures in `EDITORIAL-001` before describing the reviewed brief as verified.
6. **Export a demo report.** Include the corpus/snapshot ID, condition and profile versions, model/configuration, selected articles, human ratings, claim and metadata reviews, coverage, run outcomes, latency, token usage, and descriptive comparisons. Retain failed attempts and known limitations. Raw evidence must remain inspectable.

Evaluation runs and their ratings must remain separate from normal recommendation history and Telegram delivery. Replaying a corpus must not send messages, mark articles recommended in production, or overwrite the real profile. The user's actual preference correction is an explicit production update; evaluation conditions read immutable copies of the relevant profile versions.

**V1 acceptance:** A report can be regenerated from the recorded inputs and review labels; missing review data is visible; the before/after correction and restart checks pass; actual usefulness and accuracy results, including shortcomings, can be explained. Model-generated outputs may vary between replays, so preserve each output rather than promising identical results. No evaluation scores or improvements are claimed until measured.

**Limitations to state in the demo:** One user, one small corpus, and a few briefs provide initial descriptive evidence. They do not establish statistical significance, generalization, or a reliable causal improvement. Broader held-out evaluation, multiple trials, discovery-strategy comparisons, and advanced preference learning follow V1.

## 4. Current progress and next build order

Checkpoint as of 2026-10-31:

- **Working:** Terminal research from trusted RSS/official listings; basic metadata/content checks; personalized ranking and summaries; PostgreSQL profiles, revisions, articles, run history, and recommendation deduplication; private-chat Telegram delivery, `/start`, `/brief`, `/profile`, and durable delivery/polling state. Frozen-corpus export, isolated generic/current-profile replay, an optional corrected-profile condition, human-review templates, and report calculation are implemented. The latest verified regression suite has 62 passing tests.
- **Still to build or complete:** Source-based claim and metadata reviews for the captured evaluation; article feedback, conversational preferences and evidence, memory-management commands, web-search fallback, hosted FastAPI webhooks, and daily scheduling. Initial human usefulness ratings have been collected and the comparison report recalculated. The existing structured profile is updated through explicit JSON imports; conversational learning and its restart/effectiveness demonstration are not implemented yet.
- **Known quality work:** Track novice relevance and claim support in `ISSUES.md` (`EDITORIAL-001`); diagnose article-specific model errors (`MODEL-001`). ACM access (`ACM-001`) does not block this increment.

Recommended next increment: **capture a baseline, then build explicit feedback and preference corrections with evaluation alongside them.**

1. **Baseline captured and rated:** Exported 16 retrieved articles and the current profile in snapshot `9e531cb387895c62`. Evaluation `64c76ea0-8b1d-4b14-8d61-4c90885f29a1` produced three articles in each condition, with one shared article and five unique articles. The user rated all five for relevance, difficulty fit, and practical value; the report now contains the initial descriptive comparison. Factual support and metadata accuracy remain unmeasured. Artifacts and human ratings are local under the ignored `output/evaluations/` directory. This small comparison does not establish a reliable or causal improvement.
2. Add persistent article likes/dislikes and free-text reasons with unambiguous article association. Store original authorized messages as evidence.
3. Add a bounded conversational preference workflow, explicit-change confirmation, contradiction handling, and `/preferences` and `/forget` controls. Load accepted preferences into ranking after restart; do not silently equate an article dislike with a permanent topic ban.
4. Run the controlled comparison, collect the user's ratings and claim review, refine the observed editorial failures, and produce the demo report.
5. Complete web-search fallback, hosted webhooks, and separate scheduled research/delivery to finish the proactive V1 flow.

No new account setup is needed for the next increment. The manual contribution is the user's small set of article ratings and source-based claim reviews. Account/service setup and scheduling choices belong to the later deployment increment. Advanced adaptive weighting and large-scale benchmarking can wait; explicit feedback, persistent corrections, and honest initial evaluation remain in today's V1 scope.
