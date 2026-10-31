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
6. **Initial verification:** Demonstrate a complete run and test conversation persistence, preference correction affecting the next brief, valid article metadata/URLs, and duplicate prevention. Full three-strategy benchmarking and advanced personalization quality improvements follow V1.
