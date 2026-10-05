"""Tests for the pure parsing/rendering logic. Run with: pytest"""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import discord
from discord.components import _component_factory

import bot
from conftest import FakeChannel

POSTED = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
HOUR = 3600
FORWARD = discord.MessageReference(
    message_id=999, channel_id=888, guild_id=777, type=discord.MessageReferenceType.forward
)


def msg(content="", mid=1, embeds=(), snapshots=(), reference=None, components=(), created_at=POSTED):
    """A stand-in for discord.Message with just the attributes the bot reads."""
    return NS(
        content=content,
        id=mid,
        author=NS(id=42),
        jump_url=f"https://discord.com/channels/1/2/{mid}",
        created_at=created_at,
        embeds=list(embeds),
        message_snapshots=list(snapshots),
        reference=reference,
        components=list(components),
    )


def snapshot(content="", embeds=(), components=()):
    return NS(content=content, embeds=list(embeds), components=list(components))


def config(**overrides):
    values = dict(token="x", mappings=(bot.MappingConfig(1, 2),), preview_lines=3,
                  history_limit=None, keep_seconds=4 * HOUR)
    return bot.Config(**{**values, **overrides})


def mapping(client=None, source=1, list_=2):
    return bot.Mapping(client, list_, (source,), config())


class FakeEmoji:
    def __init__(self, name, id):
        self.name, self.id = name, id

    def is_usable(self):
        return True

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


# --- timestamps and previews ---

def test_message_without_timestamp_is_ignored():
    assert bot.extract_entry(msg("no time here"), 3) is None


def test_earliest_timestamp_wins_and_preview_is_cleaned():
    entry = bot.extract_entry(msg("# Raid night\nStarts <t:1760000000:F> - ends <t:1760007200:t>\n\nBring snacks\nline 4"), 3)
    assert entry.timestamp == 1760000000
    assert entry.preview == "Raid night\nStarts <t:1760000000:F> - ends <t:1760007200:t>\nBring snacks"


def test_timestamps_in_embeds_are_found():
    entry = bot.extract_entry(msg(embeds=[discord.Embed(title="Movie", description="At <t:1750000000:R>")]), 3)
    assert entry.timestamp == 1750000000


def test_preview_skips_mention_only_lines_and_braille_padding():
    preview = bot.make_preview("<@&123>" + bot.BRAILLE_BLANK * 20 + "\n**Title**\n<@456> <#789>\nbody", 3)
    assert preview == "**Title**\nbody"


def test_preview_truncates_long_lines():
    assert bot.make_preview("x" * 200, 1) == "x" * 119 + "…"


# --- rendering ---

def test_pages_fit_in_embeds():
    entries = [bot.extract_entry(msg(f"Event {i} <t:{1760000000 + i}>\n" + "x" * 110 + "\ny\nz", i), 3) for i in range(100)]
    pages = bot.render_pages(entries)
    assert len(pages) > 1
    assert all(len(page) <= bot.EMBED_LIMIT for page in pages)


def test_pages_are_sorted_by_time():
    late = bot.extract_entry(msg("late <t:2000000002>", 1), 3)
    early = bot.extract_entry(msg("early <t:2000000001>", 2), 3)
    page = bot.render_pages([late, early])[0]
    assert page.index("early") < page.index("late")


def test_empty_list_text():
    assert bot.render_pages([]) == ["No events scheduled"]
    assert bot.render_pages([], (), "custom") == ["custom"]


def test_empty_text_uses_server_emoji_when_present():
    m = mapping()
    m.target = NS(guild=NS(emojis=[FakeEmoji("frog", 1), FakeEmoji(bot.EMPTY_EMOJI, 9)]))
    assert m.empty_text() == f"No events scheduled <:{bot.EMPTY_EMOJI}:9>"
    m.target = NS(guild=NS(emojis=[FakeEmoji("frog", 1)]))
    assert m.empty_text() == "No events scheduled"


# --- emojis ---

def test_emoji_pick_is_stable_and_prefixes_entry():
    emojis = ["<:frog:1>", "<:toad:2>", "<a:slime:3>"]
    entry = bot.extract_entry(msg("a <t:2000000000>", 1111), 3)
    emoji = bot.pick_emoji(entry, emojis)
    assert emoji == bot.pick_emoji(entry, emojis)
    assert bot.render_pages([entry], emojis)[0].startswith(f"{emoji} **<t:2000000000:f>**")


def test_no_emojis_means_no_prefix():
    entry = bot.extract_entry(msg("a <t:2000000000>"), 3)
    assert bot.pick_emoji(entry, []) == ""
    assert bot.render_pages([entry])[0].startswith("**<t:")


# --- listing window ---

def test_events_stay_listed_until_keep_window_passes():
    now = time.time()
    old = bot.extract_entry(msg("old <t:%d>" % (now - 5 * HOUR), 1), 3)
    recent = bot.extract_entry(msg("recent <t:%d>" % (now - HOUR), 2), 3)
    later = bot.extract_entry(msg("later <t:%d>" % (now + 2 * HOUR), 3), 3)
    assert bot.upcoming([old, recent, later], now, 4 * HOUR) == [recent, later]


def test_wakes_up_when_next_event_expires():
    now = time.time()
    m = mapping()
    m.entries = {1: bot.extract_entry(msg("x <t:%d>" % (now - 4 * HOUR + 60), 1), 3)}
    assert 55 < m.seconds_until_next_expiry() <= 62
    m.entries = {}
    assert m.seconds_until_next_expiry() == bot.MAX_WAIT_SECONDS


# --- "on fill" ---

