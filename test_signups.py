"""Checks for signup reactions and the /listsignups tally. Run: python test_signups.py"""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord

from bot import (
    Config, Entry, HaskhaBot, Mapping, MappingConfig, find_emotes, format_signups, signup_emojis, signup_rows,
)

SHIELD = "\U0001f6e1️"
HEART = "\U0001f49a"
SWORDS = "⚔️"
CUSTOM = "<:tank:111>"
ANIMATED = "<a:wave:222>"
EVENT = "Raid <t:1790000000:F>"


def fake_message(content, *, reference=None, reactions=(), fail=(), channel_id=1):
    """Just enough of discord.Message for extract_entry and add_reactions; `calls` records reactions added."""
    calls = []

    async def add_reaction(mark):
        if mark in fail:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "Unknown Emoji")
        calls.append(mark)

    return SimpleNamespace(
        id=1, content=content, components=[], embeds=[], message_snapshots=[], reference=reference,
        created_at=datetime(2026, 10, 4, tzinfo=timezone.utc), author=SimpleNamespace(id=7),
        jump_url="https://discord.test/m", channel=SimpleNamespace(id=channel_id),
        reactions=list(reactions), add_reaction=add_reaction, calls=calls,
    )


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
    forward = SimpleNamespace(type=discord.MessageReferenceType.forward, jump_url="https://discord.test/orig")
    assert signup_emojis(fake_message(f"{EVENT} {SHIELD}", reference=forward)) == []


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


CONFIG = Config(token="x", mappings=(), preview_lines=3, history_limit=None, keep_seconds=0)


def reaction(mark, user_ids, me=True):
    async def users():
        for user_id in user_ids:
            yield SimpleNamespace(id=user_id)

    return SimpleNamespace(emoji=mark, me=me, users=users)


def mapping():
    return Mapping(None, MappingConfig(1, 2), CONFIG)


def test_add_reactions_follows_the_posts_order():
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM} {HEART}")
    asyncio.run(mapping().add_reactions(message))
    assert message.calls == [SHIELD, CUSTOM, HEART]


def test_add_reactions_skips_ones_the_bot_already_added():
    message = fake_message(f"{EVENT} {SHIELD} {HEART}", reactions=[reaction(SHIELD, [9])])
    asyncio.run(mapping().add_reactions(message))
    assert message.calls == [HEART]


def test_add_reactions_still_adds_its_own_when_only_a_user_reacted():
    message = fake_message(f"{EVENT} {SHIELD}", reactions=[reaction(SHIELD, [9], me=False)])
    asyncio.run(mapping().add_reactions(message))
    assert message.calls == [SHIELD]


def test_add_reactions_carries_on_after_a_failure():
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM} {HEART}", fail=(CUSTOM,))
    asyncio.run(mapping().add_reactions(message))
    assert message.calls == [SHIELD, HEART]


def test_on_message_reacts_only_in_the_source_channel():
    m = mapping()
    inside = fake_message(f"{EVENT} {SHIELD}", channel_id=1)
    outside = fake_message(f"{EVENT} {SHIELD}", channel_id=3)
    asyncio.run(m.on_message(inside))
    asyncio.run(m.on_message(outside))
    assert inside.calls == [SHIELD] and outside.calls == []


def test_signup_rows_counts_what_the_bot_placed_minus_the_bot():
    message = fake_message(
        EVENT,
        reactions=[reaction(SHIELD, [99, 5, 6]), reaction(HEART, [99]), reaction("\U0001f44d", [5], me=False)],
    )
    assert asyncio.run(signup_rows(message, bot_id=99)) == [(SHIELD, [5, 6]), (HEART, [])]


def test_signup_rows_includes_post_emotes_people_added_themselves():
    # e.g. an emote from another server the bot couldn't react with, added by hand
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM}", reactions=[reaction(SHIELD, [99, 5]), reaction(CUSTOM, [7, 8], me=False)])
    assert asyncio.run(signup_rows(message, bot_id=99)) == [(SHIELD, [5]), (CUSTOM, [7, 8])]


