"""Settings: channels in config.json, the token and tuning in environment variables (or .env).

Relative paths, including the defaults, are relative to the working directory.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

CONFIG_KEYS = ("mappings", "reaction_channels")
MAPPING_KEYS = ("source", "list")


@dataclass(frozen=True)
class MappingConfig:
    source_id: int
    list_id: int


def check_keys(where: str, item: dict, allowed: tuple[str, ...]) -> None:
    """Reject keys we don't know, so a typo fails loudly instead of quietly falling back to a default."""
    unknown = [k for k in item if k not in allowed]
    if unknown:
        raise ValueError(f"{where}: unknown key {', '.join(map(json.dumps, unknown))} "
                         f"(expected {', '.join(map(json.dumps, allowed))})")


def read_config_file(path: Path) -> tuple[tuple[MappingConfig, ...], tuple[int, ...] | None]:
    """(mappings, reaction channels) from the JSON file.

    Reaction channels are None when the file doesn't set "reaction_channels": react in the source channels.
    Raises ValueError with a readable message if the file is missing or invalid.
    """
    if not path.exists():
        raise ValueError(f"{path.resolve()} not found: copy config.example.json to it, or set CONFIG_FILE")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: Notepad adds a BOM
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        # e.g. a directory: Docker creates one when bind-mounting a host file that doesn't exist.
        raise ValueError(f"Can't read {path}: {e}") from e
    if isinstance(raw, dict):
        check_keys(str(path), raw, CONFIG_KEYS)
    items = raw.get("mappings") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError(f'{path} must be an object with a non-empty "mappings" list of '
                         '{"source": ..., "list": ...} objects')
    mappings = []
    for i, item in enumerate(items, 1):
        # type() is int, not isinstance: rejects "123" strings and true/false.
        if not isinstance(item, dict) or not all(type(item.get(k)) is int for k in MAPPING_KEYS):
            raise ValueError(f'{path} mappings entry {i} needs integer "source" and "list" channel IDs (no quotes)')
        check_keys(f"{path} mappings entry {i}", item, MAPPING_KEYS)
        mappings.append(MappingConfig(item["source"], item["list"]))
    reaction_channels = raw.get("reaction_channels")
    if reaction_channels is not None:
        if not isinstance(reaction_channels, list) or not all(type(c) is int for c in reaction_channels):
            raise ValueError(f'{path} "reaction_channels" must be a list of integer channel IDs (no quotes)')
        reaction_channels = tuple(dict.fromkeys(reaction_channels))

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
    # Real environment variables win over .env, so Docker's env_file and `docker run -e` behave as expected.
    load_dotenv(".env")
    if not os.getenv("DISCORD_TOKEN"):
        sys.exit("Missing required setting DISCORD_TOKEN (set it in the environment or in .env)")
    try:
        mappings, reaction_channels = read_config_file(Path(os.getenv("CONFIG_FILE") or "config.json"))
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
