"""Tests for signup reactions and the /listsignups tally. Run with: pytest"""

import ast
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord
import pytest
from conftest import FakeChannel

from haskhabot import bot, signups
from haskhabot.bot import HaskhaBot
from haskhabot.events import Entry
from haskhabot.mapping import Mapping
from haskhabot.settings import Config, MappingConfig
from haskhabot.signups import find_emotes, format_signups, signup_emojis, signup_rows

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


def client_for(*pairs, add_reactions=True, reaction_channels=None):
    """A built HaskhaBot for (source, list) pairs whose source channels do or don't grant Add Reactions.

    Returns the client and its channels dict, which tests can change to simulate permission changes.
    """
    channels = {s: FakeChannel(f"src{s}", **VISIBLE, add_reactions=add_reactions) for s, _ in pairs}
    for c in reaction_channels or ():
        channels.setdefault(c, FakeChannel(f"react{c}", **VISIBLE, add_reactions=add_reactions))
    config = Config(token="x", mappings=tuple(MappingConfig(s, l) for s, l in pairs), preview_lines=3,
                    history_limit=None, keep_seconds=0, reaction_channels=reaction_channels)
    client = HaskhaBot(config)
    client.get_channel = channels.get
    client.build()
    return client, channels


def lists(client):
    """list channel id -> its Mapping"""
    return {m.list_id: m for m in client.mappings}


def send(client, message):
    asyncio.run(client.on_message(message))


def edit(client, message):
    """Deliver an edit: the client fetches `message` (the new version) from its channel."""

    async def fetch_message(message_id):
        return message

    client.get_channel(message.channel.id).fetch_message = fetch_message
    asyncio.run(client.on_raw_message_edit(NS(channel_id=message.channel.id, message_id=message.id)))


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


# --- reacting: when a post is sent, and to emotes an edit adds ---

def test_new_post_gets_its_emotes_in_order():
    client, _ = client_for((1, 2))
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM} {HEART}")
    send(client, message)
    assert message.calls == [SHIELD, CUSTOM, HEART]


def test_a_failed_reaction_is_skipped():
    client, _ = client_for((1, 2))
    message = fake_message(f"{EVENT} {SHIELD} {CUSTOM} {HEART}", fail=(CUSTOM,))
    send(client, message)
    assert message.calls == [SHIELD, HEART]


def test_only_posts_in_a_source_channel_get_reactions():
    client, _ = client_for((1, 2))
    in_list, unmapped = fake_message(f"{EVENT} {SHIELD}", channel_id=2), fake_message(f"{EVENT} {SHIELD}", channel_id=3)
    send(client, in_list)
    send(client, unmapped)
    assert in_list.calls == unmapped.calls == []


def test_forwarded_post_gets_no_reactions_but_is_listed():
    client, _ = client_for((1, 2))
    message = fake_message(f"{EVENT} {SHIELD}", reference=FORWARD)
    send(client, message)
    assert message.calls == [] and message.id in lists(client)[2].entries


def test_edit_reacts_only_with_emotes_it_added():
    client, _ = client_for((1, 2))
    send(client, sent := fake_message(f"{EVENT} {SHIELD}", mid=5))
    edit(client, edited := fake_message(f"{EVENT} {SHIELD} {HEART}", mid=5))
    assert sent.calls == [SHIELD] and edited.calls == [HEART]
    assert 5 in lists(client)[2].entries and lists(client)[2].dirty.is_set()  # the list is updated too


def test_repeated_edits_without_new_emotes_cost_nothing():
    # e.g. an LFG bot updating its signup count on every signup
    client, _ = client_for((1, 2))
    send(client, sent := fake_message(f"{EVENT} {SHIELD} {CUSTOM}", mid=5, fail=(CUSTOM,)))  # CUSTOM unusable
    edits = [fake_message(f"{EVENT} {SHIELD} {CUSTOM} ({n} signed up)", mid=5, fail=(CUSTOM,)) for n in range(5)]
    for e in edits:
        edit(client, e)
    assert sent.calls == [SHIELD]
    assert all(e.calls == [] for e in edits)  # the failed one isn't retried either


