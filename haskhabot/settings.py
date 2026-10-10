"""Settings: config.json (or the legacy channel-pair env vars) and the .env values."""

from __future__ import annotations

import collections.abc
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


@dataclass(frozen=True)
class MappingConfig:
    source_id: int
    list_id: int


def env_channel_id(env: collections.abc.Mapping[str, str], name: str) -> int:
    try:
        return int(env[name])
    except ValueError:
        raise ValueError(f"{name} must be a channel ID (digits only), got {env[name]!r}") from None


def read_config_file(
    path: Path, env: collections.abc.Mapping[str, str]
) -> tuple[tuple[MappingConfig, ...], tuple[int, ...] | None]:
    """(mappings, reaction channels) from the JSON file, else the legacy SOURCE_CHANNEL_ID/LIST_CHANNEL_ID pair.

    Reaction channels are None when the file doesn't set "reaction_channels": react in the source channels.
    Raises ValueError with a readable message if the settings are missing or invalid.
    """
    reaction_channels = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: Notepad adds a BOM
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e
        except (OSError, UnicodeDecodeError) as e:
            # e.g. a directory: Docker creates one when bind-mounting a host file that doesn't exist.
            raise ValueError(f"Can't read {path}: {e}") from e
        items = raw.get("mappings") if isinstance(raw, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError(f'{path} must be an object with a non-empty "mappings" list of '
                             '{"source": ..., "list": ...} objects')
        mappings = []
        for i, item in enumerate(items, 1):
            # type() is int, not isinstance: rejects "123" strings and true/false.
            if not isinstance(item, dict) or not all(type(item.get(k)) is int for k in ("source", "list")):
                raise ValueError(f'{path} mappings entry {i} needs integer "source" and "list" channel IDs (no quotes)')
            mappings.append(MappingConfig(item["source"], item["list"]))
        reaction_channels = raw.get("reaction_channels")
        if reaction_channels is not None:
            if not isinstance(reaction_channels, list) or not all(type(c) is int for c in reaction_channels):
                raise ValueError(f'{path} "reaction_channels" must be a list of integer channel IDs (no quotes)')
            reaction_channels = tuple(dict.fromkeys(reaction_channels))
    elif env.get("SOURCE_CHANNEL_ID") and env.get("LIST_CHANNEL_ID"):
        mappings = [MappingConfig(env_channel_id(env, "SOURCE_CHANNEL_ID"), env_channel_id(env, "LIST_CHANNEL_ID"))]
    else:
        raise ValueError(f"No mappings: create {path} or set SOURCE_CHANNEL_ID and LIST_CHANNEL_ID")

    # A source may feed several lists and a list may collect several sources, but a channel can't be
    # both: the bot would read its own list embeds as event posts.
    both = sorted({m.source_id for m in mappings} & {m.list_id for m in mappings})
    if both:
        raise ValueError("A channel can't be both a source and a list; used as both: " + ", ".join(map(str, both)))
    repeated = sorted({f"{m.source_id} → {m.list_id}" for m in mappings if mappings.count(m) > 1})
    if repeated:
        raise ValueError("Pair listed more than once: " + ", ".join(repeated))
    # Same reason: the bot would react to its own list embeds.
    reacting_in_lists = sorted(set(reaction_channels or ()) & {m.list_id for m in mappings})
    if reacting_in_lists:
        raise ValueError("A list channel can't get signup reactions: " + ", ".join(map(str, reacting_in_lists)))
    return tuple(mappings), reaction_channels


def group_by_list(pairs: Iterable[MappingConfig]) -> dict[int, tuple[int, ...]]:
    """List channel -> the source channels it collects, in the order they appear in the config."""
    lists: dict[int, list[int]] = {}
    for pair in pairs:
        lists.setdefault(pair.list_id, []).append(pair.source_id)
    return {list_id: tuple(sources) for list_id, sources in lists.items()}


@dataclass(frozen=True)
class Config:
    token: str
    mappings: tuple[MappingConfig, ...]
    preview_lines: int
    history_limit: int | None
    keep_seconds: float  # how long an event stays listed after it starts
    reaction_channels: tuple[int, ...] | None = None  # None: the source channels


def load_config() -> Config:
    load_dotenv()
    if not os.getenv("DISCORD_TOKEN"):
        sys.exit("Missing required settings in .env: DISCORD_TOKEN")
    path = Path(os.getenv("CONFIG_FILE") or "config.json")
    try:
        mappings, reaction_channels = read_config_file(path, os.environ)
    except ValueError as e:
        sys.exit(str(e))
    history = os.getenv("HISTORY_LIMIT", "").strip()
    return Config(
        token=os.environ["DISCORD_TOKEN"],
        mappings=mappings,
        preview_lines=int(os.getenv("PREVIEW_LINES", "3")),
        history_limit=int(history) if history else None,
        keep_seconds=float(os.getenv("KEEP_AFTER_START_HOURS", "4")) * 3600,
        reaction_channels=reaction_channels,
    )
