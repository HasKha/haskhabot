"""Tests for mappings loading and event routing. Run with: pytest"""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from bot import Config, Entry, HaskhaBot, Mapping, MappingConfig, group_by_list, read_mappings


@pytest.fixture
def load(tmp_path):
    """read_mappings against a file holding `data` (str written as-is, else JSON), or no file if None."""

    def load(data, env=None):
        path = tmp_path / "mappings.json"
        if data is not None:
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return read_mappings(path, env or {})

    return load


def config(*mappings):
    return Config(token="x", mappings=mappings, preview_lines=3, history_limit=None, keep_seconds=0)


# --- mappings.json ---

def test_valid_file(load):
    assert load([{"source": 1, "list": 2}, {"source": 3, "list": 4}]) == (MappingConfig(1, 2), MappingConfig(3, 4))


def test_sources_and_lists_can_be_shared(load):
    # one source feeding two lists, and one list collecting two sources
    data = [{"source": 1, "list": 2}, {"source": 1, "list": 3}, {"source": 4, "list": 3}]
    pairs = load(data)
    assert pairs == (MappingConfig(1, 2), MappingConfig(1, 3), MappingConfig(4, 3))
    assert group_by_list(pairs) == {2: (1,), 3: (1, 4)}


@pytest.mark.parametrize("data, message", [
    ([{"source": 1, "list": 2}, {"source": 2, "list": 3}], "used as both: 2"),
    ([{"source": 1, "list": 1}], "used as both: 1"),
    ([{"source": 1, "list": 2}, {"source": 1, "list": 2}], "more than once: 1 → 2"),
])
def test_invalid_combinations(load, data, message):
    with pytest.raises(ValueError, match=message):
        load(data)


@pytest.mark.parametrize("data, message", [
    ("[{", "not valid JSON"),
    ({"source": 1, "list": 2}, "non-empty list"),
    ([], "non-empty list"),
    ([{"source": 1, "list": 2}, {"source": 3}], "entry 2"),
    ([{"source": "1", "list": "2"}], "entry 1"),  # quoted IDs
])
def test_invalid_file(load, data, message):
    with pytest.raises(ValueError, match=message):
        load(data)


def test_utf8_bom_accepted(load):
    assert load("﻿" + json.dumps([{"source": 1, "list": 2}])) == (MappingConfig(1, 2),)


def test_unreadable_path_is_a_readable_error(tmp_path):
    # Docker creates a directory when bind-mounting a host file that doesn't exist.
    path = tmp_path / "mappings.json"
    path.mkdir()
    with pytest.raises(ValueError, match="Can't read"):
        read_mappings(path, {})


def test_file_wins_over_env(load):
    assert load([{"source": 1, "list": 2}], {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "6"}) == (MappingConfig(1, 2),)


# --- SOURCE_CHANNEL_ID / LIST_CHANNEL_ID fallback ---

def test_env_fallback(load):
    assert load(None, {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "6"}) == (MappingConfig(5, 6),)


@pytest.mark.parametrize("env", [{"SOURCE_CHANNEL_ID": "5"}, {}])
def test_env_fallback_needs_both(load, env):
    with pytest.raises(ValueError, match="No mappings"):
        load(None, env)


def test_env_fallback_still_validated(load):
    with pytest.raises(ValueError, match="both a source and a list"):
        load(None, {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "5"})


def test_env_fallback_names_a_bad_id(load):
    with pytest.raises(ValueError, match="SOURCE_CHANNEL_ID must be a channel ID.*'12x'"):
        load(None, {"SOURCE_CHANNEL_ID": "12x", "LIST_CHANNEL_ID": "6"})


# --- routing ---

def test_forget_routes_by_channel():
    m = Mapping(None, 2, (1,), config())
    m.entries[5] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=5)
    m.list_messages = [NS(id=7), NS(id=8)]

    m.forget({5}, 3)  # some other channel: ignored
    assert 5 in m.entries and not m.dirty.is_set()

    m.forget({5}, 1)  # source channel: the entry is dropped
    assert not m.entries and m.dirty.is_set()

    m.dirty.clear()
    m.forget({7}, 2)  # list channel: our message is dropped
    assert [x.id for x in m.list_messages] == [8] and m.dirty.is_set()


class RecordingMapping:
    def __init__(self, name):
        self.name, self.calls = name, []

    def on_message(self, message):
        self.calls.append(("message", message.id))

    def forget(self, message_ids, channel_id):
        self.calls.append(("forget", sorted(message_ids), channel_id))


class RecordingReactions:
    def __init__(self):
        self.calls = []

    async def add_reactions(self, message, *, edited=False):
        self.calls.append(("edit" if edited else "send", message.id))

    def forget(self, message_ids):
        self.calls.append(("forget", sorted(message_ids)))


def test_build_groups_lists_and_routes_shared_channels():
    # source 1 feeds lists 2 and 3; list 3 also collects source 4
    client = HaskhaBot(config(MappingConfig(1, 2), MappingConfig(1, 3), MappingConfig(4, 3)))
    client.build()
    lists = {m.list_id: m.source_ids for m in client.mappings}
    assert lists == {2: (1,), 3: (1, 4)}
    assert [m.list_id for m in client.by_channel[1]] == [2, 3]  # the shared source routes to both lists
    assert [m.list_id for m in client.by_channel[4]] == [3]
    assert sorted(client.reactions) == [1, 4]  # one reactions handler per source, however many lists


def test_client_routes_events_to_every_list_using_the_channel():
    client = HaskhaBot(config(MappingConfig(1, 2), MappingConfig(1, 3), MappingConfig(4, 3)))
    a, b = RecordingMapping("list 2"), RecordingMapping("list 3")
    r1, r4 = RecordingReactions(), RecordingReactions()
    client.by_channel = {1: [a, b], 2: [a], 3: [b], 4: [b]}
    client.reactions = {1: r1, 4: r4}
    fetched = []

    async def fetch_message(message_id):
        fetched.append(message_id)
        return NS(id=message_id, channel=NS(id=1))

    client.get_channel = {1: NS(fetch_message=fetch_message)}.get

    async def events():
        await client.on_message(NS(id=10, channel=NS(id=1)))
        await client.on_raw_message_edit(NS(message_id=11, channel_id=1))
        await client.on_raw_message_edit(NS(message_id=12, channel_id=3))  # a list channel: ignored
        await client.on_raw_message_delete(NS(message_id=13, channel_id=4))
        await client.on_raw_bulk_message_delete(NS(message_ids={14, 15}, channel_id=2))
        await client.on_message(NS(id=16, channel=NS(id=99)))  # unmapped channel: ignored

    asyncio.run(events())
    assert fetched == [11]  # an edit is fetched once, however many lists use the channel
    assert a.calls == [("message", 10), ("message", 11), ("forget", [14, 15], 2)]
    assert b.calls == [("message", 10), ("message", 11), ("forget", [13], 4)]
    assert r1.calls == [("send", 10), ("edit", 11)]  # reacted once, not once per list
    assert r4.calls == [("forget", [13])]
