import asyncio

import pytest
from test_storage import database as database
from test_storage import repository
from test_telegram import BOT, CHAT, FakeClient, saved_report, update

from daily_tech.preference_conversation import PreferenceConversation
from daily_tech.preferences import PreferenceProposal, PreferenceStore
from daily_tech.telegram import feedback_buttons
from daily_tech.telegram_service import TelegramService
from daily_tech.telegram_storage import TelegramStore


def proposal(text="Prefer beginner implementation tutorials", **changes):
    data = dict(
        intent="preference",
        text=text,
        evidence_quote=text,
        topic=None,
        global_depth=None,
        supersedes=[],
        confidence=1,
        clarification="",
    )
    return PreferenceProposal(**(data | changes))


def initialize(database):
    repo = repository(database)
    repo.migrate()
    report = saved_report(repo)
    repo.set_profile(report.profile, "initial_import")
    return repo


class InteractiveClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.markups, self.edits, self.callbacks = [], [], []

    async def send(self, text, reply_markup=None):
        self.markups.append(reply_markup)
        return await super().send(text, reply_markup)

    async def edit_markup(self, id, markup):
        self.edits.append((id, markup))

    async def answer_callback(self, id):
        self.callbacks.append(id)


def test_confirmed_memory_survives_restart_and_forget_stops_retrieval(database):
    with initialize(database) as repo:
        store = PreferenceStore(repo)
        original = repo.get_profile()
        source = store.record_message("source", "Prefer beginner implementation tutorials")
        memory = store.propose(source, proposal())
        assert repo.get_profile() == original
        confirmation = store.record_message("confirmation", f"/confirm P{memory['preference_id']}")
        store.confirm(memory["preference_id"], confirmation)
    with repository(database) as repo:
        store = PreferenceStore(repo)
        assert "Prefer beginner implementation tutorials" in repo.get_profile().style
        assert store.memories()[0]["explicit"]
        assert "Evidence (telegram)" in store.inspection()
        store.forget(memory["preference_id"])
        assert repo.get_profile() == original
        assert not store.memories()
        assert (
            repo.connection.execute(
                "SELECT body FROM conversation_messages WHERE message_id = %s", (source,)
            ).fetchone()["body"]
            is None
        )


def test_conflicts_require_confirmation_and_superseded_preferences_do_not_return(database):
    with initialize(database) as repo:
        store = PreferenceStore(repo)
        a = store.record_message("a", "Global level advanced")
        first = store.propose(a, proposal("Global level advanced", global_depth="advanced"))
        store.confirm(first["preference_id"], a)
        assert repo.get_profile().depth == "advanced"
        b = store.record_message("b", "Actually global level novice")
        next = store.propose(
            b,
            proposal(
                "Actually global level novice",
                global_depth="novice",
                supersedes=[first["preference_id"]],
            ),
        )
        assert repo.get_profile().depth == "advanced"
        store.confirm(next["preference_id"], b)
        assert repo.get_profile().depth == "novice"
        assert len(store.memories()) == 1
        store.forget(next["preference_id"])
        assert repo.get_profile().depth == "novice"  # Original base, not superseded advanced.
        with pytest.raises(ValueError, match="topic-specific"):
            store.propose(
                store.record_message("topic", "Advanced at Python"),
                proposal("Advanced at Python", topic="Python", global_depth="advanced"),
            )


def test_commands_are_idempotent_and_original_messages_are_preserved(database):
    client = InteractiveClient()
    with initialize(database) as repo:
        service = TelegramService(client, TelegramStore(repo), BOT)
        asyncio.run(
            service.process_update(update(10, "/remember Prefer beginner implementation tutorials"))
        )
        memory = service.preferences.memories(include_pending=True)[0]
        assert not service.preferences.memories()
        asyncio.run(service.process_update(update(11, f"/confirm P{memory['preference_id']}")))
        assert "Prefer beginner" in repo.get_profile().style
        asyncio.run(service.process_update(update(12, "/preferences")))
        assert "confirmed" in client.sent[-1]
    with repository(database) as repo:
        service = TelegramService(client, TelegramStore(repo), BOT)
        count = len(client.sent)
        asyncio.run(service.process_update(update(11, f"/confirm P{memory['preference_id']}")))
        assert len(client.sent) == count
        asyncio.run(service.process_update(update(13, f"/forget P{memory['preference_id']}")))
        assert not service.preferences.memories()
        asyncio.run(
            service.process_update(update(10, "/remember Prefer beginner implementation tutorials"))
        )
        assert not service.preferences.memories()


