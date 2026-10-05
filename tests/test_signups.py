"""Tests for signup reactions and the /listsignups tally. Run with: pytest"""

import ast
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord

from bot import (
    Config, Entry, HaskhaBot, Mapping, MappingConfig, find_emotes, format_signups, signup_emojis, signup_rows,
)
from conftest import FakeChannel

SHIELD = "\U0001f6e1️"
HEART = "\U0001f49a"
CUSTOM = "<:tank:111>"
ANIMATED = "<a:wave:222>"
EVENT = "Raid <t:1790000000:F>"
FORWARD = NS(type=discord.MessageReferenceType.forward, jump_url="https://discord.test/orig")
CONFIG = Config(token="x", mappings=(), preview_lines=3, history_limit=None, keep_seconds=0)
VISIBLE = dict(view_channel=True, read_message_history=True)


def fake_message(content, *, reference=None, reactions=(), fail=(), channel_id=1, mid=1):
    """Just enough of discord.Message for extract_entry and add_reactions; `calls` records reactions added."""
    calls = []

    async def add_reaction(mark):
        if mark in fail:
            raise discord.HTTPException(NS(status=400, reason="Bad Request"), "Unknown Emoji")
        calls.append(mark)

    return NS(
        id=mid, content=content, components=[], embeds=[], message_snapshots=[], reference=reference,
        created_at=datetime(2026, 10, 4, tzinfo=timezone.utc), author=NS(id=7),
        jump_url="https://discord.test/m", channel=NS(id=channel_id),
        reactions=list(reactions), add_reaction=add_reaction, calls=calls,
    )


def mapping(add_reactions=True, config=CONFIG):
    """A Mapping for source channel 1 / list channel 2, with or without Add Reactions in the source."""
    channels = {1: FakeChannel("src", **VISIBLE, add_reactions=add_reactions)}
    return Mapping(NS(get_channel=channels.get, user=None), MappingConfig(1, 2), config)


def reaction(mark, user_ids, me=True):
    async def users():
        for user_id in user_ids:
            yield NS(id=user_id)

    return NS(emoji=mark, me=me, users=users)


# --- finding emotes ---

def test_find_emotes_mixes_custom_and_unicode_in_order():
    assert find_emotes(f"{SHIELD} tank {CUSTOM} heal {HEART} {ANIMATED}") == [SHIELD, CUSTOM, HEART, ANIMATED]


def test_find_emotes_dedupes():
    assert find_emotes(f"{SHIELD} {CUSTOM} {SHIELD} {CUSTOM}") == [SHIELD, CUSTOM]


def test_find_emotes_caps_at_twenty():
    got = find_emotes(" ".join(f"<:e{i}:{i}>" for i in range(1, 30)))
    assert len(got) == 20 and got[0] == "<:e1:1>" and got[-1] == "<:e20:20>"


def test_find_emotes_compound_unicode():
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    flag = "\U0001f1ef\U0001f1f5"
    thumbs = "\U0001f44d\U0001f3fd"
    keycap = "1️⃣"
    assert find_emotes(f"{family} {flag} {thumbs} {keycap}") == [family, flag, thumbs, keycap]


def test_find_emotes_ignores_plain_text_symbols_and_markup():
    assert find_emotes("tank 1 vs 2 # * \xa9 ™ <@123> <@&5> <#9> <t:1790000000:F>") == []


def test_signup_emojis_for_an_event():
    assert signup_emojis(fake_message(f"{EVENT} {SHIELD} {HEART}")) == [SHIELD, HEART]


def test_signup_emojis_on_fill_counts_as_an_event():
    assert signup_emojis(fake_message(f"on fill {SHIELD}")) == [SHIELD]


def test_signup_emojis_none_without_a_timestamp():
    assert signup_emojis(fake_message(f"{SHIELD} just chatting")) == []


def test_signup_emojis_none_for_a_forward():
    assert signup_emojis(fake_message(f"{EVENT} {SHIELD}", reference=FORWARD)) == []


# --- reacting: only to newly sent posts ---

def test_new_post_gets_its_emotes_in_order():
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM} {HEART}")
    asyncio.run(mapping().on_message(message))
    assert message.calls == [SHIELD, CUSTOM, HEART]


def test_a_failed_reaction_is_skipped():
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM} {HEART}", fail=(CUSTOM,))
    asyncio.run(mapping().on_message(message))
    assert message.calls == [SHIELD, HEART]


def test_only_posts_in_the_source_channel_get_reactions():
    m = mapping()
    inside, outside = fake_message(f"{EVENT} {SHIELD}", channel_id=1), fake_message(f"{EVENT} {SHIELD}", channel_id=3)
    asyncio.run(m.on_message(inside))
    asyncio.run(m.on_message(outside))
    assert inside.calls == [SHIELD] and outside.calls == []


def test_forwarded_post_gets_no_reactions_but_is_listed():
    m = mapping()
    message = fake_message(f"{EVENT} {SHIELD}", reference=FORWARD)
    asyncio.run(m.on_message(message))
    assert message.calls == [] and message.id in m.entries


def test_edit_updates_the_list_but_never_reacts():
    m = mapping()
    edited = fake_message(f"{EVENT} {SHIELD} {HEART}", mid=5)  # emotes written in by the edit

    async def fetch_message(message_id):
        return edited

    m.source = NS(fetch_message=fetch_message)
    asyncio.run(m.on_edit(NS(channel_id=1, message_id=5)))
    assert 5 in m.entries and m.dirty.is_set()
    assert edited.calls == []


