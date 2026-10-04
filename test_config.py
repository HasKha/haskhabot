"""Checks for mappings loading and routing. Run: python test_config.py"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from bot import Config, Entry, Mapping, MappingConfig, read_mappings


def load(data, env=None):
    """read_mappings against a temp file holding `data` (str written as-is, else JSON), or no file if None."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mappings.json"
        if data is not None:
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return read_mappings(path, env or {})


def fails(text, data, env=None):
    try:
        load(data, env)
    except ValueError as e:
        assert text in str(e), f"expected {text!r} in {str(e)!r}"
    else:
        raise AssertionError(f"expected ValueError containing {text!r}")


def test_valid_file():
    got = load([{"source": 1, "list": 2}, {"source": 3, "list": 4}])
    assert got == (MappingConfig(1, 2), MappingConfig(3, 4))


def test_duplicate_list():
    fails("unique", [{"source": 1, "list": 9}, {"source": 2, "list": 9}])


def test_duplicate_source():
    fails("unique", [{"source": 1, "list": 2}, {"source": 1, "list": 3}])


def test_channel_in_both_roles():
    fails("unique", [{"source": 1, "list": 2}, {"source": 2, "list": 3}])


def test_source_equals_list():
    fails("unique", [{"source": 1, "list": 1}])


def test_malformed_json():
    fails("not valid JSON", "[{")


def test_wrong_shape():
    fails("non-empty list", {"source": 1, "list": 2})
    fails("entry 2", [{"source": 1, "list": 2}, {"source": 3}])


def test_string_ids_rejected():
    fails("entry 1", [{"source": "1", "list": "2"}])


def test_empty_list_rejected():
    fails("non-empty list", [])


def test_utf8_bom_accepted():
    bom = "﻿" + json.dumps([{"source": 1, "list": 2}])
    assert load(bom) == (MappingConfig(1, 2),)


def test_env_fallback():
    got = load(None, {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "6"})
    assert got == (MappingConfig(5, 6),)


def test_env_fallback_needs_both():
    fails("No mappings", None, {"SOURCE_CHANNEL_ID": "5"})
    fails("No mappings", None, {})


def test_env_fallback_still_validated():
    fails("unique", None, {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "5"})


def test_file_wins_over_env():
    got = load([{"source": 1, "list": 2}], {"SOURCE_CHANNEL_ID": "5", "LIST_CHANNEL_ID": "6"})
    assert got == (MappingConfig(1, 2),)


def test_forget_routes_by_channel():
    config = Config(token="x", mappings=(), preview_lines=3, history_limit=None, keep_seconds=0)
    m = Mapping(None, MappingConfig(1, 2), config)
    m.entries[5] = Entry(timestamp=1, author_id=1, preview="", url="u", message_id=5)
    m.list_messages = [SimpleNamespace(id=7), SimpleNamespace(id=8)]

    m.forget({5}, 3)  # some other channel: ignored
    assert 5 in m.entries and not m.dirty.is_set()

    m.forget({5}, 1)  # source channel: the entry is dropped
    assert not m.entries and m.dirty.is_set()

    m.dirty.clear()
    m.forget({7}, 2)  # list channel: our message is dropped
    assert [x.id for x in m.list_messages] == [8] and m.dirty.is_set()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
