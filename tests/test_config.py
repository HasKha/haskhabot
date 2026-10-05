"""Tests for mappings loading and event routing. Run with: pytest"""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from bot import Config, Entry, HaskhaBot, Mapping, MappingConfig, read_mappings


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


@pytest.mark.parametrize("data", [
    [{"source": 1, "list": 9}, {"source": 2, "list": 9}],  # shared list
    [{"source": 1, "list": 2}, {"source": 1, "list": 3}],  # shared source
    [{"source": 1, "list": 2}, {"source": 2, "list": 3}],  # a channel in both roles
    [{"source": 1, "list": 1}],                            # source == list
])
def test_channel_ids_must_be_unique(load, data):
    with pytest.raises(ValueError, match="unique"):
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
    with pytest.raises(ValueError, match="unique"):
        load(None, {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "5"})


def test_env_fallback_names_a_bad_id(load):
    with pytest.raises(ValueError, match="SOURCE_CHANNEL_ID must be a channel ID.*'12x'"):
        load(None, {"SOURCE_CHANNEL_ID": "12x", "LIST_CHANNEL_ID": "6"})


# --- routing ---

def test_forget_routes_by_channel():
    m = Mapping(None, MappingConfig(1, 2), config())
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

    async def on_edit(self, payload):
        self.calls.append(("edit", payload.message_id))

    def forget(self, message_ids, channel_id):
        self.calls.append(("forget", sorted(message_ids), channel_id))


def test_client_routes_events_to_the_mapping_owning_the_channel():
    client = HaskhaBot(config(MappingConfig(1, 2), MappingConfig(3, 4)))
    a, b = RecordingMapping("a"), RecordingMapping("b")
    client.by_channel = {1: a, 2: a, 3: b, 4: b}

    async def events():
        await client.on_message(NS(id=10, channel=NS(id=3)))
        await client.on_raw_message_edit(NS(message_id=11, channel_id=1))
        await client.on_raw_message_delete(NS(message_id=12, channel_id=4))
        await client.on_raw_bulk_message_delete(NS(message_ids={13, 14}, channel_id=2))
        await client.on_message(NS(id=15, channel=NS(id=99)))  # unmapped channel: ignored

    asyncio.run(events())
    assert a.calls == [("edit", 11), ("forget", [13, 14], 2)]
    assert b.calls == [("message", 10), ("forget", [12], 4)]
