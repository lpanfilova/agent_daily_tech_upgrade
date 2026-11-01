import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from test_brief import article, summary
from test_storage import database as database
from test_storage import report, repository

from daily_tech.models import Rating, Recommendation
from daily_tech.storage import StorageError
from daily_tech.telegram import (
    TelegramClient,
    TelegramError,
    TelegramSettings,
    brief_messages,
    profile_messages,
    split_text,
)
from daily_tech.telegram_service import TelegramService
from daily_tech.telegram_storage import TelegramStore

BOT = {"id": 123, "is_bot": True, "username": "reading_bot"}
CHAT = 456
SETTINGS = TelegramSettings("123:test-token", CHAT)


def saved_report(repo):
    run = report()
    run.status = "partial"
    run.completed_at = datetime.now(UTC)
    item = article()
    run.articles = [item]
    rating = Rating(article_id=item.id, quality=4, relevance=5, topic="agents", reason="tutorial")
    run.recommendations = [Recommendation(article=item, rating=rating, summary=summary())]
    repo.begin_run(run, False)
    repo.finish_run(run)
    return run


def update(id=10, text="/start", chat=CHAT, sender=CHAT, kind="private"):
    return {
        "update_id": id,
        "message": {
            "chat": {"id": chat, "type": kind},
            "from": {"id": sender, "is_bot": False},
            "text": text,
        },
    }


class FakeClient:
    settings = SETTINGS

    def __init__(self, failure=None):
        self.sent = []
        self.failure = failure

    async def send(self, text, reply_markup=None):
        self.sent.append(text)
        if self.failure:
            raise self.failure
        return len(self.sent)


def test_settings_require_explicit_private_chat_and_hide_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SETTINGS.token)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramSettings.from_env(require_chat=False).chat_id is None
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        TelegramSettings.from_env()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-123")
    with pytest.raises(ValueError, match="private chat"):
        TelegramSettings.from_env()
    assert SETTINGS.token not in repr(SETTINGS)


def test_split_preserves_content_and_respects_unicode_limits():
    text = "Intro & <literal text>\n\n" + ("A practical agent example 🚀. " * 600)
    parts = split_text(text)
    assert len(parts) > 1
    assert all(len(p.encode("utf-16-le")) // 2 <= 3500 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())
    assert all(p for p in parts)


def test_rendering_preserves_article_url_and_plain_text():
    run = report()
    run.status = "partial"
    item = article()
    item.title = "Agent <tools> & examples"
    run.recommendations = [
        Recommendation(
            article=item,
            rating=Rating(
                article_id=item.id, relevance=5, quality=4, topic="agents", reason="tutorial"
            ),
            summary=summary(),
        )
    ]
    messages = brief_messages(run)
    assert len(messages) == 2
    assert item.url in messages[1]
    assert item.title in messages[1]
    assert "shorter selection" in messages[0]
    assert "novice" in profile_messages(run.profile)[0]
    item.url = "javascript:alert(1)"
    with pytest.raises(ValueError, match="URL"):
        brief_messages(run)


def test_api_transport_targets_only_configured_chat_and_disables_previews():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["chat_id"] == CHAT
        assert "parse_mode" not in payload
        assert payload["link_preview_options"]["is_disabled"]
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await TelegramClient(http, SETTINGS).send("Test <literal>") == 9

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["network", "invalid_json", "server_error", "missing_message_id", "invalid_envelope"]
)
def test_ambiguous_send_errors_are_redacted_and_not_automatically_retried(mode):
    calls = []

    def handler(request):
        calls.append(request)
        if mode == "network":
            raise httpx.ReadTimeout("Request URL contained a token", request=request)
        if mode == "invalid_json":
            return httpx.Response(200, text="not json")
        if mode == "server_error":
            return httpx.Response(500, json={"ok": False, "error_code": 500})
        if mode == "invalid_envelope":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(TelegramError) as error:
                await TelegramClient(http, SETTINGS).send("Test")
            assert error.value.uncertain
            assert SETTINGS.token not in str(error.value)

    asyncio.run(run())
    assert len(calls) == 1


def test_explicit_rejection_is_distinguished_from_unknown_delivery():
    async def run():
        transport = httpx.MockTransport(
            lambda r: httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            )
        )
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(TelegramError) as error:
                await TelegramClient(http, SETTINGS).send("Test")
            assert not error.value.uncertain

    asyncio.run(run())


def test_delivery_is_idempotent_across_new_connections(database):
    client = FakeClient()
    with repository(database) as repo:
        repo.migrate()
        run = saved_report(repo)
        result = asyncio.run(TelegramService(client, TelegramStore(repo), BOT).send_brief(run))
        assert result.status == "sent"
        assert len(client.sent) == 2
    with repository(database) as repo:
        service = TelegramService(client, TelegramStore(repo), BOT)
        result = asyncio.run(service.send_brief(repo.get_report(run.run_id)))
        assert result.status == "already_sent"
        assert len(client.sent) == 2
        assert service.store.list_deliveries()[0]["confirmed_parts"] == 2


