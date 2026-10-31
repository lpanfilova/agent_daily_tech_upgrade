"""Durable Telegram outbox, command plans, and polling cursor."""

from contextlib import contextmanager
from uuid import uuid4

from psycopg.types.json import Jsonb

from .storage import Repository, StorageError


class TelegramStore:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.connection = repository.connection

    @contextmanager
    def lock(self, *, polling=False):
        key = 72419613 if polling else 72419612
        if not self.connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)
        ).fetchone()["acquired"]:
            raise StorageError(
                "Another Telegram listener is active"
                if polling
                else "Another Telegram delivery is active; try again shortly"
            )
        try:
            yield
        finally:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def delivery(self, bot_id, chat_id, key):
        return self.connection.execute(
            """SELECT * FROM telegram_deliveries
            WHERE bot_id = %s AND chat_id = %s AND delivery_key = %s""",
            (bot_id, chat_id, key),
        ).fetchone()

    def prepare(self, bot_id, chat_id, key, messages, run_id=None):
        if not messages:
            raise ValueError("Delivery requires at least one message")
        with self.connection.transaction():
            delivery_id = uuid4()
            inserted = self.connection.execute(
                """INSERT INTO telegram_deliveries
                (delivery_id, bot_id, chat_id, delivery_key, run_id) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (bot_id, chat_id, delivery_key) DO NOTHING""",
                (delivery_id, bot_id, chat_id, key, run_id),
            ).rowcount
            if inserted:
                for index, body in enumerate(messages):
                    self.connection.execute(
                        """INSERT INTO telegram_delivery_parts(delivery_id, part_index, body)
                        VALUES (%s, %s, %s)""",
                        (delivery_id, index, body),
                    )
            return self.delivery(bot_id, chat_id, key)

    def parts(self, delivery_id):
        return list(
            self.connection.execute(
                """SELECT * FROM telegram_delivery_parts
            WHERE delivery_id = %s ORDER BY part_index""",
                (delivery_id,),
            )
        )

    def recover(self, delivery_id):
        # The delivery lock guarantees no other sender is still working on these parts.
        with self.connection.transaction():
            self.connection.execute(
                """UPDATE telegram_delivery_parts SET status = 'uncertain',
                error = 'Previous sender stopped before confirming delivery', updated_at = now()
                WHERE delivery_id = %s AND status = 'sending'""",
                (delivery_id,),
            )
            parts = self.parts(delivery_id)
            if any(p["status"] == "uncertain" for p in parts):
                status = "uncertain"
            elif all(p["status"] == "sent" for p in parts):
                status = "sent"
            else:
                status = "pending"
            self.connection.execute(
                "UPDATE telegram_deliveries SET status = %s, updated_at = now() WHERE delivery_id = %s",
                (status, delivery_id),
            )
            return status

    def mark_part(self, delivery_id, index, status, *, message_id=None, error=None):
        with self.connection.transaction():
            self.connection.execute(
                """UPDATE telegram_delivery_parts SET status = %s,
                message_id = %s, error = %s, attempts = attempts + %s, updated_at = now()
                WHERE delivery_id = %s AND part_index = %s""",
                (status, message_id, error, int(status == "sending"), delivery_id, index),
            )
            self.connection.execute(
                "UPDATE telegram_deliveries SET status = %s, updated_at = now() WHERE delivery_id = %s",
                ("sending" if status == "sent" else status, delivery_id),
            )

    def mark_sent(self, delivery_id):
        self.connection.execute(
            "UPDATE telegram_deliveries SET status = 'sent', updated_at = now() WHERE delivery_id = %s",
            (delivery_id,),
        )

    def next_offset(self, bot_id):
        row = self.connection.execute(
            "SELECT next_update_id FROM telegram_polling_state WHERE bot_id = %s", (bot_id,)
        ).fetchone()
        return row["next_update_id"] if row else 0

    def advance(self, bot_id, update_id):
        self.connection.execute(
            """INSERT INTO telegram_polling_state(bot_id, next_update_id) VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE SET
            next_update_id = GREATEST(telegram_polling_state.next_update_id, EXCLUDED.next_update_id),
            updated_at = now()""",
            (bot_id, update_id + 1),
        )

    def update(self, bot_id, update_id):
        return self.connection.execute(
            "SELECT * FROM telegram_updates WHERE bot_id = %s AND update_id = %s",
            (bot_id, update_id),
        ).fetchone()

    def save_plan(self, bot_id, update_id, plan):
        self.connection.execute(
            """INSERT INTO telegram_updates(bot_id, update_id, plan) VALUES (%s, %s, %s)
            ON CONFLICT (bot_id, update_id) DO NOTHING""",
            (bot_id, update_id, Jsonb(plan)),
        )
        return self.update(bot_id, update_id)

    def complete_update(self, bot_id, update_id):
        with self.connection.transaction():
            self.connection.execute(
                "UPDATE telegram_updates SET done = true WHERE bot_id = %s AND update_id = %s",
                (bot_id, update_id),
            )
            self.advance(bot_id, update_id)

    def list_deliveries(self):
        return list(
            self.connection.execute("""SELECT d.delivery_id, d.run_id, d.status,
            count(p.part_index) AS parts, count(p.message_id) AS confirmed_parts
            FROM telegram_deliveries d JOIN telegram_delivery_parts p USING(delivery_id)
            GROUP BY d.delivery_id ORDER BY d.created_at DESC LIMIT 20""")
        )
