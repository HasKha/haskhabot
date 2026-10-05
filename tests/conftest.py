import sys
from pathlib import Path
from types import SimpleNamespace as NS

import discord

# Make bot.py importable from the tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeChannel(discord.abc.GuildChannel, discord.abc.Messageable):
    """A guild text channel that grants exactly the given permissions to everyone."""

    def __init__(self, name, **permissions):
        self.name, self.guild = name, NS(me=object())
        self._permissions = discord.Permissions(**permissions)

    def permissions_for(self, member):
        return self._permissions
