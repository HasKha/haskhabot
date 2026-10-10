"""Tests for config loading and event routing. Run with: pytest"""

import asyncio
import json
import os
from types import SimpleNamespace as NS

import pytest

from haskhabot.bot import HaskhaBot
from haskhabot.settings import Config, MappingConfig, group_by_list, load_config, read_config_file
from haskhabot.events import Entry
from haskhabot.mapping import Mapping


@pytest.fixture
def load_file(tmp_path):
    """read_config_file against a file holding `data` (str written as-is, else JSON), or no file if None."""

    def load_file(data):
        path = tmp_path / "config.json"
        if data is not None:
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return read_config_file(path)

    return load_file


@pytest.fixture
def load(load_file):
    """Just the mappings, from a file whose "mappings" is `data`."""
    return lambda data: load_file({"mappings": data})[0]

def config(*mappings):
    return Config(token="x", mappings=mappings, preview_lines=3, history_limit=None, keep_seconds=0)


# --- config.json ---

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
    ({"source": 1, "list": 2}, "non-empty \"mappings\" list"),
    ([], "non-empty \"mappings\" list"),
    ([{"source": 1, "list": 2}, {"source": 3}], "entry 2"),
    ([{"source": "1", "list": "2"}], "entry 1"),  # quoted IDs
])
def test_invalid_mappings(load, data, message):
    with pytest.raises(ValueError, match=message):
        load(data)


@pytest.mark.parametrize("data, message", [
    ("{[", "not valid JSON"),
    ([{"source": 1, "list": 2}], 'object with a non-empty "mappings"'),  # the old bare-list format
    ({"reaction_channels": [1]}, 'object with a non-empty "mappings"'),
])
def test_invalid_file(load_file, data, message):
    with pytest.raises(ValueError, match=message):
        load_file(data)


def test_utf8_bom_accepted(load_file):
    assert load_file("﻿" + json.dumps({"mappings": [{"source": 1, "list": 2}]}))[0] == (MappingConfig(1, 2),)


def test_unreadable_path_is_a_readable_error(tmp_path):
    # Docker creates a directory when bind-mounting a host file that doesn't exist.
    path = tmp_path / "config.json"
    path.mkdir()
    with pytest.raises(ValueError, match="Can't read"):
        read_config_file(path)


def test_missing_file_is_an_error(load_file):
    with pytest.raises(ValueError, match="config.json not found: copy config.example.json"):
        load_file(None)


@pytest.mark.parametrize("data, message", [
    ({"mappings": [{"source": 1, "list": 2}], "reaction_channel": []}, 'unknown key "reaction_channel"'),
    ({"mapping": [{"source": 1, "list": 2}]}, 'unknown key "mapping"'),
    ({"mappings": [{"source": 1, "list": 2, "lsit": 3}]}, 'mappings entry 1: unknown key "lsit"'),
])
def test_unknown_keys_are_rejected(load_file, data, message):
    with pytest.raises(ValueError, match=message):
        load_file(data)


def test_load_config_reads_from_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DISCORD_TOKEN=abc\n", encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({"mappings": [{"source": 1, "list": 2}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("DISCORD_TOKEN", "CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    try:
        loaded = load_config()
    finally:
        os.environ.pop("DISCORD_TOKEN", None)  # load_dotenv set it outside monkeypatch's control
    assert loaded.token == "abc" and loaded.mappings == (MappingConfig(1, 2),)


def test_config_file_env_var_picks_the_file(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"mappings": [{"source": 3, "list": 4}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "abc")
    monkeypatch.setenv("CONFIG_FILE", str(other))
    assert load_config().mappings == (MappingConfig(3, 4),)

# --- reaction_channels ---

MAPPING = {"source": 1, "list": 2}


@pytest.mark.parametrize("extra, expected", [
    ({}, None),  # unset: react in the sources
    ({"reaction_channels": []}, ()),  # explicitly none
    ({"reaction_channels": [1, 9, 9]}, (1, 9)),
])
def test_reaction_channels(load_file, extra, expected):
    assert load_file({"mappings": [MAPPING], **extra})[1] == expected


@pytest.mark.parametrize("value, message", [
    ("1", "list of integer"),
    (["1"], "list of integer"),
    ([True], "list of integer"),
    ([2], "list channel can't get signup reactions: 2"),
])
def test_invalid_reaction_channels(load_file, value, message):
    with pytest.raises(ValueError, match=message):
        load_file({"mappings": [MAPPING], "reaction_channels": value})


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
    def __init__(self, name, source_ids):
        self.name, self.source_ids, self.calls = name, source_ids, []

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
    a, b = RecordingMapping("list 2", (1,)), RecordingMapping("list 3", (1, 4))
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
