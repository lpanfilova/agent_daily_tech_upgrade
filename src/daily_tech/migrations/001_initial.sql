CREATE TABLE profiles (
    profile_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE profile_revisions (
    revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id text NOT NULL REFERENCES profiles(profile_id),
    payload jsonb NOT NULL,
    origin text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runs (
    run_id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'discovery_only', 'complete', 'partial', 'failed')),
    discovery_only boolean NOT NULL,
    allow_repeats boolean NOT NULL,
    profile_snapshot jsonb NOT NULL,
    report jsonb NOT NULL,
    brief text,
    error text
);

CREATE TABLE articles (
    url text PRIMARY KEY,
    title_key text NOT NULL,
    content_hash text NOT NULL,
    publisher text NOT NULL,
    published_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE run_articles (
    run_id uuid NOT NULL REFERENCES runs(run_id),
    article_url text NOT NULL REFERENCES articles(url),
    rating jsonb,
    PRIMARY KEY (run_id, article_url)
);

CREATE TABLE recommendations (
    run_id uuid NOT NULL REFERENCES runs(run_id),
    article_url text NOT NULL REFERENCES articles(url),
    position smallint NOT NULL CHECK (position BETWEEN 1 AND 3),
    content_hash text NOT NULL,
    title_key text NOT NULL,
    summary jsonb NOT NULL,
    rating jsonb NOT NULL,
    PRIMARY KEY (run_id, article_url),
    UNIQUE (run_id, position)
);

CREATE INDEX recommendations_article_idx ON recommendations(article_url);
CREATE INDEX runs_started_idx ON runs(started_at DESC);