def test_renamed_custom_emote_and_variation_selector_are_the_same_emote():
    client, _ = client_for((1, 2))
    send(client, fake_message(f"{EVENT} <:tank:111> \u2694\ufe0f", mid=5))
    edit(client, edited := fake_message(f"{EVENT} <:tank_new:111> \u2694", mid=5))  # renamed, selector dropped
    assert edited.calls == []


def test_first_edit_of_a_post_from_before_startup_only_records_it():
    client, _ = client_for((1, 2))
    edit(client, first := fake_message(f"{EVENT} {SHIELD}", mid=5))  # sent before the bot started
    edit(client, second := fake_message(f"{EVENT} {SHIELD} {HEART}", mid=5))
    assert first.calls == []  # existing posts never get reactions
    assert second.calls == [HEART]  # but emotes added afterwards do


def test_editing_a_chat_message_into_an_event_reacts():
    client, _ = client_for((1, 2))
    send(client, sent := fake_message(f"raid tonight? {SHIELD}", mid=5))  # no time yet: not an event
    edit(client, edited := fake_message(f"{EVENT} {SHIELD}", mid=5))
    assert sent.calls == [] and edited.calls == [SHIELD]


def test_edited_forward_gets_no_reactions():
    client, _ = client_for((1, 2))
    send(client, sent := fake_message(f"{EVENT} {SHIELD}", mid=5, reference=FORWARD))
    edit(client, edited := fake_message(f"{EVENT} {SHIELD} {HEART}", mid=5, reference=FORWARD))
    assert sent.calls == edited.calls == []


def test_deleting_a_post_forgets_its_emotes():
    client, _ = client_for((1, 2))
    send(client, fake_message(f"{EVENT} {SHIELD}", mid=5))
    asyncio.run(client.on_raw_message_delete(NS(message_id=5, channel_id=1)))
    assert 5 not in client.reactions[1].seen_emotes and 5 not in lists(client)[2].entries


def test_emote_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(signups, "REACTION_MEMORY", 3)
    client, _ = client_for((1, 2))
    for mid in range(1, 6):
        send(client, fake_message(f"{EVENT} {SHIELD}", mid=mid))
    assert list(client.reactions[1].seen_emotes) == [3, 4, 5]


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
    m = Mapping(NS(get_channel={1: source, 2: target}.get, user=None), 2, (1,), config)
    m.check_channels = lambda: []
    asyncio.run(m.rescan())
    assert 5 in m.entries  # still listed
    assert existing.calls == []  # startup and rescans never add reactions


# --- several lists and sources ---

def test_shared_source_post_is_listed_everywhere_and_reacted_to_once():
    client, _ = client_for((1, 2), (1, 3))
    send(client, message := fake_message(f"{EVENT} {SHIELD}", mid=5))
    assert 5 in lists(client)[2].entries and 5 in lists(client)[3].entries
    assert message.calls == [SHIELD]  # once, not once per list


def test_list_collects_posts_from_all_its_sources():
    client, _ = client_for((1, 3), (4, 3))
    send(client, a := fake_message(f"{EVENT} {SHIELD}", mid=5, channel_id=1))
    send(client, b := fake_message(f"{EVENT} {HEART}", mid=6, channel_id=4))
    assert {5, 6} <= lists(client)[3].entries.keys()
    assert a.calls == [SHIELD] and b.calls == [HEART]


def test_edit_in_a_shared_source_updates_every_list():
    client, _ = client_for((1, 2), (1, 3))
    send(client, fake_message(f"chat {SHIELD}", mid=5))  # not an event yet
    edit(client, edited := fake_message(f"{EVENT} {SHIELD}", mid=5))
    assert 5 in lists(client)[2].entries and 5 in lists(client)[3].entries
    assert edited.calls == [SHIELD]


