CREATE TABLE conversation_messages (
    message_id uuid PRIMARY KEY,
    source_key text NOT NULL UNIQUE,
    origin text NOT NULL CHECK (origin IN ('telegram', 'chat')),
    body text,
    context jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE preference_memories (
    preference_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_message_id uuid NOT NULL UNIQUE REFERENCES conversation_messages(message_id),
    confirmation_message_id uuid REFERENCES conversation_messages(message_id),
    body text,
    topic text,
    global_depth text CHECK (global_depth IN ('novice', 'intermediate', 'advanced')),
    supersedes bigint[] NOT NULL DEFAULT '{}',
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    explicit boolean NOT NULL DEFAULT false,
    status text NOT NULL CHECK (status IN ('pending', 'active', 'superseded', 'rejected', 'forgotten')),
    model_usage jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE article_feedback (
    feedback_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_message_id uuid NOT NULL UNIQUE REFERENCES conversation_messages(message_id),
    run_id uuid NOT NULL,
    article_url text NOT NULL,
    article_position smallint NOT NULL CHECK (article_position BETWEEN 1 AND 3),
    vote text NOT NULL CHECK (vote IN ('up', 'down')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, article_url) REFERENCES recommendations(run_id, article_url)
);

CREATE TABLE feedback_reasons (
    reason_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feedback_id bigint NOT NULL REFERENCES article_feedback(feedback_id),
    source_message_id uuid NOT NULL UNIQUE REFERENCES conversation_messages(message_id),
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE telegram_delivery_parts ADD COLUMN reply_markup jsonb;
ALTER TABLE telegram_delivery_parts ADD COLUMN article_position smallint
    CHECK (article_position BETWEEN 1 AND 3);
ALTER TABLE telegram_delivery_parts ADD COLUMN buttons_installed_at timestamptz;
