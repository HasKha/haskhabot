"""haskhabot: mirrors timestamped posts from one channel into a sorted list in another."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import discord
import emoji
from dotenv import load_dotenv

log = logging.getLogger("haskhabot")

# Discord timestamp markup, e.g. <t:1700000000> or <t:1700000000:F>.
TIMESTAMP_RE = re.compile(r"<t:(-?\d+)(?::[a-zA-Z])?>")
# "on fill": starts as soon as enough people join. Listed at the time it was posted.
ON_FILL_RE = re.compile(r"\bon[\s-]?fill\b", re.IGNORECASE)
MENTIONS_ONLY_RE = re.compile(r"(?:<(?:@[!&]?|#)\d+>\s*)+")
BRAILLE_BLANK = "⠀"
CUSTOM_EMOTE_RE = re.compile(r"<a?:\w+:\d+>")
MAX_REACTIONS = 20  # Discord's limit of distinct reactions on one message
TAIL_RESERVE = 20  # room kept in a signup line for " …and N more"

EMBED_LIMIT = 4096  # max characters in an embed description
DEBOUNCE_SECONDS = 2.0
EMPTY_TEXT = "No events scheduled"
EMPTY_EMOJI = "toad_pleasure_pain"  # server emoji appended to EMPTY_TEXT, if it exists
RETRY_SECONDS = 60
MAX_WAIT_SECONDS = 3600  # re-render at least hourly, as a safety net
SOURCE_PERMISSIONS = ("view_channel", "read_message_history")
LIST_PERMISSIONS = ("view_channel", "read_message_history", "send_messages", "embed_links")


@dataclass(frozen=True)
class MappingConfig:
    source_id: int
    list_id: int


def env_channel_id(env: collections.abc.Mapping[str, str], name: str) -> int:
    try:
        return int(env[name])
    except ValueError:
        raise ValueError(f"{name} must be a channel ID (digits only), got {env[name]!r}") from None


def read_mappings(path: Path, env: collections.abc.Mapping[str, str]) -> tuple[MappingConfig, ...]:
    """Mappings from the JSON file, else from the legacy SOURCE_CHANNEL_ID/LIST_CHANNEL_ID pair.

    Raises ValueError with a readable message if there are none or they're invalid.
    """
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: Notepad adds a BOM
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e
        except (OSError, UnicodeDecodeError) as e:
            # e.g. a directory: Docker creates one when bind-mounting a host file that doesn't exist.
            raise ValueError(f"Can't read {path}: {e}") from e
        if not isinstance(raw, list) or not raw:
            raise ValueError(f'{path} must be a non-empty list of {{"source": ..., "list": ...}} objects')
        mappings = []
        for i, item in enumerate(raw, 1):
            # type() is int, not isinstance: rejects "123" strings and true/false.
            if not isinstance(item, dict) or not all(type(item.get(k)) is int for k in ("source", "list")):
                raise ValueError(f'{path} entry {i} needs integer "source" and "list" channel IDs (no quotes)')
            mappings.append(MappingConfig(item["source"], item["list"]))
    elif env.get("SOURCE_CHANNEL_ID") and env.get("LIST_CHANNEL_ID"):
        mappings = [MappingConfig(env_channel_id(env, "SOURCE_CHANNEL_ID"), env_channel_id(env, "LIST_CHANNEL_ID"))]
    else:
        raise ValueError(f"No mappings: create {path} or set SOURCE_CHANNEL_ID and LIST_CHANNEL_ID")

    ids = [i for m in mappings for i in (m.source_id, m.list_id)]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(
            "Every channel ID must be unique across all sources and lists; repeated: "
            + ", ".join(map(str, repeated))
        )
    return tuple(mappings)


@dataclass(frozen=True)
class Config:
    token: str
    mappings: tuple[MappingConfig, ...]
    preview_lines: int
    history_limit: int | None
    keep_seconds: float  # how long an event stays listed after it starts


def load_config() -> Config:
    load_dotenv()
    if not os.getenv("DISCORD_TOKEN"):
        sys.exit("Missing required settings in .env: DISCORD_TOKEN")
    path = Path(os.getenv("MAPPINGS_FILE") or Path(__file__).with_name("mappings.json"))
    try:
        mappings = read_mappings(path, os.environ)
    except ValueError as e:
        sys.exit(str(e))
    history = os.getenv("HISTORY_LIMIT", "").strip()
    return Config(
        token=os.environ["DISCORD_TOKEN"],
        mappings=mappings,
        preview_lines=int(os.getenv("PREVIEW_LINES", "3")),
        history_limit=int(history) if history else None,
        keep_seconds=float(os.getenv("KEEP_AFTER_START_HOURS", "4")) * 3600,
    )


@dataclass(frozen=True)
class Entry:
    timestamp: int
    author_id: int
    preview: str
    url: str  # the original post, for forwards
    message_id: int
    on_fill: bool = False  # listed by "on fill" rather than a timestamp
    forwarded: bool = False


def component_text(components: Iterable) -> list[str]:
    """Text from layout components (Components V2), which some bots use instead of content/embeds."""
    parts = []
    for component in components:
        if isinstance(component, discord.TextDisplay):
            parts.append(component.content)
        parts += component_text(getattr(component, "children", ()))  # containers, sections, rows
    return parts


def message_text(message: discord.Message) -> str:
    """Content plus embed and component text, so posts made by other event bots are picked up too.

    Forwarded messages have no content of their own; their text is in the snapshot of the original.
    """
    parts = [message.content, *component_text(message.components)]
    embeds = list(message.embeds)
    for snapshot in message.message_snapshots:
        parts += [snapshot.content, *component_text(snapshot.components)]
        embeds += snapshot.embeds
    for embed in embeds:
        parts += [embed.title or "", embed.description or ""]
        for field in embed.fields:
            parts += [field.name or "", field.value or ""]
    return "\n".join(p for p in parts if p)


def make_preview(text: str, max_lines: int, width: int = 120) -> str:
    lines = []
    for raw in text.splitlines():
        # Blank Braille characters are a common padding trick in bot posts.
        line = raw.replace(BRAILLE_BLANK, "").strip()
        line = line.lstrip("#>").strip()  # heading/quote markers would break the layout
        if not line or MENTIONS_ONLY_RE.fullmatch(line):  # e.g. a role ping on its own line
            continue
        if len(line) > width:
            line = line[: width - 1] + "…"
        lines.append(line)
        if len(lines) == max_lines:
            break
    return "\n".join(lines)


def forwarded_from(message: discord.Message) -> discord.MessageReference | None:
    ref = message.reference
    if ref is not None and ref.type is discord.MessageReferenceType.forward:
        return ref
    return None


def extract_entry(message: discord.Message, preview_lines: int) -> Entry | None:
    text = message_text(message)
    stamps = [int(s) for s in TIMESTAMP_RE.findall(text)]
    posted = int(message.created_at.timestamp())
    if ON_FILL_RE.search(text):
        stamps.append(posted)
    if not stamps:
        return None
    timestamp = min(stamps)  # e.g. "<start> - <end>" lists under the start time
    forward = forwarded_from(message)
    return Entry(
        timestamp=timestamp,
        author_id=message.author.id,
        preview=make_preview(text, preview_lines),
        url=forward.jump_url if forward else message.jump_url,
        message_id=message.id,
        on_fill=timestamp == posted and bool(ON_FILL_RE.search(text)),
        forwarded=forward is not None,
    )


def find_emotes(text: str) -> list[str]:
    """Custom and fully-qualified unicode emotes in text: deduplicated, in order of first appearance, at most 20."""
    found = [(m.start(), m.group()) for m in CUSTOM_EMOTE_RE.finditer(text)]
    found += [
        (e["match_start"], e["emoji"])
        for e in emoji.emoji_list(text)
        if emoji.EMOJI_DATA[e["emoji"]]["status"] == emoji.STATUS["fully_qualified"]  # not a bare ™ or ©
    ]
    return list(dict.fromkeys(e for _, e in sorted(found)))[:MAX_REACTIONS]


def signup_emojis(message: discord.Message) -> list[str]:
    """Emotes to offer as signup reactions: none for forwards, which carry their original's reactions."""
    if forwarded_from(message) or extract_entry(message, 1) is None:
        return []
    return find_emotes(message_text(message))


