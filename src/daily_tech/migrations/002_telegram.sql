CREATE TABLE telegram_deliveries (
    delivery_id uuid PRIMARY KEY,
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL CHECK (chat_id > 0),
    delivery_key text NOT NULL,
    run_id uuid REFERENCES runs(run_id),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bot_id, chat_id, delivery_key)
);

CREATE TABLE telegram_delivery_parts (
    delivery_id uuid NOT NULL REFERENCES telegram_deliveries(delivery_id),
    part_index integer NOT NULL CHECK (part_index >= 0),
    body text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
    message_id bigint,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_id, part_index)
);

CREATE TABLE telegram_polling_state (
    bot_id bigint PRIMARY KEY,
    next_update_id bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE telegram_updates (
    bot_id bigint NOT NULL,
    update_id bigint NOT NULL,
    plan jsonb NOT NULL,
    done boolean NOT NULL DEFAULT false,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, update_id)
);
