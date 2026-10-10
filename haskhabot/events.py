"""Event posts: reading a message into an Entry, and rendering the sorted list of entries."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

import discord

# Discord timestamp markup, e.g. <t:1700000000> or <t:1700000000:F>.
TIMESTAMP_RE = re.compile(r"<t:(-?\d+)(?::[a-zA-Z])?>")
# "on fill": starts as soon as enough people join. Listed at the time it was posted.
ON_FILL_RE = re.compile(r"\bon[\s-]?fill\b", re.IGNORECASE)
MENTIONS_ONLY_RE = re.compile(r"(?:<(?:@[!&]?|#)\d+>\s*)+")
BRAILLE_BLANK = "⠀"

EMBED_LIMIT = 4096  # max characters in an embed description
EMPTY_TEXT = "No events scheduled"


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