def test_signup_rows_matches_an_external_emote_by_id_not_name():
    message = fake_message(f"{EVENT} {CUSTOM}", reactions=[reaction("<:tank_renamed:111>", [7], me=False)])
    assert asyncio.run(signup_rows(message, bot_id=99)) == [("<:tank_renamed:111>", [7])]


def test_signup_rows_matches_unicode_with_or_without_variation_selector():
    message = fake_message(f"{EVENT} {SHIELD}", reactions=[reaction("\U0001f6e1", [7], me=False)])
    assert asyncio.run(signup_rows(message, bot_id=99)) == [("\U0001f6e1", [7])]


def test_event_in_thread():
    m = mapping()
    m.entries[10] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=10)
    m.entries[11] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=11, forwarded=True)

    assert m.event_in_thread(SimpleNamespace(id=10, parent_id=1)) is m.entries[10]
    assert m.event_in_thread(SimpleNamespace(id=10, parent_id=2)) is None  # a thread in the list channel
    assert m.event_in_thread(SimpleNamespace(id=12, parent_id=1)) is None  # not an event post
    assert m.event_in_thread(SimpleNamespace(id=11, parent_id=1)) is None  # a forward
    assert m.event_in_thread(SimpleNamespace(id=1)) is None  # not a thread at all


def rescan_mapping(keep_seconds, message):
    """A Mapping whose source channel holds just `message`, ready for rescan()."""

    async def posts(**kwargs):
        yield message

    async def nothing(**kwargs):
        return
        yield

    channels = {1: SimpleNamespace(name="src", history=posts), 2: SimpleNamespace(name="list", history=nothing)}
    config = Config(token="x", mappings=(), preview_lines=3, history_limit=None, keep_seconds=keep_seconds)
    m = Mapping(SimpleNamespace(user=None, get_channel=channels.get), MappingConfig(1, 2), config)
    m.check_channels = lambda: []
    return m


def test_rescan_returns_before_the_backfill_reactions_finish():
    message = fake_message(f"{EVENT} {SHIELD}")

    async def run():
        gate = asyncio.Event()

        async def slow(mark):
            await gate.wait()
            message.calls.append(mark)

        message.add_reaction = slow
        m = rescan_mapping(1e12, message)
        await asyncio.wait_for(m.rescan(), timeout=1)  # times out if rescan waits for the reactions
        assert message.calls == []
        gate.set()
        await m.backfilling
        assert message.calls == [SHIELD]

    asyncio.run(run())


def test_rescan_does_not_backfill_expired_posts():
    message = fake_message(f"{EVENT} {SHIELD}")

    async def run():
        m = rescan_mapping(0, message)
        await m.rescan()
        await m.backfilling

    asyncio.run(run())
    assert message.calls == []


def test_a_failing_backfill_is_logged_not_raised():
    message = fake_message(f"{EVENT} {SHIELD}")

    async def boom(mark):
        raise RuntimeError("boom")

    message.add_reaction = boom

    async def run():
        m = rescan_mapping(1e12, message)
        await m.rescan()
        await m.backfilling  # would re-raise if the backfill let the error escape

    asyncio.run(run())


def test_a_failing_command_sync_does_not_stop_startup():
    async def run():
        bot = HaskhaBot(CONFIG)

        async def broken_sync(*args, **kwargs):
            raise OSError("network down")

        bot.tree.sync = broken_sync
        try:
            await bot.setup_hook()
        finally:
            for task in [*bot.updaters, bot.watcher]:
                task.cancel()

    asyncio.run(run())


def test_no_async_comprehension_inside_another_comprehension():
    """A SyntaxError before Python 3.11, which would stop the whole bot starting; the README promises 3.9+."""
    comps = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
    for outer in ast.walk(tree):
        if not isinstance(outer, comps):
            continue
        for inner in ast.walk(outer):
            if inner is not outer and isinstance(inner, comps) and any(g.is_async for g in inner.generators):
                raise AssertionError(f"bot.py:{inner.lineno}: async comprehension inside a comprehension")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
