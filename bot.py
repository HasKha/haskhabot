"""haskhabot: mirrors timestamped posts from one channel into a sorted list in another."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable

import discord
from dotenv import load_dotenv

log = logging.getLogger("haskhabot")

# Discord timestamp markup, e.g. <t:1700000000> or <t:1700000000:F>.
TIMESTAMP_RE = re.compile(r"<t:(-?\d+)(?::[a-zA-Z])?>")

EMBED_LIMIT = 4096  # max characters in an embed description
DEBOUNCE_SECONDS = 2.0
LIST_TITLE = "Events"


@dataclass(frozen=True)
class Config:
    token: str
    source_channel_id: int
    list_channel_id: int
    preview_lines: int
    history_limit: int | None


def load_config() -> Config:
    load_dotenv()
    missing = [k for k in ("DISCORD_TOKEN", "SOURCE_CHANNEL_ID", "LIST_CHANNEL_ID") if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing required settings in .env: {', '.join(missing)}")
    history = os.getenv("HISTORY_LIMIT", "").strip()
    config = Config(
        token=os.environ["DISCORD_TOKEN"],
        source_channel_id=int(os.environ["SOURCE_CHANNEL_ID"]),
        list_channel_id=int(os.environ["LIST_CHANNEL_ID"]),
        preview_lines=int(os.getenv("PREVIEW_LINES", "3")),
        history_limit=int(history) if history else None,
    )
    if config.source_channel_id == config.list_channel_id:
        sys.exit("SOURCE_CHANNEL_ID and LIST_CHANNEL_ID must be different channels")
    return config


@dataclass(frozen=True)
class Entry:
    timestamp: int
    author_id: int
    preview: str
    url: str


def message_text(message: discord.Message) -> str:
    """Content plus embed text, so posts made by other event bots are picked up too."""
    parts = [message.content]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or ""]
        for field in embed.fields:
            parts += [field.name or "", field.value or ""]
    return "\n".join(p for p in parts if p)


def make_preview(text: str, max_lines: int, width: int = 120) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("#>").strip()  # heading/quote markers would break the layout
        if not line:
            continue
        if len(line) > width:
            line = line[: width - 1] + "…"
        lines.append(line)
        if len(lines) == max_lines:
            break
    return "\n".join(lines)


def extract_entry(message: discord.Message, preview_lines: int) -> Entry | None:
    text = message_text(message)
    stamps = [int(s) for s in TIMESTAMP_RE.findall(text)]
    if not stamps:
        return None
    return Entry(
        timestamp=min(stamps),  # e.g. "<start> - <end>" lists under the start time
        author_id=message.author.id,
        preview=make_preview(text, preview_lines),
        url=message.jump_url,
    )


def render_entry(entry: Entry) -> str:
    ts = entry.timestamp
    lines = [f"**<t:{ts}:f>** (<t:{ts}:R>) · <@{entry.author_id}> · [jump]({entry.url})"]
    lines += [f"> {line}" for line in entry.preview.splitlines()]
    return "\n".join(lines)


def render_pages(entries: Iterable[Entry]) -> list[str]:
    """Render the sorted list, split into chunks that each fit in one embed."""
    pages: list[str] = []
    current = ""
    for entry in sorted(entries, key=lambda e: (e.timestamp, e.url)):
        block = render_entry(entry)
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > EMBED_LIMIT and current:
            pages.append(current)
            current = block
        else:
            current = candidate
    pages.append(current or "No timestamped posts found yet.")
    return pages


class HaskhaBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: must also be enabled in the Developer Portal
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.entries: dict[int, Entry] = {}  # source message id -> entry
        self.list_messages: list[discord.Message] = []  # our messages in the list channel, in order
        self.source: discord.abc.Messageable | None = None
        self.target: discord.abc.Messageable | None = None

    async def setup_hook(self) -> None:
        self.lock = asyncio.Lock()
        self.dirty = asyncio.Event()
        self.updater = asyncio.create_task(self.run_updater())

    async def on_ready(self) -> None:
        # Fires again after a full reconnect, so rescanning here also catches anything missed offline.
        log.info("Logged in as %s", self.user)
        self.source = self.get_channel(self.config.source_channel_id)
        self.target = self.get_channel(self.config.list_channel_id)
        if not isinstance(self.source, discord.abc.Messageable) or not isinstance(self.target, discord.abc.Messageable):
            log.error("Can't see the source and/or list channel: check the IDs and the bot's permissions")
            await self.close()
            return

        async with self.lock:
            self.list_messages = [
                m async for m in self.target.history(limit=100, oldest_first=True) if m.author == self.user
            ]
            self.entries.clear()
            async for message in self.source.history(limit=self.config.history_limit):
                self.ingest(message)
        log.info("Found %d timestamped posts", len(self.entries))
        self.dirty.set()

    def ingest(self, message: discord.Message) -> bool:
        """Record or drop a source message. Returns True if the list changed."""
        entry = extract_entry(message, self.config.preview_lines)
        if entry is None:
            return self.entries.pop(message.id, None) is not None
        changed = self.entries.get(message.id) != entry
        self.entries[message.id] = entry
        return changed

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id == self.config.source_channel_id and self.ingest(message):
            self.dirty.set()

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.channel_id != self.config.source_channel_id or self.source is None:
            return
        try:
            message = await self.source.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        if self.ingest(message):
            self.dirty.set()

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        self.forget({payload.message_id}, payload.channel_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        self.forget(payload.message_ids, payload.channel_id)

    def forget(self, message_ids: set[int], channel_id: int) -> None:
        if channel_id == self.config.source_channel_id:
            removed = [self.entries.pop(i) for i in message_ids if i in self.entries]
        elif channel_id == self.config.list_channel_id:
            # Someone deleted one of our list messages; the next sync recreates it.
            removed = [m for m in self.list_messages if m.id in message_ids]
            self.list_messages = [m for m in self.list_messages if m.id not in message_ids]
        else:
            return
        if removed:
            self.dirty.set()

    async def run_updater(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            await self.dirty.wait()
            await asyncio.sleep(DEBOUNCE_SECONDS)  # batch bursts of changes into one update
            self.dirty.clear()
            try:
                await self.sync_list()
            except Exception:
                log.exception("Failed to update the list channel")

    async def sync_list(self) -> None:
        async with self.lock:
            pages = render_pages(self.entries.values())
            for i, page in enumerate(pages):
                embed = discord.Embed(
                    title=LIST_TITLE if i == 0 else None,
                    description=page,
                    colour=discord.Colour.blurple(),
                )
                if i < len(self.list_messages):
                    message = self.list_messages[i]
                    current = message.embeds[0] if message.embeds else None
                    if current and current.title == embed.title and current.description == page:
                        continue
                    self.list_messages[i] = await message.edit(content=None, embed=embed)
                else:
                    self.list_messages.append(await self.target.send(embed=embed))

            extras = self.list_messages[len(pages):]
            del self.list_messages[len(pages):]
            for message in extras:
                with contextlib.suppress(discord.NotFound):
                    await message.delete()


def main() -> None:
    config = load_config()
    HaskhaBot(config).run(config.token, root_logger=True)


if __name__ == "__main__":
    main()