def test_missing_add_reactions_in_one_source_only_affects_that_source():
    client, channels = client_for((1, 3), (4, 3))
    channels[4] = FakeChannel("src4", **VISIBLE, add_reactions=False)
    send(client, a := fake_message(f"{EVENT} {SHIELD}", mid=5, channel_id=1))
    send(client, b := fake_message(f"{EVENT} {HEART}", mid=6, channel_id=4))
    assert a.calls == [SHIELD] and b.calls == []
    assert {5, 6} <= lists(client)[3].entries.keys()  # both still listed


# --- reaction channels are independent of watched sources ---

def test_reaction_channels_can_exclude_a_watched_source():
    client, _ = client_for((1, 2), reaction_channels=())
    send(client, message := fake_message(f"{EVENT} {SHIELD}", mid=5))
    assert message.calls == [] and 5 in lists(client)[2].entries  # listed, not reacted to
    edit(client, edited := fake_message(f"{EVENT} {SHIELD} {HEART}", mid=5))
    assert edited.calls == [] and lists(client)[2].entries[5].message_id == 5


def test_reaction_channels_can_include_an_unwatched_channel():
    client, _ = client_for((1, 2), reaction_channels=(9,))
    send(client, other := fake_message(f"{EVENT} {SHIELD}", channel_id=9))
    send(client, watched := fake_message(f"{EVENT} {SHIELD}", channel_id=1, mid=2))
    assert other.calls == [SHIELD] and watched.calls == []
    assert lists(client)[2].entries.keys() == {2}  # channel 9's post isn't listed
    edit(client, edited := fake_message(f"{EVENT} {SHIELD} {HEART}", channel_id=9))
    assert edited.calls == [HEART]


@pytest.mark.parametrize("reaction_channels, logged", [((1, 9), "#src1, #react9"), ((), "no channels")])
def test_startup_logs_where_reactions_are_added(caplog, reaction_channels, logged):
    client, _ = client_for((1, 2), reaction_channels=reaction_channels)
    client.mappings = []  # nothing to rescan
    with caplog.at_level(logging.INFO, logger="haskhabot"):
        asyncio.run(client.on_ready())
    assert f"Adding signup reactions in: {logged}" in caplog.text


# --- the optional Add Reactions permission ---

def test_without_add_reactions_the_post_is_listed_with_one_warning(caplog):
    client, _ = client_for((1, 2), add_reactions=False)
    first, second = fake_message(f"{EVENT} {SHIELD}", mid=5), fake_message(f"{EVENT} {HEART}", mid=6)
    with caplog.at_level(logging.WARNING, logger="haskhabot"):
        send(client, first)
        send(client, second)
    assert first.calls == second.calls == []  # no doomed API calls
    assert {5, 6} <= lists(client)[2].entries.keys() and lists(client)[2].dirty.is_set()  # the list still updates
    warnings = [r for r in caplog.records if "Add Reactions" in r.getMessage()]
    assert len(warnings) == 1  # warned once, not per post or per emote


def test_regaining_add_reactions_is_logged_and_reacting_resumes(caplog):
    client, channels = client_for((1, 2), add_reactions=False)
    assert client.reactions[1].check() is False
    channels[1] = FakeChannel("src1", **VISIBLE, add_reactions=True)
    with caplog.at_level(logging.INFO, logger="haskhabot"):
        assert client.reactions[1].check() is True
    assert any("back on" in r.getMessage() for r in caplog.records)
    send(client, message := fake_message(f"{EVENT} {SHIELD}"))
    assert message.calls == [SHIELD]


def test_check_warns_when_add_reactions_is_removed(caplog):
    client, channels = client_for((1, 2))
    assert client.reactions[1].check() is True
    channels[1] = FakeChannel("src1", **VISIBLE, add_reactions=False)
    with caplog.at_level(logging.WARNING, logger="haskhabot"):
        assert client.reactions[1].check() is False
    assert any("Missing Add Reactions" in r.getMessage() for r in caplog.records)