def test_failed_part_resumes_without_repeating_confirmed_messages(database):
    class SecondPartFails(FakeClient):
        async def send(self, text, reply_markup=None):
            self.sent.append(text)
            if len(self.sent) == 2:
                raise TelegramError("Telegram rejected sendMessage (code 429)")
            return len(self.sent)

    client = SecondPartFails()
    with repository(database) as repo:
        repo.migrate()
        run = saved_report(repo)
        result = asyncio.run(TelegramService(client, TelegramStore(repo), BOT).send_brief(run))
        assert result.status == "failed"
    with repository(database) as repo:
        result = asyncio.run(TelegramService(client, TelegramStore(repo), BOT).send_brief(run))
        assert result.status == "sent"
        assert client.sent == [
            brief_messages(run)[0],
            brief_messages(run)[1],
            brief_messages(run)[1],
        ]


def test_uncertain_send_is_held_after_restart(database):
    client = FakeClient(TelegramError("Network timeout", uncertain=True))
    with repository(database) as repo:
        repo.migrate()
        service = TelegramService(client, TelegramStore(repo), BOT)
        first = asyncio.run(service.deliver("test", ["Message"]))
        assert first.status == "uncertain"
    client.failure = None
    with repository(database) as repo:
        result = asyncio.run(
            TelegramService(client, TelegramStore(repo), BOT).deliver("test", ["Message"])
        )
        assert result.status == "uncertain"
    assert len(client.sent) == 1


def test_crashed_inflight_part_is_not_resent(database):
    client = FakeClient()
    with repository(database) as repo:
        repo.migrate()
        store = TelegramStore(repo)
        delivery = store.prepare(BOT["id"], CHAT, "crash", ["Message"])
        store.mark_part(delivery["delivery_id"], 0, "sending")
    with repository(database) as repo:
        result = asyncio.run(
            TelegramService(client, TelegramStore(repo), BOT).deliver("crash", ["Message"])
        )
        assert result.status == "uncertain"
        assert not client.sent


@pytest.mark.parametrize(
    "message",
    [
        update(chat=789),
        update(sender=789),
        update(kind="group"),
        {"update_id": 10, "callback_query": {}},
    ],
)
def test_unauthorized_updates_never_receive_private_data(database, message):
    client = FakeClient()
    with repository(database) as repo:
        repo.migrate()
        store = TelegramStore(repo)
        asyncio.run(TelegramService(client, store, BOT).process_update(message))
        assert not client.sent
        assert store.next_offset(BOT["id"]) == 11
        assert not store.update(BOT["id"], 10)


def test_commands_and_offsets_survive_restart_without_repeat_replies(database):
    client = FakeClient()
    with repository(database) as repo:
        repo.migrate()
        run = saved_report(repo)
        repo.set_profile(run.profile, "initial_import")
        service = TelegramService(client, TelegramStore(repo), BOT)
        for message in [update(10), update(11, "/profile"), update(12, "/brief")]:
            asyncio.run(service.process_update(message))
        assert "Welcome" in client.sent[0]
        assert "novice" in client.sent[1]
        assert run.recommendations[0].article.url in client.sent[3]
        sent_count = len(client.sent)
    with repository(database) as repo:
        service = TelegramService(client, TelegramStore(repo), BOT)
        for message in [update(10), update(11, "/profile"), update(12, "/brief")]:
            asyncio.run(service.process_update(message))
        assert len(client.sent) == sent_count
        asyncio.run(service.process_update(update(13, "/brief")))
        assert "already in this chat" in client.sent[-1]
        assert len(client.sent) == sent_count + 1


def test_sent_command_is_not_replied_again_after_crash_before_cursor_commit(database):
    client = FakeClient()
    message = update(10, "/brief")
    with repository(database) as repo:
        repo.migrate()
        saved_report(repo)
        store = TelegramStore(repo)
        service = TelegramService(client, store, BOT)
        plan = service.make_plan(message)
        store.save_plan(BOT["id"], 10, plan)
        asyncio.run(service.send_brief(repo.get_report(UUID(plan["run_id"]))))
        assert store.next_offset(BOT["id"]) == 0
        assert not store.update(BOT["id"], 10)["done"]
    count = len(client.sent)
    with repository(database) as repo:
        store = TelegramStore(repo)
        asyncio.run(TelegramService(client, store, BOT).process_update(message))
        assert len(client.sent) == count
        assert store.next_offset(BOT["id"]) == 11


def test_existing_webhook_is_not_removed(database):
    class WebhookClient(FakeClient):
        async def call(self, method):
            assert method == "getWebhookInfo"
            return {"url": "https://example.test/existing"}

    with repository(database) as repo:
        repo.migrate()
        service = TelegramService(WebhookClient(), TelegramStore(repo), BOT)
        with pytest.raises(TelegramError, match="left unchanged"):
            asyncio.run(service.listen(once=True))


def test_only_one_poller_can_own_cursor(database):
    with repository(database) as first, repository(database) as second:
        with TelegramStore(first).lock(polling=True):
            with pytest.raises(StorageError, match="listener"):
                with TelegramStore(second).lock(polling=True):
                    pytest.fail("A second poller must not acquire the lock")