def test_natural_preference_is_evidence_checked_and_requires_confirmation(database):
    class Model:
        usage = [{"usage": {"input_tokens": 20, "output_tokens": 10}}]

        async def generate(self, instructions, data, schema):
            return proposal(data["message"])

    with initialize(database) as repo:
        store = PreferenceStore(repo)
        conversation = PreferenceConversation(store, Model)
        text = "Prefer beginner implementation tutorials"
        source = store.record_message("natural", text)
        interpreted = asyncio.run(conversation.interpret(source, text))
        reply = conversation.apply(source, text, interpreted)
        assert "/confirm P" in reply
        assert not store.memories()
        assert "Saved explicit" not in repo.get_profile().style
        bad = proposal("Invented global preference", evidence_quote="Invented evidence")
        assert "couldn’t verify" in conversation.apply(store.record_message("bad", text), text, bad)
        assert len(store.memories(include_pending=True)) == 1


def callback(report, *, sender=CHAT, position=1):
    return {
        "update_id": 20,
        "callback_query": {
            "id": "query-20",
            "from": {"id": sender, "is_bot": False},
            "message": {"message_id": 2, "chat": {"id": CHAT, "type": "private"}},
            "data": f"fb:{report.run_id.hex}:{position}:down",
        },
    }


def test_feedback_and_reason_are_bound_to_a_delivered_article_without_topic_ban(database):
    client = InteractiveClient()
    with initialize(database) as repo:
        report = repo.latest_report()
        service = TelegramService(client, TelegramStore(repo), BOT)
        asyncio.run(service.send_brief(report))
        assert client.markups[-1] == feedback_buttons(report.run_id, 1)
        assert len(client.markups[-1]["inline_keyboard"][0][0]["callback_data"].encode()) <= 64
        event = callback(report)
        asyncio.run(service.process_update(event))
        id = repo.connection.execute("SELECT feedback_id FROM article_feedback").fetchone()[
            "feedback_id"
        ]
        asyncio.run(service.process_update(event))
        assert (
            repo.connection.execute("SELECT count(*) AS n FROM article_feedback").fetchone()["n"]
            == 1
        )
        asyncio.run(service.process_update(update(21, f"/reason F{id} Too advanced for me")))
        assert "Too advanced for me" in repo.get_profile().style
        assert "weak contextual signals" in repo.get_profile().style
        assert not service.preferences.memories()
        assert client.callbacks == ["query-20"]


@pytest.mark.parametrize("sender,position", [(999, 1), (CHAT, 2)])
def test_feedback_rejects_other_users_and_article_position_mismatch(database, sender, position):
    client = InteractiveClient()
    with initialize(database) as repo:
        report = repo.latest_report()
        service = TelegramService(client, TelegramStore(repo), BOT)
        asyncio.run(service.send_brief(report))
        asyncio.run(service.process_update(callback(report, sender=sender, position=position)))
        assert not repo.connection.execute("SELECT * FROM article_feedback").fetchone()


def test_existing_messages_get_buttons_without_another_brief_send(database):
    client = InteractiveClient()
    with initialize(database) as repo:
        service = TelegramService(client, TelegramStore(repo), BOT)
        report = repo.latest_report()
        from daily_tech.telegram import brief_messages

        asyncio.run(
            service.deliver(f"brief:{report.run_id}", brief_messages(report), run_id=report.run_id)
        )
        count = len(client.sent)
        assert asyncio.run(service.enable_feedback_buttons()) == 1
        assert len(client.sent) == count
        assert client.edits[0][0] == 2
        assert asyncio.run(service.enable_feedback_buttons()) == 0


def test_pending_rejection_and_ambiguous_feedback_never_change_profile(database):
    with initialize(database) as repo:
        store = PreferenceStore(repo)
        conversation = PreferenceConversation(store)
        original = repo.get_profile()
        source = store.record_message("ambiguous", "too advanced")
        reply = conversation.apply(source, "too advanced", proposal("", intent="feedback"))
        assert "specific article" in reply
        assert repo.get_profile() == original
        pending = store.propose(
            store.record_message("pending", "More examples"), proposal("More examples")
        )
        store.reject(pending["preference_id"])
        assert repo.get_profile() == original
