"""Signup reactions: which emotes a post offers, reacting with them, and tallying who signed up."""

from __future__ import annotations

import collections
import logging
import re
from typing import Iterable, Sequence

import discord
import emoji

from .events import EMBED_LIMIT, extract_entry, forwarded_from, message_text

log = logging.getLogger("haskhabot")

CUSTOM_EMOTE_RE = re.compile(r"<a?:\w+:(\d+)>")
MAX_REACTIONS = 20  # Discord's limit of distinct reactions on one message
REACTION_MEMORY = 1000  # source posts whose emotes are remembered, to react only to emotes an edit adds
VARIATION_SELECTOR = "️"
TAIL_RESERVE = 20  # room kept in a signup line for " …and N more"
REACTION_PERMISSIONS = ("add_reactions",)  # optional, in the source channel: only signup reactions need it


def find_emotes(text: str) -> list[str]:
    """Custom and fully-qualified unicode emotes in text: deduplicated, in order of first appearance, at most 20."""
    found = [(m.start(), m.group()) for m in CUSTOM_EMOTE_RE.finditer(text)]
    found += [
        (e["match_start"], e["emoji"])
        for e in emoji.emoji_list(text)
        if emoji.EMOJI_DATA[e["emoji"]]["status"] == emoji.STATUS["fully_qualified"]  # not a bare ™ or ©
    ]
    return list(dict.fromkeys(e for _, e in sorted(found)))[:MAX_REACTIONS]


def emote_key(mark: str) -> str:
    """What makes two emotes the same: a custom one's ID (its name can change), unicode without variation selectors."""
    custom = CUSTOM_EMOTE_RE.fullmatch(mark)
    return custom.group(1) if custom else mark.replace(VARIATION_SELECTOR, "")


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


async def signup_rows(message: discord.Message, bot_id: int) -> list[tuple[str, list[int]]]:
    """(emote, user ids) for each reaction the bot placed on the post, without the bot itself."""
    rows = []
    for r in message.reactions:
        if r.me:  # the bot's own emotes only: a stray thumbs-up isn't a signup
            # A loop, not a nested comprehension: async-inside-comprehension is a SyntaxError before Python 3.11.
            rows.append((str(r.emoji), [u.id async for u in r.users() if u.id != bot_id]))
    return rows


class SourceReactions:
    """Signup reactions for one source channel. Shared by every list the channel feeds, so each post is
    reacted to once however many lists it appears in."""

    def __init__(self, client: discord.Client, source_id: int):
        self.client = client
        self.source_id = source_id
        self.label = str(source_id)
        self.can_react = True  # last known state of the optional Add Reactions permission
        # Emotes already handled (reacted, failed or predating the bot) per post, so an edit only reacts
        # with emotes it added. In memory only, bounded to the most recent posts.
        self.seen_emotes: collections.OrderedDict[int, set[str]] = collections.OrderedDict()

    def check(self) -> bool:
        """Whether the bot may add reactions in this channel; warns when that changes.

        Optional: without it the bot just doesn't react, and everything else keeps working.
        """
        channel = self.client.get_channel(self.source_id)
        allowed = isinstance(channel, discord.abc.GuildChannel) and all(
            getattr(channel.permissions_for(channel.guild.me), name) for name in REACTION_PERMISSIONS
        )
        if allowed != self.can_react:
            where = f"#{channel.name}" if isinstance(channel, discord.abc.GuildChannel) else self.label
            if allowed:
                log.info("[%s] Add Reactions permission is back: signup reactions are on again", where)
            else:
                log.warning("[%s] Missing Add Reactions: new posts won't get signup reactions until it's "
                            "granted. Everything else keeps working.", where)
        self.can_react = allowed
        return allowed

    def remember(self, message_id: int, keys: set[str]) -> None:
        self.seen_emotes[message_id] = keys
        self.seen_emotes.move_to_end(message_id)
        while len(self.seen_emotes) > REACTION_MEMORY:
            self.seen_emotes.popitem(last=False)  # forget the least recently sent/edited post

    def forget(self, message_ids: Iterable[int]) -> None:
        for i in message_ids:
            self.seen_emotes.pop(i, None)

    async def add_reactions(self, message: discord.Message, *, edited: bool = False) -> None:
        """React with the post's signup emotes that haven't been handled yet. A failed one is logged and skipped.

        When a post is sent, all its emotes are new. On an edit, only emotes the edit added are: each emote is
        tried once per post, so a bot that keeps editing its post, or an emote that can't be used, costs nothing.
        Existing posts never get reactions: rescans don't call this, and the first edit the bot sees of a post
        it didn't see sent (it predates startup) only records that post's emotes.
        """
        emotes = signup_emojis(message)
        known = self.seen_emotes.get(message.id)
        if edited and known is None:
            self.remember(message.id, {emote_key(e) for e in emotes})
            return
        known = known or set()
        new = [e for e in emotes if emote_key(e) not in known]
        self.remember(message.id, known | {emote_key(e) for e in new})
        if not new or not self.check():
            return
        for mark in new:
            try:
                await message.add_reaction(mark)
            except discord.HTTPException as e:
                log.warning("[%s] Couldn't react with %s on %s: %s", self.label, mark, message.jump_url, e)
