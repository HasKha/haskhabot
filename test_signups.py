"""Checks for signup reactions and the /listsignups tally. Run: python test_signups.py"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import discord

from bot import find_emotes, signup_emojis

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
