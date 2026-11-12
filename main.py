import asyncio
import logging
import os
import re
import sqlite3
from datetime import timezone
from pathlib import Path
from typing import Optional

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession

LOG = logging.getLogger("tg-discord-bridge")


def env_list(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class State:
    def __init__(self, filename: str):
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS delivered (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        self.db.commit()

    def already_delivered(self, chat_id: int, message_id: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM delivered WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return row is not None

    def mark_delivered(self, chat_id: int, message_id: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO delivered(chat_id, message_id) VALUES (?, ?)",
            (chat_id, message_id),
        )
        self.db.commit()


class MessageFilter:
    """Rule-based gold-trading classifier with short-lived per-chat context."""

    PROMO_RE = re.compile(
        r"\b(?:vip|vvip|premium\s+(?:group|signals?|membership)|"
        r"paid\s+(?:group|signals?|membership)|subscription|subscribe|"
        r"join\s+(?:our\s+)?(?:vip|premium)|membership|mentorship|"
        r"course|webinar|affiliate|promo\s+code|discount|limited\s+offer)\b",
        re.IGNORECASE,
    )
    ENTRY_RE = re.compile(
        r"\b(?:xau\s*/?\s*usd|xauusd|gold)\b.{0,100}\b(?:buy|sell|long|short)\b"
        r"|\b(?:buy|sell|long|short)\b.{0,50}\b(?:xau\s*/?\s*usd|xauusd|gold)\b"
        r"|\bnew\s+(?:gold\s+)?trade\s+idea\b",
        re.IGNORECASE | re.DOTALL,
    )
    GOLD_ACTION_RE = re.compile(
        r"\b(?:open|take|enter|close|add|hold)\b.{0,45}\b(?:gold|xauusd|xau\s*/?\s*usd)\b"
        r"|\b(?:gold|xauusd|xau\s*/?\s*usd)\b.{0,45}\b(?:buy|buys|sell|sells|position|trade)s?\b",
        re.IGNORECASE | re.DOTALL,
    )
    UPDATE_RE = re.compile(
        r"\b(?:tp\s*[123]?\b|take[\s-]?profit|target\s*[123]?\b|"
        r"\bstop[\s-]?loss\b|\bsl\b|breakeven|\bbreak\s*even\b|"
        r"\bmove\s+(?:your|all|our)\b.{0,35}\b(?:sl|stop|entry)\b|"
        r"\b(?:pips?\s+(?:profit|secured)|profit\s+secured|"
        r"(?:win|loss|lost|hit)\b|recover(?:y)?|double\s+lot\s*size|"
        r"lot\s*size)\b)",
        re.IGNORECASE | re.DOTALL,
    )
    TRADE_CONTEXT_RE = re.compile(
        r"\b(?:gold|xauusd|xau\s*/?\s*usd|forex\s+signal|trade\s+idea|"
        r"open\s+(?:gold\s+)?(?:buys?|sells?)|live\s+free\s+gold\s+trade)\b",
        re.IGNORECASE,
    )
    TRADING_COMMENT_RE = re.compile(
        r"\b(?:be\s+ready|stay\s+tuned|inside|room|counter\s+gold|"
        r"expecting\s+it|look\s+at\s+the\s+(?:drop|move)|"
        r"i\s+warned\s+you|time\s+to\s+recover|part\s+of\s+the\s+game|"
        r"one\s+more\s+(?:gold\s+)?trade|jackpot|market\s+move)\b",
        re.IGNORECASE,
    )

    def __init__(self):
        self.keywords = [x.casefold() for x in env_list("FILTER_KEYWORDS")]
        self.users = [x.lstrip("@").casefold() for x in env_list("FILTER_USERS")]
        self.regexes = []
        for pattern in env_list("FILTER_REGEX"):
            try:
                self.regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                raise RuntimeError(f"Invalid FILTER_REGEX {pattern!r}: {exc}") from exc
        self.excluded = [x.casefold() for x in env_list("EXCLUDE_KEYWORDS")]
        self.mode = os.getenv("FILTER_MODE", "any").strip().lower()
        if self.mode not in {"any", "all"}:
            raise RuntimeError("FILTER_MODE must be 'any' or 'all'")
        self.context_seconds = max(60, int(os.getenv("TRADE_CONTEXT_MINUTES", "360")) * 60)
        self.active_trade_until: dict[int, float] = {}

    def matches(self, message, chat_id: int = 0) -> bool:
        import time

        text = (message.raw_text or "").strip()
        if not text:
            return False
        folded = text.casefold()

        # Explicit user exclusions and promotional language always take priority.
        if any(term in folded for term in self.excluded) or self.PROMO_RE.search(text):
            return False

        now = time.time()
        active = self.active_trade_until.get(chat_id, 0) > now
        explicit_gold = bool(self.TRADE_CONTEXT_RE.search(text))
        entry = bool(self.ENTRY_RE.search(text) or self.GOLD_ACTION_RE.search(text))
        update = bool(self.UPDATE_RE.search(text))
        commentary = bool(self.TRADING_COMMENT_RE.search(text))

        # A fresh gold setup or explicit gold trade action opens the context window.
        if entry or (explicit_gold and (update or commentary)):
            self.active_trade_until[chat_id] = now + self.context_seconds
            active = True

        # Updates without the word gold are accepted only while a gold trade is active.
        contextual_update = active and update
        contextual_comment = active and commentary
        trading_match = entry or contextual_update or contextual_comment

        # Optional legacy filters can broaden matching, but never bypass promo exclusions.
        checks = []
        if self.keywords:
            checks.append(any(term in folded for term in self.keywords))
        if self.users:
            sender = getattr(message, "sender", None)
            username = (getattr(sender, "username", None) or "").casefold()
            checks.append(username in self.users)
        if self.regexes:
            checks.append(any(pattern.search(text) for pattern in self.regexes))

        if checks:
            configured_match = all(checks) if self.mode == "all" else any(checks)
            return trading_match or configured_match
        return trading_match

def telegram_message_url(message, chat) -> Optional[str]:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"
    # Telegram's public link format for private/supergroup IDs is not safely
    # derivable for every chat type; omit it rather than invent a URL.
    return None


def build_discord_payload(message, chat) -> dict:
    sender = getattr(message, "sender", None)
    sender_name = (
        getattr(sender, "username", None)
        or getattr(sender, "first_name", None)
        or "Unknown sender"
    )
    chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Telegram group"
    text = (message.raw_text or "").strip() or "[Message has no text]"
    if len(text) > 3900:
        text = text[:3880] + "\n… [truncated]"
    link = telegram_message_url(message, chat)
    source = f"**Source:** {chat_name}\n**Sender:** {sender_name}"
    if link:
        source += f"\n[Open original Telegram message]({link})"
    return {
        "embeds": [{
            "description": text,
            "footer": {"text": source[:2000]},
        }],
        "allowed_mentions": {"parse": []},
    }


async def post_to_discord(session: aiohttp.ClientSession, webhook: str, payload: dict) -> None:
    async with session.post(webhook, json=payload) as response:
        if response.status == 429:
            data = await response.json(content_type=None)
            retry_after = float(data.get("retry_after", 2))
            LOG.warning("Discord rate limited delivery; retrying in %.2fs", retry_after)
            await asyncio.sleep(min(retry_after, 30))
            async with session.post(webhook, json=payload) as retry:
                if retry.status >= 300:
                    body = await retry.text()
                    raise RuntimeError(f"Discord retry failed ({retry.status}): {body[:500]}")
                return
        if response.status >= 300:
            body = await response.text()
            raise RuntimeError(f"Discord delivery failed ({response.status}): {body[:500]}")


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_id = int(required("TELEGRAM_API_ID"))
    api_hash = required("TELEGRAM_API_HASH")
    session_string = required("TELEGRAM_SESSION")
    webhook = required("DISCORD_WEBHOOK_URL")
    source_chats = env_list("TELEGRAM_SOURCE_CHATS")
    if not source_chats:
        raise RuntimeError("Set TELEGRAM_SOURCE_CHATS to one or more group usernames or IDs")

    filters = MessageFilter()
    state = State(os.getenv("DATABASE_PATH", "/app/data/bridge_state.sqlite3"))
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        connection_retries=None,
        request_retries=5,
        auto_reconnect=True,
        flood_sleep_threshold=60,
    )

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        await client.start()
        me = await client.get_me()
        LOG.info("Telegram connected as %s", getattr(me, "username", None) or me.id)

        resolved = []
        for source in source_chats:
            try:
                entity = await client.get_entity(int(source) if source.lstrip("-").isdigit() else source)
                resolved.append(entity)
                LOG.info("Monitoring accessible group: %s", getattr(entity, "title", None) or source)
            except Exception:
                LOG.exception("Could not access configured Telegram source %r", source)
                raise

        @client.on(events.NewMessage(chats=resolved))
        async def on_new_message(event):
            message = event.message
            chat_id = int(event.chat_id or 0)
            if state.already_delivered(chat_id, message.id):
                return
            try:
                chat = await event.get_chat()
                if not filters.matches(message, chat_id):
                    LOG.debug("Filtered out Telegram message %s/%s", chat_id, message.id)
                    return
                payload = build_discord_payload(message, chat)
                await post_to_discord(http, webhook, payload)
                state.mark_delivered(chat_id, message.id)
                LOG.info("Forwarded Telegram message %s/%s", chat_id, message.id)
            except Exception:
                # Leave it unmarked so a later Telegram update/restart can retry.
                LOG.exception("Could not forward Telegram message %s/%s", chat_id, message.id)

        if env_bool("FORWARD_EDITED_MESSAGES"):
            @client.on(events.MessageEdited(chats=resolved))
            async def on_edited_message(event):
                # Edits are intentionally not re-sent by default because Discord
                # webhook messages are not mapped to Telegram IDs yet.
                LOG.info("Edited message observed (%s); edit forwarding is not implemented", event.id)

        LOG.info("Bridge is running. Waiting for matching messages.")
        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