def signup_line(mark: str, user_ids: Sequence[int], budget: int) -> str:
    """'<emote> **N**: @a @b', cut short with '…and K more' if the mentions would pass budget characters."""
    line = f"{mark} **{len(user_ids)}**" + (":" if user_ids else "")
    shown = 0
    for user_id in user_ids:
        mention = f" <@{user_id}>"
        if len(line) + len(mention) > budget - TAIL_RESERVE:
            break
        line += mention
        shown += 1
    if shown < len(user_ids):
        line += f" …and {len(user_ids) - shown} more"
    return line


def format_signups(rows: Sequence[tuple[str, Sequence[int]]], limit: int = EMBED_LIMIT) -> str:
    """One line per emote. Each gets an equal share of limit, so a huge list can't hide another emote's count."""
    budget = limit // len(rows) - 1  # -1 for the newline between lines
    return "\n".join(signup_line(mark, user_ids, budget) for mark, user_ids in rows)


def pick_emoji(entry: Entry, emojis: Sequence[str]) -> str:
    """A random emoji, seeded by the message so it stays the same across re-renders."""
    return random.Random(entry.message_id).choice(emojis) if emojis else ""


def render_entry(entry: Entry, emoji: str = "") -> str:
    ts = entry.timestamp
    prefix = f"{emoji} " if emoji else ""
    when = f"**On fill** (posted <t:{ts}:R>)" if entry.on_fill else f"**<t:{ts}:f>** (<t:{ts}:R>)"
    who = f"fwd by <@{entry.author_id}>" if entry.forwarded else f"<@{entry.author_id}>"
    lines = [f"{prefix}{when} · {who} · [jump]({entry.url})"]
    lines += [f"> {line}" for line in entry.preview.splitlines()]
    return "\n".join(lines)


