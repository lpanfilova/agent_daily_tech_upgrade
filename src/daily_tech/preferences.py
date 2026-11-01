"""Explicit, evidence-linked preferences and article-specific feedback."""

from typing import Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import Field

from .models import StrictModel


class PreferenceProposal(StrictModel):
    intent: Literal["preference", "feedback", "unclear"]
    text: str = Field(max_length=500)
    evidence_quote: str = Field(max_length=1000)
    topic: str | None = Field(max_length=100)
    global_depth: Literal["novice", "intermediate", "advanced"] | None
    supersedes: list[int] = Field(max_length=10)
    confidence: float = Field(ge=0, le=1)
    clarification: str = Field(max_length=400)


class PreferenceStore:
    def __init__(self, repository):
        self.repository = repository
        self.connection = repository.connection

    def record_message(self, key, text, *, origin="telegram", context=None):
        return self.connection.execute(
            """INSERT INTO conversation_messages
            (message_id, source_key, origin, body, context) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_key) DO UPDATE SET source_key = EXCLUDED.source_key
            RETURNING message_id""",
            (uuid4(), key, origin, text, Jsonb(context or {})),
        ).fetchone()["message_id"]

    def memories(self, *, include_pending=False):
        return list(
            self.connection.execute(
                """SELECT * FROM preference_memories
            WHERE status = ANY(%s) ORDER BY preference_id""",
                (["active", "pending"] if include_pending else ["active"],),
            )
        )

    def propose(self, source, proposal, *, usage=None):
        if proposal.global_depth and proposal.topic:
            raise ValueError("A topic-specific preference cannot change your global reading level")
        row = self.connection.execute(
            "SELECT * FROM preference_memories WHERE source_message_id = %s", (source,)
        ).fetchone()
        if row:
            return row
        if len(self.memories(include_pending=True)) >= 50:
            raise ValueError(
                "Please confirm, reject, or forget existing preferences before adding more"
            )
        active = {m["preference_id"] for m in self.memories()}
        if not set(proposal.supersedes) <= active:
            raise ValueError(
                "The preferences to replace have changed; please restate the correction"
            )
        return self.connection.execute(
            """INSERT INTO preference_memories
            (source_message_id, body, topic, global_depth, supersedes, confidence, status, model_usage)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING *""",
            (
                source,
                proposal.text,
                proposal.topic,
                proposal.global_depth,
                proposal.supersedes,
                proposal.confidence,
                Jsonb(usage or []),
            ),
        ).fetchone()

    def confirm(self, id, source):
        with self.connection.transaction():
            memory = self.connection.execute(
                "SELECT * FROM preference_memories WHERE preference_id = %s FOR UPDATE", (id,)
            ).fetchone()
            if not memory or memory["status"] not in ("pending", "active"):
                raise ValueError("That preference is not awaiting confirmation")
            if memory["status"] == "active":
                return memory
            if len(self.memories()) >= 30:
                raise ValueError("Please forget an active preference before adding another")
            replaced = list(
                self.connection.execute(
                    "SELECT preference_id FROM preference_memories WHERE preference_id = ANY(%s) AND status = 'active' FOR UPDATE",
                    (memory["supersedes"],),
                )
            )
            if {r["preference_id"] for r in replaced} != set(memory["supersedes"]):
                raise ValueError(
                    "The preferences to replace have changed; please restate the correction"
                )
            self.connection.execute(
                "UPDATE preference_memories SET status = 'superseded', updated_at = now() WHERE preference_id = ANY(%s)",
                (memory["supersedes"],),
            )
            return self.connection.execute(
                """UPDATE preference_memories SET status = 'active', explicit = true,
                confidence = 1, confirmation_message_id = %s, updated_at = now()
                WHERE preference_id = %s RETURNING *""",
                (source, id),
            ).fetchone()

    def reject(self, id):
        if not self.connection.execute(
            "UPDATE preference_memories SET status = 'rejected', updated_at = now() WHERE preference_id = %s AND status = 'pending'",
            (id,),
        ).rowcount:
            raise ValueError("That preference is not awaiting confirmation")

    def forget(self, id):
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT * FROM preference_memories WHERE preference_id = %s FOR UPDATE", (id,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown preference ID")
            self.connection.execute(
                """UPDATE preference_memories SET status = 'forgotten', body = NULL,
                topic = NULL, global_depth = NULL, supersedes = '{}', confidence = 0,
                updated_at = now() WHERE preference_id = %s""",
                (id,),
            )
            self.connection.execute(
                "UPDATE conversation_messages SET body = NULL, context = '{}' WHERE message_id = ANY(%s)",
                ([s for s in (row["source_message_id"], row["confirmation_message_id"]) if s],),
            )

    def add_feedback(self, source, run_id, position, vote):
        row = self.connection.execute(
            """SELECT article_url FROM recommendations
            WHERE run_id = %s AND position = %s""",
            (run_id, position),
        ).fetchone()
        if not row:
            raise ValueError("That article is not part of a saved recommendation")
        return self.connection.execute(
            """INSERT INTO article_feedback
            (source_message_id, run_id, article_url, article_position, vote) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_message_id) DO UPDATE SET source_message_id = EXCLUDED.source_message_id
            RETURNING feedback_id""",
            (source, run_id, row["article_url"], position, vote),
        ).fetchone()["feedback_id"]

    def add_reason(self, source, id, text):
        if not self.connection.execute(
            "SELECT 1 FROM article_feedback WHERE feedback_id = %s", (id,)
        ).fetchone():
            raise ValueError("Unknown feedback ID; use the F number returned after a rating")
        self.connection.execute(
            """INSERT INTO feedback_reasons(feedback_id, source_message_id, body)
            VALUES (%s, %s, %s) ON CONFLICT (source_message_id) DO NOTHING""",
            (id, source, text),
        )

    def effective_profile(self, base):
        active = self.memories()
        notes = []
        for memory in active:
            if memory["global_depth"]:
                base = base.model_copy(update={"depth": memory["global_depth"]})
            notes.append(
                f"P{memory['preference_id']} [scope: {memory['topic'] or 'general reading'}]: {memory['body']}"
            )
        signals = list(
            self.connection.execute("""SELECT latest.*, a.payload->>'title' AS title,
            (SELECT body FROM feedback_reasons r WHERE r.feedback_id = latest.feedback_id
             ORDER BY reason_id DESC LIMIT 1) AS reason
            FROM (SELECT DISTINCT ON (run_id, article_position) * FROM article_feedback
                  ORDER BY run_id, article_position, feedback_id DESC) latest
            JOIN articles a ON a.url = latest.article_url ORDER BY feedback_id DESC LIMIT 5""")
        )
        style = base.style
        if notes:
            style += (
                "\n\nSaved explicit reading preferences (override conflicting general defaults only within their stated scope):\n"
                + "\n".join(notes)
            )
        if signals:
            style += "\n\nRecent article-specific feedback (weak contextual signals; do not infer a permanent topic ban from one reaction):\n"
            style += "\n".join(
                f"{s['vote']}: {s['title'][:200]}; reason: {(s['reason'] or 'not provided')[:500]}"
                for s in signals
            )
        return base.model_copy(update={"style": style})

    def inspection(self):
        rows = self.memories(include_pending=True)
        if not rows:
            return "No saved preference memories yet. Tell me what technical reading you prefer."
        sections = []
        for memory in rows:
            source = self.connection.execute(
                "SELECT origin, body FROM conversation_messages WHERE message_id = %s",
                (memory["source_message_id"],),
            ).fetchone()
            evidence = (source["body"] or "redacted")[:200]
            sections.append(
                f"P{memory['preference_id']} [{memory['status']}; {'confirmed' if memory['explicit'] else 'tentative'}; confidence {memory['confidence']:.2f}]\n"
                f"Scope: {memory['topic'] or 'general reading'}\n{memory['body']}\nEvidence ({source['origin']}): {evidence}"
            )
        return (
            "Saved preferences\n\n"
            + "\n\n".join(sections)
            + "\n\n/confirm Pn · /reject Pn · /forget Pn"
        )
