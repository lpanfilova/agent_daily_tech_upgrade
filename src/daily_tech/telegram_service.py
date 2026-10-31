"""Idempotent delivery and local command polling, independent of research."""

import asyncio
from dataclasses import dataclass
from uuid import UUID

from .telegram import TelegramClient, TelegramError, brief_messages, profile_messages
from .telegram_storage import TelegramStore


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: UUID
    status: str


class TelegramService:
    def __init__(self, client: TelegramClient, store: TelegramStore, bot: dict):
        self.client, self.store, self.bot = client, store, bot
        self.repo = store.repository
        self.bot_id = bot["id"]
        self.chat_id = client.settings.chat_id
        if self.chat_id is None:
            raise ValueError("A verified private chat is required for delivery")

    async def deliver(self, key, messages, *, run_id=None) -> DeliveryResult:
        with self.store.lock():
            delivery = self.store.prepare(self.bot_id, self.chat_id, key, messages, run_id)
            delivery_id = delivery["delivery_id"]
            status = self.store.recover(delivery_id)
            if status in ("sent", "uncertain"):
                return DeliveryResult(delivery_id, "already_sent" if status == "sent" else status)
            for part in self.store.parts(delivery_id):
                if part["status"] == "sent":
                    continue
                self.store.mark_part(delivery_id, part["part_index"], "sending")
                try:
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
        return await self.deliver(
            f"brief:{report.run_id}", brief_messages(report), run_id=report.run_id
        )

    def authorized(self, update):
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        chat, sender = message.get("chat", {}), message.get("from", {})
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
                    "Welcome to Daily Tech Upgrade.\n\n/brief — your latest reading brief\n/profile — your reading preferences"
                ],
            }
        return {
            "kind": "text",
            "messages": [
                "Use /brief for your latest reading brief or /profile to view your preferences."
            ],
        }

    async def process_update(self, update):
        update_id = update["update_id"]
        if update_id < self.store.next_offset(self.bot_id):
            return
        if not self.authorized(update):
            self.store.advance(self.bot_id, update_id)
            return
        saved = self.store.update(self.bot_id, update_id)
        if saved is None:
            saved = self.store.save_plan(self.bot_id, update_id, self.make_plan(update))
        if saved["done"]:
            self.store.advance(self.bot_id, update_id)
            return
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