def test_a_reaction_channel_that_isnt_found_says_so(caplog):
    client, channels = client_for((1, 2), reaction_channels=(404,))
    del channels[404]  # a wrong ID: get_channel finds nothing
    with caplog.at_level(logging.WARNING, logger="haskhabot"):
        assert client.reactions[404].check() is False
        assert client.reactions[404].check() is False
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and "[404] Reaction channel not found" in messages[0]  # once, not per check
    assert "Add Reactions" not in messages[0]


def test_startup_log_leaves_out_channels_that_cant_react(caplog):
    client, channels = client_for((1, 2), (3, 4), reaction_channels=(1, 3))
    channels[3] = FakeChannel("src3", **VISIBLE, add_reactions=False)
    client.mappings = []  # nothing to rescan
    with caplog.at_level(logging.INFO, logger="haskhabot"):
        asyncio.run(client.on_ready())
    assert "Adding signup reactions in: #src1\n" in caplog.text + "\n"
    assert "[#src3] Missing Add Reactions" in caplog.text

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
    m = Mapping(None, 3, (1, 4), CONFIG)  # a list collecting sources 1 and 4
    m.entries[10] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=10)
    m.entries[11] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=11, forwarded=True)
    m.entries[12] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=12)

    assert m.event_in_thread(NS(id=10, parent_id=1)) is m.entries[10]
    assert m.event_in_thread(NS(id=12, parent_id=4)) is m.entries[12]  # either source works
    assert m.event_in_thread(NS(id=10, parent_id=3)) is None  # a thread in the list channel
    assert m.event_in_thread(NS(id=13, parent_id=1)) is None  # not an event post
    assert m.event_in_thread(NS(id=11, parent_id=1)) is None  # a forward
    assert m.event_in_thread(NS(id=1)) is None  # not a thread at all


def run_listsignups(client, thread, post):
    """Run /listsignups in `thread`, whose starter post is `post`; returns what the bot replied."""
    replies = []

    async def reply(content=None, *, embed=None, ephemeral=False):
        replies.append(embed.description if embed else content)

    async def defer():
        pass

    async def fetch_message(message_id):
        return post

    client._connection.user = NS(id=99)
    if (source := client.get_channel(thread.parent_id)) is not None:
        source.fetch_message = fetch_message
    interaction = NS(channel=thread, response=NS(send_message=reply, defer=defer), followup=NS(send=reply))
    asyncio.run(client.list_signups(interaction))
    return replies


def test_listsignups_in_a_listed_source():
    client, _ = client_for((1, 2), reaction_channels=())
    post = fake_message(EVENT, mid=5, reactions=[reaction(SHIELD, [99, 6])])
    lists(client)[2].ingest(post)
    assert run_listsignups(client, NS(id=5, parent_id=1), post) == [f"{SHIELD} **1**: <@6>"]


def test_listsignups_in_a_reaction_only_channel():
    client, _ = client_for((1, 2), reaction_channels=(9,))
    post = fake_message(EVENT, channel_id=9, mid=5, reactions=[reaction(SHIELD, [99, 6])])
    assert run_listsignups(client, NS(id=5, parent_id=9), post) == [f"{SHIELD} **1**: <@6>"]


def test_listsignups_elsewhere_is_refused():
    client, _ = client_for((1, 2), reaction_channels=())
    post = fake_message("chat", mid=5)
    assert run_listsignups(client, NS(id=5, parent_id=1), post) == [bot.NOT_AN_EVENT_THREAD]  # not an event
    assert run_listsignups(client, NS(id=5, parent_id=8), post) == [bot.NOT_AN_EVENT_THREAD]  # unknown channel


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
    sources = list(Path(__file__).resolve().parent.parent.glob("haskhabot/*.py"))
    assert sources  # the check below would pass vacuously on an empty list
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for outer in ast.walk(tree):
            if not isinstance(outer, comps):
                continue
            for inner in ast.walk(outer):
                if inner is not outer and isinstance(inner, comps) and any(g.is_async for g in inner.generators):
                    raise AssertionError(f"{source.name}:{inner.lineno}: async comprehension inside a comprehension")