def test_on_fill_is_listed_at_posting_time():
    entry = bot.extract_entry(msg("Raid **On Fill**\nbring pots"), 3)
    posted = int(POSTED.timestamp())
    assert entry.timestamp == posted and entry.on_fill
    assert f"**On fill** (posted <t:{posted}:R>)" in bot.render_entry(entry)


def test_on_fill_spellings():
    for text in ("on-fill", "ONFILL", "starts on fill!"):
        assert bot.extract_entry(msg(text), 3).on_fill, text
    for text in ("dragon filling", "salmon fillet", "upon filler"):
        assert bot.extract_entry(msg(text), 3) is None, text


def test_on_fill_versus_timestamps():
    posted = int(POSTED.timestamp())
    assert bot.extract_entry(msg("on fill, latest <t:%d>" % (posted + HOUR)), 3).on_fill
    early = bot.extract_entry(msg("<t:%d> or on fill" % (posted - 60)), 3)
    assert early.timestamp == posted - 60 and not early.on_fill


# --- forwards ---

def test_forward_reads_snapshot_and_links_to_original():
    entry = bot.extract_entry(msg(mid=60, snapshots=[snapshot("Event <t:2000000000:F>")], reference=FORWARD), 3)
    assert entry.forwarded and entry.timestamp == 2000000000
    assert entry.url == "https://discord.com/channels/777/888/999"
    assert "fwd by <@42>" in bot.render_entry(entry)


def test_forward_with_embed():
    entry = bot.extract_entry(msg(snapshots=[snapshot(embeds=[discord.Embed(description="on fill")])], reference=FORWARD), 3)
    assert entry.on_fill and entry.forwarded


def test_reply_is_not_a_forward():
    reply = discord.MessageReference(message_id=1, channel_id=2, guild_id=3)
    entry = bot.extract_entry(msg("reply <t:2000000000>", 62, reference=reply), 3)
    assert not entry.forwarded and entry.url.endswith("/62")


# --- layout components (Components V2), shaped like a real LFG-bot post ---

LAYOUT = [{"type": 17, "id": 1, "accent_color": 7501311, "components": [
    {"type": 9, "id": 2, "components": [{"type": 10, "id": 3, "content":
        "<@&1543590281096724600>" + bot.BRAILLE_BLANK * 30
        + "\n**NoE CM Prog**\nCommander: <@201706773365784577>\nStarting time: On Fill\nDuration: 1hr"}],
     "accessory": {"type": 11, "id": 4, "media": {"url": "https://example.com/x.png"}}},
    {"type": 14, "id": 5, "spacing": 1, "divider": True},
    {"type": 10, "id": 6, "content": "New to CM gamers welcome."},
    {"type": 1, "id": 10, "components": [{"type": 2, "id": 11, "custom_id": "x", "style": 1, "label": "Sign up"}]},
]}]


def layout_components():
    return [c for c in (_component_factory(d, None) for d in LAYOUT) if c is not None]


def test_forwarded_layout_post():
    entry = bot.extract_entry(msg(snapshots=[snapshot(components=layout_components())], reference=FORWARD), 3)
    assert entry is not None and entry.on_fill and entry.forwarded
    assert entry.preview.splitlines()[0] == "**NoE CM Prog**"
    assert bot.BRAILLE_BLANK not in entry.preview


def test_direct_layout_post():
    entry = bot.extract_entry(msg(components=layout_components()), 3)
    assert entry is not None and entry.on_fill and not entry.forwarded


# --- client wiring ---

def test_client_constructs_without_running_loop():
    bot.HaskhaBot(config())


# --- access monitoring ---

def test_check_channels_reports_missing_permissions():
    full = dict(view_channel=True, read_message_history=True, send_messages=True, embed_links=True)
    channels = {1: FakeChannel("scheduling", **full), 2: FakeChannel("upcoming", **full)}
    m = mapping(NS(get_channel=channels.get))
    assert m.check_channels() == []
    channels[1] = FakeChannel("scheduling")  # e.g. re-synced with a category that lacks the bot
    assert m.check_channels() == ["missing permissions in #scheduling (source): view_channel, read_message_history"]
    del channels[2]
    assert "list channel 2 not found" in m.check_channels()[1]


def test_recheck_rescans_on_lost_and_regained_access():
    m = mapping()
    m.target = object()  # currently working
    problems = [["no access"], ["no access"], []]  # lost, still lost, regained
    rescans = []

    async def rescan():
        rescans.append(problems[0])
        m.target = None if problems.pop(0) else object()

    m.check_channels = lambda: problems[0]
    m.rescan = rescan
    for _ in range(3):
        asyncio.run(m.recheck_access())
    assert rescans == [["no access"], ["no access"], []]
    assert m.target is not None


def test_recheck_leaves_a_working_mapping_alone():
    m = mapping()
    m.target = object()
    m.check_channels = lambda: []

    async def rescan():
        raise AssertionError("should not rescan")

    m.rescan = rescan
    asyncio.run(m.recheck_access())


def test_watcher_isolates_a_failing_mapping(monkeypatch):
    monkeypatch.setattr(bot, "RETRY_SECONDS", 0)
    client = bot.HaskhaBot(config())
    broken, healthy = mapping(source=1, list_=2), mapping(source=3, list_=4)
    rounds = []

    async def explode():
        raise RuntimeError("boom")

    async def record():
        rounds.append("healthy rescanned")

    broken.target = healthy.target = None  # both waiting for access
    broken.rescan, healthy.rescan = explode, record
    client.mappings = [broken, healthy]

    async def wait_until_ready():
        pass

    client.wait_until_ready = wait_until_ready
    client.is_closed = lambda: len(rounds) >= 2
    asyncio.run(client.watch_access())
    assert rounds == ["healthy rescanned", "healthy rescanned"]  # kept going despite the broken one
