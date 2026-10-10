"""Mapping: one list channel, the source channels it collects, and keeping the list up to date."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Sequence

import discord

from .events import EMPTY_TEXT, Entry, extract_entry, render_pages, upcoming
from .settings import Config

log = logging.getLogger("haskhabot")

DEBOUNCE_SECONDS = 2.0
EMPTY_EMOJI = "toad_pleasure_pain"  # server emoji appended to EMPTY_TEXT, if it exists
RETRY_SECONDS = 60
MAX_WAIT_SECONDS = 3600  # re-render at least hourly, as a safety net
SOURCE_PERMISSIONS = ("view_channel", "read_message_history")
LIST_PERMISSIONS = ("view_channel", "read_message_history", "send_messages", "embed_links")


class Mapping:
    """One list channel and the source channels it collects: entries, the list messages we keep, and sync."""

    def __init__(self, client: discord.Client, list_id: int, source_ids: Sequence[int], config: Config):
        self.client = client
        self.list_id = list_id
        self.source_ids = tuple(source_ids)
        self.config = config
        self.label = f"{'+'.join(map(str, self.source_ids))}→{list_id}"
        self.entries: dict[int, Entry] = {}  # source message id -> entry, across all sources
        self.list_messages: list[discord.Message] = []  # our messages in the list channel, in order
        self.target: discord.abc.Messageable | None = None  # set once every channel is reachable
        self.lock = asyncio.Lock()
        self.dirty = asyncio.Event()
        self.last_problems: list[str] = []

    def check_channels(self) -> list[str]:
        """Return what's stopping this list from working, or an empty list if nothing is."""
        problems = []
        checks = [("source", i, SOURCE_PERMISSIONS) for i in self.source_ids]
        checks.append(("list", self.list_id, LIST_PERMISSIONS))
        for label, channel_id, needed in checks:
            channel = self.client.get_channel(channel_id)
            if not isinstance(channel, discord.abc.GuildChannel) or not isinstance(channel, discord.abc.Messageable):
                problems.append(f"{label} channel {channel_id} not found (wrong ID, or the bot isn't in that server)")
                continue
            granted = channel.permissions_for(channel.guild.me)
            missing = [name for name in needed if not getattr(granted, name)]
            if missing:
                problems.append(f"missing permissions in #{channel.name} ({label}): {', '.join(missing)}")
        return problems

    async def rescan_logged(self) -> None:
        """rescan(), logging failures so they can't affect the other lists."""
        try:
            await self.rescan()
        except Exception:
            log.exception("[%s] Rescan failed", self.label)

    async def recheck_access(self) -> None:
        """Called periodically: rescan when waiting for access, or when access was just lost."""
        try:
            if self.target is None:
                await self.rescan()  # waiting for access: full rescan once it's back
            elif self.check_channels():
                log.error("[%s] Lost access to the channels", self.label)
                await self.rescan()  # logs what's missing and waits for a fix
        except Exception:
            log.exception("[%s] Rescan failed", self.label)

    async def rescan(self) -> None:
        problems = self.check_channels()
        if problems:
            if problems != self.last_problems:
                for problem in problems:
                    log.error("[%s] %s", self.label, problem)
                log.error("[%s] Retrying every %d seconds", self.label, RETRY_SECONDS)
            self.last_problems = problems
            self.target = None
            return
        self.last_problems = []
        sources = [self.client.get_channel(i) for i in self.source_ids]
        target = self.client.get_channel(self.list_id)

        async with self.lock:
            self.list_messages = [
                m async for m in target.history(limit=100, oldest_first=True) if m.author == self.client.user
            ]
            self.entries.clear()
            for source in sources:
                async for message in source.history(limit=self.config.history_limit):
                    self.ingest(message)  # existing posts are listed but never get reactions
            self.target = target
        log.info("Watching %s, list in #%s: found %d timestamped posts",
                 ", ".join(f"#{s.name}" for s in sources), target.name, len(self.entries))
        self.dirty.set()

    def ingest(self, message: discord.Message) -> bool:
        """Record or drop a source message. Returns True if the list changed."""
        entry = extract_entry(message, self.config.preview_lines)
        if entry is None:
            return self.entries.pop(message.id, None) is not None
        changed = self.entries.get(message.id) != entry
        self.entries[message.id] = entry
        return changed

    def on_message(self, message: discord.Message) -> None:
        """A message was sent, or (on edit) its new version: update the list if it's from one of our sources."""
        if message.channel.id in self.source_ids and self.ingest(message):
            self.dirty.set()

    def forget(self, message_ids: set[int], channel_id: int) -> None:
        if channel_id in self.source_ids:
            removed = [self.entries.pop(i) for i in message_ids if i in self.entries]
        elif channel_id == self.list_id:
            # Someone deleted one of our list messages; the next sync recreates it.
            removed = [m for m in self.list_messages if m.id in message_ids]
            self.list_messages = [m for m in self.list_messages if m.id not in message_ids]
        else:
            return
        if removed:
            self.dirty.set()

    def event_in_thread(self, thread) -> Entry | None:
        """The event post this thread was started from, if it's one of ours (and not a forward)."""
        if getattr(thread, "parent_id", None) not in self.source_ids:
            return None
        entry = self.entries.get(thread.id)  # a thread started from a message shares its id
        return None if entry is None or entry.forwarded else entry

    def server_emojis(self) -> list[str]:
        """The list channel's server's custom emojis, as message markup, in a stable order."""
        emojis = sorted(self.target.guild.emojis, key=lambda e: e.id)
        return [str(e) for e in emojis if e.is_usable()]

    def empty_text(self) -> str:
        emoji = discord.utils.get(self.target.guild.emojis, name=EMPTY_EMOJI)
        return f"{EMPTY_TEXT} {emoji}" if emoji and emoji.is_usable() else EMPTY_TEXT

    async def run_updater(self) -> None:
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=self.seconds_until_next_expiry())
                await asyncio.sleep(DEBOUNCE_SECONDS)  # batch bursts of changes into one update
            except asyncio.TimeoutError:
                pass  # the oldest listed event has expired: re-render to drop it
            self.dirty.clear()
            try:
                await self.sync_list()
            except Exception:
                log.exception("[%s] Failed to update the list channel", self.label)

    def seconds_until_next_expiry(self) -> float:
        now = time.time()
        keep = self.config.keep_seconds
        expiries = [e.timestamp + keep for e in upcoming(self.entries.values(), now, keep)]
        if not expiries:
            return MAX_WAIT_SECONDS
        return min(min(expiries) - now + 1, MAX_WAIT_SECONDS)

    async def sync_list(self) -> None:
        if self.target is None:
            return
        async with self.lock:
            listed = upcoming(self.entries.values(), time.time(), self.config.keep_seconds)
            pages = render_pages(listed, self.server_emojis(), self.empty_text())
            for i, page in enumerate(pages):
                embed = discord.Embed(description=page, colour=discord.Colour.blurple())
                if i < len(self.list_messages):
                    message = self.list_messages[i]
                    current = message.embeds[0] if message.embeds else None
                    if current and current.title is None and current.description == page:
                        continue
                    self.list_messages[i] = await message.edit(content=None, embed=embed)
                else:
                    self.list_messages.append(await self.target.send(embed=embed))

            extras = self.list_messages[len(pages):]
            del self.list_messages[len(pages):]
            for message in extras:
                with contextlib.suppress(discord.NotFound):
                    await message.delete()