def test_rescan_lists_existing_posts_but_never_reacts():
    existing = fake_message(f"{EVENT} {SHIELD}", mid=5)

    async def posts(**kwargs):
        yield existing

    async def nothing(**kwargs):
        return
        yield

    source = FakeChannel("src", **VISIBLE, add_reactions=True)
    source.history = posts
    target = NS(name="list", history=nothing)
    config = Config(token="x", mappings=(), preview_lines=3, history_limit=None, keep_seconds=1e12)
    m = Mapping(NS(get_channel={1: source, 2: target}.get, user=None), MappingConfig(1, 2), config)
    m.check_channels = lambda: []
    asyncio.run(m.rescan())
    assert 5 in m.entries  # still listed
    assert existing.calls == []  # startup and rescans never add reactions


# --- the optional Add Reactions permission ---

def test_without_add_reactions_the_post_is_listed_with_one_warning(caplog):
    m = mapping(add_reactions=False)
    first, second = fake_message(f"{EVENT} {SHIELD}", mid=5), fake_message(f"{EVENT} {HEART}", mid=6)
    with caplog.at_level(logging.WARNING, logger="haskhabot"):
        asyncio.run(m.on_message(first))
        asyncio.run(m.on_message(second))
    assert first.calls == second.calls == []  # no doomed API calls
    assert {5, 6} <= m.entries.keys() and m.dirty.is_set()  # the list still updates
    warnings = [r for r in caplog.records if "Add Reactions" in r.getMessage()]
    assert len(warnings) == 1  # warned once, not per post or per emote


def test_regaining_add_reactions_is_logged_and_reacting_resumes(caplog):
    channels = {1: FakeChannel("src", **VISIBLE, add_reactions=False)}
    m = Mapping(NS(get_channel=channels.get, user=None), MappingConfig(1, 2), CONFIG)
    assert m.check_reactions() is False
    channels[1] = FakeChannel("src", **VISIBLE, add_reactions=True)
    with caplog.at_level(logging.INFO, logger="haskhabot"):
        assert m.check_reactions() is True
    assert any("is back" in r.getMessage() for r in caplog.records)
    message = fake_message(f"{EVENT} {SHIELD}")
    asyncio.run(m.on_message(message))
    assert message.calls == [SHIELD]


def test_periodic_check_warns_when_add_reactions_is_removed(caplog):
    channels = {1: FakeChannel("src", **VISIBLE, add_reactions=True)}
    m = Mapping(NS(get_channel=channels.get, user=None), MappingConfig(1, 2), CONFIG)
    m.target = object()  # working
    m.check_channels = lambda: []
    channels[1] = FakeChannel("src", **VISIBLE, add_reactions=False)
    with caplog.at_level(logging.WARNING, logger="haskhabot"):
        asyncio.run(m.recheck_access())
    assert any("Add Reactions" in r.getMessage() for r in caplog.records)
    assert m.target is not None  # not treated as lost access


# --- /listsignups ---

def test_format_signups_lists_count_and_mentions():
    assert format_signups([(SHIELD, [1, 2, 3])]) == f"{SHIELD} **3**: <@1> <@2> <@3>"


def test_format_signups_shows_an_empty_emote_with_zero():
    assert format_signups([(SHIELD, [1]), (HEART, [])]) == f"{SHIELD} **1**: <@1>\n{HEART} **0**"


def test_format_signups_truncates_without_hiding_other_emotes():
    text = format_signups([(SHIELD, list(range(1000))), (HEART, [5])], limit=400)
    first, second = text.split("\n")
    assert len(text) <= 400
    assert first.startswith(f"{SHIELD} **1000**:") and "more" in first.split(">")[-1]
    assert second == f"{HEART} **1**: <@5>"


def test_signup_rows_counts_what_the_bot_placed_minus_the_bot():
    message = fake_message(
        EVENT,
        reactions=[reaction(SHIELD, [99, 5, 6]), reaction(HEART, [99]), reaction("\U0001f44d", [5], me=False)],
    )
    assert asyncio.run(signup_rows(message, bot_id=99)) == [(SHIELD, [5, 6]), (HEART, [])]


def test_event_in_thread():
    m = mapping()
    m.entries[10] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=10)
    m.entries[11] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=11, forwarded=True)

    assert m.event_in_thread(NS(id=10, parent_id=1)) is m.entries[10]
    assert m.event_in_thread(NS(id=10, parent_id=2)) is None  # a thread in the list channel
    assert m.event_in_thread(NS(id=12, parent_id=1)) is None  # not an event post
    assert m.event_in_thread(NS(id=11, parent_id=1)) is None  # a forward
    assert m.event_in_thread(NS(id=1)) is None  # not a thread at all


# --- startup ---

def test_a_failing_command_sync_does_not_stop_startup():
    async def run():
        client = HaskhaBot(CONFIG)

        async def broken_sync(*args, **kwargs):
            raise OSError("network down")

        client.tree.sync = broken_sync
        try:
            await client.setup_hook()
        finally:
            for task in [*client.updaters, client.watcher]:
                task.cancel()

    asyncio.run(run())


def test_no_async_comprehension_inside_another_comprehension():
    """A SyntaxError before Python 3.11, which would stop the whole bot starting; the README promises 3.9+."""
    comps = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    tree = ast.parse((Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8"))
    for outer in ast.walk(tree):
        if not isinstance(outer, comps):
            continue
        for inner in ast.walk(outer):
            if inner is not outer and isinstance(inner, comps) and any(g.is_async for g in inner.generators):
                raise AssertionError(f"bot.py:{inner.lineno}: async comprehension inside a comprehension")