def upcoming(entries: Iterable[Entry], now: float, keep_seconds: float) -> list[Entry]:
    """Entries that haven't started yet, or started less than keep_seconds ago."""
    return [e for e in entries if e.timestamp + keep_seconds > now]


def render_pages(entries: Iterable[Entry], emojis: Sequence[str] = (), empty_text: str = EMPTY_TEXT) -> list[str]:
    """Render the sorted list, split into chunks that each fit in one embed."""
    pages: list[str] = []
    current = ""
    for entry in sorted(entries, key=lambda e: (e.timestamp, e.url)):
        block = render_entry(entry, pick_emoji(entry, emojis))
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > EMBED_LIMIT and current:
            pages.append(current)
            current = block
        else:
            current = candidate
    pages.append(current or empty_text)
    return pages


class Mapping:
    """One source → list pair: its entries, the list messages we keep, and the sync logic."""

    def __init__(self, client: discord.Client, pair: MappingConfig, config: Config):
        self.client = client
        self.pair = pair
        self.config = config
        self.label = f"{pair.source_id}→{pair.list_id}"
        self.entries: dict[int, Entry] = {}  # source message id -> entry
        self.list_messages: list[discord.Message] = []  # our messages in the list channel, in order
        self.source: discord.abc.Messageable | None = None
        self.target: discord.abc.Messageable | None = None
        self.lock = asyncio.Lock()
        self.dirty = asyncio.Event()
        self.last_problems: list[str] = []

    def check_channels(self) -> list[str]:
        """Return what's stopping this mapping from working, or an empty list if nothing is."""
        problems = []
        for label, channel_id, needed in (
            ("source", self.pair.source_id, SOURCE_PERMISSIONS),
            ("list", self.pair.list_id, LIST_PERMISSIONS),
        ):
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
        """rescan(), logging failures so they can't affect the other mappings."""
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
            self.source = self.target = None
            return
        self.last_problems = []
        source = self.client.get_channel(self.pair.source_id)
        target = self.client.get_channel(self.pair.list_id)

        backfill: list[discord.Message] = []
        async with self.lock:
            self.list_messages = [
                m async for m in target.history(limit=100, oldest_first=True) if m.author == self.client.user
            ]
            self.entries.clear()
            async for message in source.history(limit=self.config.history_limit):
                self.ingest(message)
                entry = self.entries.get(message.id)
                if entry and upcoming([entry], time.time(), self.config.keep_seconds):
                    backfill.append(message)  # still listed: catches posts made while the bot was offline
            self.source, self.target = source, target
        log.info("Watching #%s, list in #%s: found %d timestamped posts", source.name, target.name, len(self.entries))
        self.dirty.set()
        for message in backfill:  # outside the lock: reacting is slow and the list needn't wait for it
            await self.add_reactions(message)

    def ingest(self, message: discord.Message) -> bool:
        """Record or drop a source message. Returns True if the list changed."""
        entry = extract_entry(message, self.config.preview_lines)
        if entry is None:
            return self.entries.pop(message.id, None) is not None
        changed = self.entries.get(message.id) != entry
        self.entries[message.id] = entry
        return changed

    async def add_reactions(self, message: discord.Message) -> None:
        """React with the post's signup emotes the bot hasn't added yet. A failed one is logged and skipped."""
        have = {str(r.emoji) for r in message.reactions if r.me}
        for mark in signup_emojis(message):
            if mark in have:
                continue
            try:
                await message.add_reaction(mark)
            except discord.HTTPException as e:
                log.warning("[%s] Couldn't react with %s on %s: %s", self.label, mark, message.jump_url, e)

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != self.pair.source_id:
            return
        if self.ingest(message):
            self.dirty.set()
        await self.add_reactions(message)

    async def on_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.channel_id != self.pair.source_id or self.source is None:
            return
        try:
            message = await self.source.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        if self.ingest(message):
            self.dirty.set()
        await self.add_reactions(message)  # picks up emotes added by the edit

    def forget(self, message_ids: set[int], channel_id: int) -> None:
        if channel_id == self.pair.source_id:
            removed = [self.entries.pop(i) for i in message_ids if i in self.entries]
        elif channel_id == self.pair.list_id:
            # Someone deleted one of our list messages; the next sync recreates it.
            removed = [m for m in self.list_messages if m.id in message_ids]
            self.list_messages = [m for m in self.list_messages if m.id not in message_ids]
        else:
            return
        if removed:
            self.dirty.set()

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


class HaskhaBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: must also be enabled in the Developer Portal
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.mappings: list[Mapping] = []
        self.by_channel: dict[int, Mapping] = {}  # source and list channel ids -> their mapping

    async def setup_hook(self) -> None:
        # Built here, not in __init__: asyncio.Lock/Event need the running loop on older Pythons.
        self.mappings = [Mapping(self, pair, self.config) for pair in self.config.mappings]
        self.by_channel = {i: m for m in self.mappings for i in (m.pair.source_id, m.pair.list_id)}
        self.updaters = [asyncio.create_task(m.run_updater()) for m in self.mappings]
        self.watcher = asyncio.create_task(self.watch_access())

    async def on_ready(self) -> None:
        # Fires again after a full reconnect, so rescanning here also catches anything missed offline.
        log.info("Logged in as %s", self.user)
        # Concurrently, so a big channel's history scan doesn't delay the other lists.
        await asyncio.gather(*(m.rescan_logged() for m in self.mappings))

    async def watch_access(self) -> None:
        # Stay connected and retry rather than exiting: a restart loop would burn through Discord's
        # login limit. Polling also picks up permission changes, which the bot gets no event for:
        # losing access (e.g. a channel re-synced with its category) stops message events silently.
        # Each mapping is handled on its own, so one with a problem doesn't hold up the others.
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(RETRY_SECONDS)
            await asyncio.gather(*(m.recheck_access() for m in self.mappings))

    async def on_message(self, message: discord.Message) -> None:
        mapping = self.by_channel.get(message.channel.id)
        if mapping:
            await mapping.on_message(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        mapping = self.by_channel.get(payload.channel_id)
        if mapping:
            await mapping.on_edit(payload)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        mapping = self.by_channel.get(payload.channel_id)
        if mapping:
            mapping.forget({payload.message_id}, payload.channel_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        mapping = self.by_channel.get(payload.channel_id)
        if mapping:
            mapping.forget(payload.message_ids, payload.channel_id)

    async def on_guild_emojis_update(self, guild: discord.Guild, before, after) -> None:
        for mapping in self.mappings:
            if mapping.target is not None and guild == mapping.target.guild:
                mapping.dirty.set()


def main() -> None:
    config = load_config()
    HaskhaBot(config).run(config.token, root_logger=True)


if __name__ == "__main__":
    main()
