"""Idempotent delivery and local command polling, independent of research."""

import asyncio
import re
from dataclasses import dataclass
from uuid import UUID

from .preference_conversation import PreferenceConversation
from .preferences import PreferenceStore
from .telegram import (
    TelegramClient,
    TelegramError,
    brief_parts,
    feedback_buttons,
    profile_messages,
    split_text,
)
from .telegram_storage import TelegramStore


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: UUID
    status: str


class TelegramService:
    def __init__(
        self,
        client: TelegramClient,
        store: TelegramStore,
        bot: dict,
        *,
        preference_model_factory=None,
    ):
        self.client, self.store, self.bot = client, store, bot
        self.repo = store.repository
        self.bot_id = bot["id"]
        self.chat_id = client.settings.chat_id
        self.preferences = PreferenceStore(self.repo)
        self.conversation = PreferenceConversation(self.preferences, preference_model_factory)
        if self.chat_id is None:
            raise ValueError("A verified private chat is required for delivery")

    async def deliver(
        self, key, messages, *, run_id=None, markups=None, positions=None
    ) -> DeliveryResult:
        with self.store.lock():
            delivery = self.store.prepare(
                self.bot_id, self.chat_id, key, messages, run_id, markups, positions
            )
            delivery_id = delivery["delivery_id"]
            status = self.store.recover(delivery_id)
            if status in ("sent", "uncertain"):
                return DeliveryResult(delivery_id, "already_sent" if status == "sent" else status)
            for part in self.store.parts(delivery_id):
                if part["status"] == "sent":
                    continue
                self.store.mark_part(delivery_id, part["part_index"], "sending")
                try:
                    if part["reply_markup"]:
                        message_id = await self.client.send(
                            part["body"], reply_markup=part["reply_markup"]
                        )
                    else:
                        message_id = await self.client.send(part["body"])
                except TelegramError as exc:
                    status = "uncertain" if exc.uncertain else "failed"
                    self.store.mark_part(delivery_id, part["part_index"], status, error=str(exc))
                    return DeliveryResult(delivery_id, status)
                except BaseException:
                    self.store.mark_part(
                        delivery_id,
                        part["part_index"],
                        "uncertain",
                        error="Sender stopped during a request",
                    )
                    raise
                # If this DB write fails after Telegram accepted the message, the durable
                # 'sending' state causes a future sender to hold the part as uncertain.
                self.store.mark_part(delivery_id, part["part_index"], "sent", message_id=message_id)
            self.store.mark_sent(delivery_id)
            return DeliveryResult(delivery_id, "sent")

    async def send_brief(self, report):
        parts = brief_parts(report)
        return await self.deliver(
            f"brief:{report.run_id}",
            [text for text, _ in parts],
            run_id=report.run_id,
            markups=[feedback_buttons(report.run_id, pos) if pos else None for _, pos in parts],
            positions=[pos for _, pos in parts],
        )

    async def enable_feedback_buttons(self):
        changed = 0
        with self.store.lock():
            for delivery in self.store.recent_brief_deliveries(self.bot_id, self.chat_id):
                parts = self.store.parts(delivery["delivery_id"])
                layout = brief_parts(self.repo.get_report(delivery["run_id"]))
                if len(parts) != len(layout) or any(
                    p["body"] != item[0] for p, item in zip(parts, layout, strict=True)
                ):
                    raise ValueError(
                        "Saved message layout differs; existing messages were not re-sent"
                    )
                for part, (_, position) in zip(parts, layout, strict=True):
                    if not position or part["status"] != "sent" or part["buttons_installed_at"]:
                        continue
                    markup = feedback_buttons(delivery["run_id"], position)
                    self.store.set_buttons(
                        delivery["delivery_id"], part["part_index"], position, markup
                    )
                    await self.client.edit_markup(part["message_id"], markup)
                    self.store.set_buttons(
                        delivery["delivery_id"],
                        part["part_index"],
                        position,
                        markup,
                        confirmed=True,
                    )
                    changed += 1
        return changed

    def authorized(self, update):
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        if not isinstance(message, dict):
            return False
        chat = message.get("chat", {})
        sender = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {})
        return (
            chat.get("type") == "private"
            and chat.get("id") == self.chat_id
            and sender.get("id") == self.chat_id
            and not sender.get("is_bot")
        )

    def make_plan(self, update):
        text = update["message"].get("text", "").strip()
        word = text.split()[0] if text else ""
        command, _, target = word.partition("@")
        if target and target.casefold() != self.bot.get("username", "").casefold():
            return {"kind": "ignored"}
        if command == "/brief":
            report = self.repo.latest_report()
            if report is None:
                return {"kind": "text", "messages": ["Your first reading brief isn’t ready yet."]}
            previous = self.store.delivery(self.bot_id, self.chat_id, f"brief:{report.run_id}")
            if previous and previous["status"] == "sent":
                return {
                    "kind": "text",
                    "messages": [
                        "Your latest brief is already in this chat. There isn’t a newer one yet."
                    ],
                }
            return {"kind": "brief", "run_id": str(report.run_id)}
        if command == "/profile":
            return {"kind": "text", "messages": profile_messages(self.repo.get_profile())}
        if command == "/start":
            return {
                "kind": "text",
                "messages": [
                    "Welcome to Daily Tech Upgrade.\n\n/brief — your latest reading brief\n/profile — your reading profile\n/preferences — inspect saved preferences\n\nTell me what technical reading you prefer; I’ll propose a change for you to confirm."
                ],
            }
        return {
            "kind": "text",
            "messages": [
                "Use /brief for your latest reading brief or /profile to view your preferences."
            ],
        }

    async def prepare_saved_plan(self, update):
        callback = update.get("callback_query")
        text = callback.get("data", "") if callback else update["message"].get("text", "")
        source = self.preferences.record_message(
            f"telegram:{self.bot_id}:{update['update_id']}",
            text,
            context={"kind": "callback" if callback else "message"},
        )
        proposal = (
            await self.conversation.interpret(source, text.strip())
            if text and not callback
            else None
        )
        with self.store.connection.transaction():
            try:
                if callback:
                    match = re.fullmatch(r"fb:([0-9a-f]{32}):([1-3]):(up|down)", text)
                    target = self.store.feedback_target(
                        self.bot_id, self.chat_id, callback["message"].get("message_id")
                    )
                    if (
                        not match
                        or not target
                        or target["run_id"] != UUID(match[1])
                        or target["article_position"] != int(match[2])
                    ):
                        raise ValueError(
                            "This button could not be associated with a delivered article"
                        )
                    id = self.preferences.add_feedback(
                        source, target["run_id"], int(match[2]), match[3]
                    )
                    plan = {
                        "kind": "text",
                        "messages": [
                            f"{'Like' if match[3] == 'up' else 'Dislike'} saved as F{id} for article {match[2]}.\nAdd a reason with /reason F{id} your reason.\nThis does not create a permanent topic preference."
                        ],
                    }
                else:
                    command = text.strip().split(" ", 1)[0].split("@")[0]
                    word = text.strip().split(" ", 1)[0]
                    if (
                        "@" in word
                        and word.split("@", 1)[1].casefold()
                        != self.bot.get("username", "").casefold()
                    ):
                        plan = {"kind": "ignored"}
                    elif command in ("/brief", "/profile", "/start"):
                        plan = self.make_plan(update)
                    else:
                        response = self.conversation.apply(source, text.strip(), proposal)
                        plan = {"kind": "text", "messages": split_text(response)}
            except ValueError as exc:
                plan = {"kind": "text", "messages": split_text(str(exc))}
            return self.store.save_plan(self.bot_id, update["update_id"], plan)

    async def process_update(self, update):
        update_id = update["update_id"]
        if update_id < self.store.next_offset(self.bot_id):
            return
        if not self.authorized(update):
            self.store.advance(self.bot_id, update_id)
            return
        saved = self.store.update(self.bot_id, update_id)
        if saved is None:
            saved = await self.prepare_saved_plan(update)
        if saved["done"]:
            self.store.advance(self.bot_id, update_id)
            return
        callback = update.get("callback_query")
        if callback and callback.get("id"):
            try:
                await self.client.answer_callback(callback["id"])
            except TelegramError:
                print(
                    "Callback acknowledgement expired or failed; saved feedback is preserved",
                    flush=True,
                )
        plan = saved["plan"]
        if plan["kind"] == "brief":
            result = await self.send_brief(self.repo.get_report(UUID(plan["run_id"])))
        elif plan["kind"] == "text":
            result = await self.deliver(f"update:{update_id}:response", plan["messages"])
        else:
            self.store.complete_update(self.bot_id, update_id)
            return
        print(
            f"Telegram update {update_id}: {result.status}; delivery {result.delivery_id}",
            flush=True,
        )
        if result.status in ("failed", "uncertain"):
            notice = (
                "I couldn’t confirm the whole delivery. Some messages may already be in this chat; "
                "I haven’t automatically resent them."
                if result.status == "uncertain"
                else "I couldn’t finish sending that response. Please try the command again later."
            )
            await self.deliver(f"update:{update_id}:status", [notice])
        self.store.complete_update(self.bot_id, update_id)

    async def listen(self, *, once=False):
        with self.store.lock(polling=True):
            webhook = await self.client.call("getWebhookInfo")
            if not isinstance(webhook, dict):
                raise TelegramError("Telegram returned invalid webhook status", fatal=True)
            if webhook.get("url"):
                raise TelegramError(
                    "A webhook is configured; local polling stopped and the webhook was left unchanged",
                    fatal=True,
                )
            print(f"Listening as @{self.bot.get('username', '')}. Stop with Ctrl+C.", flush=True)
            delay = 1
            while True:
                try:
                    updates = await self.client.updates(
                        self.store.next_offset(self.bot_id), timeout=0 if once else 20
                    )
                except TelegramError as exc:
                    if once or exc.fatal:
                        raise
                    print(f"{exc}; retrying update retrieval in {delay}s", flush=True)
                    await asyncio.sleep(delay)
                    delay = min(15, delay * 2)
                    continue
                delay = 1
                for update in updates:
                    await self.process_update(update)
                if once:
                    return
