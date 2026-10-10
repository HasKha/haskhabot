"""haskhabot: mirrors timestamped posts from one channel into a sorted list in another."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .mapping import RETRY_SECONDS, Mapping
from .settings import Config, group_by_list, load_config
from .signups import ReactionChannel, format_signups, signup_rows

log = logging.getLogger("haskhabot")

NOT_AN_EVENT_THREAD = "Run this in the thread of an event post."


class HaskhaBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: must also be enabled in the Developer Portal
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.mappings: list[Mapping] = []
        self.by_channel: dict[int, list[Mapping]] = {}  # source and list channel ids -> the lists using them
        self.reactions: dict[int, ReactionChannel] = {}  # reaction channel id -> its signup reactions
        self.tree = app_commands.CommandTree(self)

    def build(self) -> None:
        """One Mapping per list channel, routing by channel id, and one ReactionChannel per reaction channel."""
        lists = group_by_list(self.config.mappings)
        self.mappings = [Mapping(self, list_id, sources, self.config) for list_id, sources in lists.items()]
        self.by_channel = {}
        for m in self.mappings:
            for channel_id in (*m.source_ids, m.list_id):
                self.by_channel.setdefault(channel_id, []).append(m)
        react_in = self.config.reaction_channels
        if react_in is None:
            react_in = tuple(dict.fromkeys(pair.source_id for pair in self.config.mappings))
        self.reactions = {channel_id: ReactionChannel(self, channel_id) for channel_id in react_in}

    async def setup_hook(self) -> None:
        # Built here, not in __init__: asyncio.Lock/Event need the running loop on older Pythons.
        self.build()
        self.updaters = [asyncio.create_task(m.run_updater()) for m in self.mappings]
        self.watcher = asyncio.create_task(self.watch_access())

        @self.tree.command(name="listsignups", description="List who signed up, by reaction, on this event's post")
        @app_commands.guild_only()
        async def listsignups(interaction: discord.Interaction) -> None:
            await self.list_signups(interaction)

        try:
            await self.tree.sync()
        except Exception:  # any failure, not just HTTP: exiting here would only restart-loop the bot
            log.exception("Couldn't register /listsignups; the list itself keeps working")

    async def on_ready(self) -> None:
        # Fires again after a full reconnect, so rescanning here also catches anything missed offline.
        log.info("Logged in as %s", self.user)
        reacting = [r.where() for r in self.reactions.values() if r.check()]  # check() warns about the rest
        log.info("Adding signup reactions in: %s", ", ".join(reacting) or "no channels")
        # Concurrently, so a big channel's history scan doesn't delay the other lists.
        await asyncio.gather(*(m.rescan_logged() for m in self.mappings))

    async def watch_access(self) -> None:
        # Stay connected and retry rather than exiting: a restart loop would burn through Discord's
        # login limit. Polling also picks up permission changes, which the bot gets no event for:
        # losing access (e.g. a channel re-synced with its category) stops message events silently.
        # Each list is handled on its own, so one with a problem doesn't hold up the others.
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(RETRY_SECONDS)
            for reactions in self.reactions.values():
                reactions.check()  # warns if Add Reactions was removed or granted since last time
            await asyncio.gather(*(m.recheck_access() for m in self.mappings))

    async def list_signups(self, interaction: discord.Interaction) -> None:
        thread = interaction.channel
        parent_id = getattr(thread, "parent_id", None)
        found = [m for m in self.by_channel.get(parent_id, []) if m.event_in_thread(thread) is not None]
        # A reaction channel needn't be a source, so its posts may not be listed anywhere: the reactions
        # themselves tell whether it's a signup post (the bot only reacts to event posts).
        source = self.get_channel(parent_id) if found or parent_id in self.reactions else None
        if source is None:
            await interaction.response.send_message(NOT_AN_EVENT_THREAD, ephemeral=True)
            return
        await interaction.response.defer()  # reading reactions can take longer than Discord's 3 seconds
        try:
            message = await source.fetch_message(thread.id)
            rows = await signup_rows(message, self.user.id)
        except discord.HTTPException:
            log.exception("[%s] Couldn't read signups for %s", parent_id, thread.id)
            await interaction.followup.send("Couldn't read that post's reactions.", ephemeral=True)
            return
        if not rows:
            await interaction.followup.send("No signup reactions on this post.")
            return
        embed = discord.Embed(description=format_signups(rows), colour=discord.Colour.blurple())
        await interaction.followup.send(embed=embed)

    async def on_message(self, message: discord.Message) -> None:
        channel_id = message.channel.id
        for mapping in self.by_channel.get(channel_id, []):
            mapping.on_message(message)
        if channel_id in self.reactions:
            await self.reactions[channel_id].add_reactions(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        channel_id = payload.channel_id
        # Watched as a source and reacted in are independent; list channels hold our own posts and are neither.
        watching = [m for m in self.by_channel.get(channel_id, []) if channel_id in m.source_ids]
        reactions = self.reactions.get(channel_id)
        if not watching and reactions is None:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)  # once, for every list using this source
        except (discord.NotFound, discord.Forbidden):
            return
        for mapping in watching:
            mapping.on_message(message)
        if reactions is not None:
            await reactions.add_reactions(message, edited=True)  # only emotes the edit added

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        self.forget({payload.message_id}, payload.channel_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        self.forget(set(payload.message_ids), payload.channel_id)

    def forget(self, message_ids: set[int], channel_id: int) -> None:
        for mapping in self.by_channel.get(channel_id, []):
            mapping.forget(message_ids, channel_id)
        if channel_id in self.reactions:
            self.reactions[channel_id].forget(message_ids)

    async def on_guild_emojis_update(self, guild: discord.Guild, before, after) -> None:
        for mapping in self.mappings:
            if mapping.target is not None and guild == mapping.target.guild:
                mapping.dirty.set()


def main() -> None:
    config = load_config()
    HaskhaBot(config).run(config.token, root_logger=True)

