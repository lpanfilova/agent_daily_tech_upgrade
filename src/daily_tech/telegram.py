"""Telegram transport and rendering; no research or model calls."""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .models import Profile, RunReport


class TelegramError(RuntimeError):
    def __init__(self, message, *, uncertain=False, fatal=False):
        super().__init__(message)
        self.uncertain = uncertain
        self.fatal = fatal


@dataclass(frozen=True, repr=False)
class TelegramSettings:
    token: str
    chat_id: int | None

    @classmethod
    def from_env(cls, *, require_chat=True):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Set a valid TELEGRAM_BOT_TOKEN in .env")
        raw_chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if raw_chat and (not raw_chat.isascii() or not raw_chat.isdecimal() or int(raw_chat) <= 0):
            raise ValueError("TELEGRAM_CHAT_ID must identify your verified private chat")
        if require_chat and not raw_chat:
            raise ValueError("Set TELEGRAM_CHAT_ID to your verified private chat in .env")
        return cls(token, int(raw_chat) if raw_chat else None)


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient, settings: TelegramSettings):
        self.client, self.settings = client, settings
        self.last_send_at = None
        # Telegram puts credentials in URL paths. Suppress HTTP request debug/info logs.
        for logger in ("httpx", "httpcore"):
            logging.getLogger(logger).setLevel(logging.WARNING)

    async def call(self, method, payload=None):
        sending = method == "sendMessage"
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.settings.token}/{method}",
                json=payload or {},
                timeout=35,
            )
        except httpx.HTTPError:
            raise TelegramError("Telegram network request failed", uncertain=sending) from None
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(
                "Telegram returned an unreadable response", uncertain=sending
            ) from None
        if not isinstance(data, dict) or data.get("ok") is not True or not response.is_success:
            if (
                method == "editMessageReplyMarkup"
                and isinstance(data, dict)
                and data.get("error_code") == 400
                and "message is not modified" in data.get("description", "").lower()
            ):
                return True  # Idempotent retry after a lost edit acknowledgement.
            code = data.get("error_code") if isinstance(data, dict) else None
            code = code if isinstance(code, int) else response.status_code
            explicitly_rejected = (
                isinstance(data, dict)
                and data.get("ok") is False
                and 400 <= code < 500
                and response.status_code < 500
            )
            raise TelegramError(
                f"Telegram rejected {method} (code {code})",
                uncertain=sending and not explicitly_rejected,
                fatal=code in (401, 404, 409),
            )
        return data.get("result")

    async def get_me(self):
        bot = await self.call("getMe")
        if not isinstance(bot, dict) or not bot.get("is_bot") or not isinstance(bot.get("id"), int):
            raise TelegramError("Telegram returned an invalid bot identity", fatal=True)
        return bot

    async def send(self, text, reply_markup=None):
        if self.settings.chat_id is None:
            raise ValueError("No verified Telegram chat is configured")
        loop = asyncio.get_running_loop()
        if self.last_send_at is not None:
            await asyncio.sleep(max(0, 1.05 - (loop.time() - self.last_send_at)))
        self.last_send_at = loop.time()
        payload = {
            "chat_id": self.settings.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        message = await self.call("sendMessage", payload)
        if not isinstance(message, dict) or not isinstance(message.get("message_id"), int):
            raise TelegramError("Telegram did not confirm a message ID", uncertain=True)
        return message["message_id"]

    async def edit_markup(self, message_id, markup):
        return await self.call(
            "editMessageReplyMarkup",
            {"chat_id": self.settings.chat_id, "message_id": message_id, "reply_markup": markup},
        )

    async def answer_callback(self, query_id):
        await self.call("answerCallbackQuery", {"callback_query_id": query_id, "text": "Received"})

    async def updates(self, offset, *, timeout=20):
        updates = await self.call(
            "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100}
        )
        if not isinstance(updates, list) or any(
            not isinstance(u, dict) or not isinstance(u.get("update_id"), int) for u in updates
        ):
            raise TelegramError("Telegram returned invalid updates")
        return sorted(updates, key=lambda u: u["update_id"])


def split_text(text: str, limit=3500) -> list[str]:
    """Plain text avoids markup injection; budget conservatively in UTF-16 units."""
    if limit < 2:
        raise ValueError("Message limit must allow at least two UTF-16 units")
    result = []
    text = text.strip()
    while text:
        units, cut = 0, 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            cut += 1
        if cut < len(text):
            boundary = max(text.rfind("\n", 0, cut + 1), text.rfind(" ", 0, cut + 1))
            if boundary > 0:
                cut = boundary
        result.append(text[:cut])
        text = text[cut:].lstrip()
    return result


def brief_parts(report: RunReport) -> list[tuple[str, int | None]]:
    if report.status not in ("complete", "partial") or not report.recommendations:
        raise ValueError("This run has no completed recommendations to deliver")
    minutes = sum(r.article.reading_minutes for r in report.recommendations)
    header = (
        f"Daily Tech Upgrade · {report.started_at.date()}\n"
        f"{len(report.recommendations)} articles · ~{minutes} minutes of reading"
    )
    if report.status == "partial":
        header += "\nA shorter selection met your reading criteria today."
    messages = [(text, None) for text in split_text(header)]
    for index, rec in enumerate(report.recommendations, 1):
        a, s = rec.article, rec.summary
        if urlsplit(a.url).scheme != "https" or len(a.url) > 2000:
            raise ValueError("An article URL cannot be safely formatted for Telegram")
        text = (
            f"{index}. {a.title}\n{a.publisher} · {a.published_at.date()} · ~{a.reading_minutes} min\n"
            f"{a.url}\n\n{s.summary}\n\nTechnical insights\n"
            + "\n".join(f"• {i.claim}" for i in s.insights)
            + f"\n\nWhy it matters\n{s.why_it_matters}"
        )
        chunks = split_text(text)
        messages.extend(
            (chunk, index if i == len(chunks) - 1 else None) for i, chunk in enumerate(chunks)
        )
    return messages


def brief_messages(report: RunReport) -> list[str]:
    return [text for text, _ in brief_parts(report)]


def feedback_buttons(run_id, position):
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Like", "callback_data": f"fb:{run_id.hex}:{position}:up"},
                {"text": "👎 Dislike", "callback_data": f"fb:{run_id.hex}:{position}:down"},
            ]
        ]
    }


def profile_messages(profile: Profile | None) -> list[str]:
    if profile is None:
        return ["Your reading profile hasn’t been set up yet."]
    text = (
        f"Your reading profile\n\nLevel: {profile.depth}\n\nInterests\n"
        + "\n".join(f"• {interest}" for interest in profile.interests)
        + f"\n\nStyle\n{profile.style}"
    )
    if profile.goals:
        text += "\n\nGoals\n" + "\n".join(f"• {goal}" for goal in profile.goals)
    return split_text(text)
