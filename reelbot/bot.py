"""Discord client, slash commands and the message context menu."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from .allowlist import Allowlist
from .config import Config
from .downloader import Downloader, DownloadError
from .links import ReelLink, find_link

log = logging.getLogger(__name__)

PRIVATE_MSG = "This bot is private."
NO_LINK_MSG = "I couldn't find an Instagram reel or post link there."
OWNER_ONLY_MSG = "Only the bot owner can do that."

# Applied to every command so they work when user-installed: DMs, group DMs and servers.
installs = app_commands.allowed_installs(guilds=True, users=True)
contexts = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)


class ReelBot(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.allowlist = Allowlist(
            config.allowlist_path,
            config.owner_id,
            allow_everyone=config.allow_everyone,
            seed=config.allowed_user_ids,
        )
        self.downloader = Downloader(
            cookies=config.ig_cookies, max_concurrent=config.max_concurrent_downloads
        )
        _register_commands(self)

    async def setup_hook(self) -> None:
        # Sync once per process start, not on every reconnect (on_ready fires repeatedly).
        synced = await self.tree.sync()
        log.info("Synced %d global command(s)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id if self.user else "?")

    async def handle_reel(self, interaction: discord.Interaction, text: str | None, source: str) -> None:
        user_id = interaction.user.id
        # Access check comes first: nothing is deferred or downloaded for strangers.
        if not self.allowlist.is_allowed(user_id):
            log.info("denied user=%s source=%s", user_id, source)
            await interaction.response.send_message(PRIVATE_MSG, ephemeral=True)
            return

        link = find_link(text)
        if link is None:
            log.info("no-link user=%s source=%s", user_id, source)
            await interaction.response.send_message(NO_LINK_MSG, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        log.info("request user=%s source=%s link=%s", user_id, source, link.url)

        limit = self.config.max_upload_bytes
        # Discord tells us the real per-interaction limit (discord.py >= 2.5).
        interaction_limit = getattr(interaction, "filesize_limit", None)
        if interaction_limit:
            limit = min(limit, interaction_limit)

        with tempfile.TemporaryDirectory(prefix="reelbot-", ignore_cleanup_errors=True) as tmp:
            try:
                path = await self.downloader.fetch(link.url, Path(tmp), limit)
            except DownloadError as e:
                reason = str(e)
                log.warning("failed user=%s link=%s reason=%s", user_id, link.url, reason)
            except Exception:
                reason = "something went wrong"
                log.exception("error user=%s link=%s", user_id, link.url)
            else:
                try:
                    await interaction.followup.send(file=discord.File(path, filename="reel.mp4"))
                except discord.HTTPException as e:
                    reason = "the video is too large to upload" if e.status == 413 else "Discord rejected the upload"
                    log.warning("upload-failed user=%s link=%s status=%s code=%s", user_id, link.url, e.status, e.code)
                else:
                    log.info("sent user=%s link=%s bytes=%d", user_id, link.url, path.stat().st_size)
                    return

        await self._send_fallback(interaction, link, reason)

    async def _send_fallback(self, interaction: discord.Interaction, link: ReelLink, reason: str) -> None:
        content = (
            f"{link.embed_url(self.config.embed_fixer_domain)}\n"
            f"-# Couldn't post the video ({reason}), so here's an embed link instead."
        )
        try:
            await interaction.followup.send(content)
        except discord.HTTPException:
            log.exception("fallback-failed link=%s", link.url)


def _register_commands(bot: ReelBot) -> None:
    tree = bot.tree

    @tree.command(name="reel", description="Post an Instagram reel as a video")
    @app_commands.describe(link="Instagram reel or post URL")
    @installs
    @contexts
    async def reel(interaction: discord.Interaction, link: str) -> None:
        await bot.handle_reel(interaction, link, "slash")

    @tree.context_menu(name="Send reel")
    @installs
    @contexts
    async def send_reel(interaction: discord.Interaction, message: discord.Message) -> None:
        text = " ".join([message.content or "", *(e.url for e in message.embeds if e.url)])
        await bot.handle_reel(interaction, text, "context-menu")

    async def owner_only(interaction: discord.Interaction) -> bool:
        if interaction.user.id == bot.config.owner_id:
            return True
        log.info("owner-command-denied user=%s command=%s", interaction.user.id, interaction.command and interaction.command.name)
        await interaction.response.send_message(OWNER_ONLY_MSG, ephemeral=True)
        return False

    @tree.command(name="allow", description="(Owner) Let a user use this bot")
    @app_commands.describe(user="User to allow")
    @installs
    @contexts
    async def allow(interaction: discord.Interaction, user: discord.User) -> None:
        if not await owner_only(interaction):
            return
        added = bot.allowlist.add(user.id)
        log.info("allowlist-add user=%s added=%s", user.id, added)
        msg = f"Allowed {user.mention}." if added else f"{user.mention} is already allowed."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name="disallow", description="(Owner) Stop a user from using this bot")
    @app_commands.describe(user="User to remove")
    @installs
    @contexts
    async def disallow(interaction: discord.Interaction, user: discord.User) -> None:
        if not await owner_only(interaction):
            return
        removed = bot.allowlist.remove(user.id)
        log.info("allowlist-remove user=%s removed=%s", user.id, removed)
        msg = f"Removed {user.mention}." if removed else f"{user.mention} wasn't on the allowlist."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name="allowlist", description="(Owner) Show allowed users")
    @installs
    @contexts
    async def allowlist(interaction: discord.Interaction) -> None:
        if not await owner_only(interaction):
            return
        users = bot.allowlist.users()
        lines = [f"- <@{u}> (`{u}`)" for u in users] or ["(nobody yet)"]
        header = "Allowlist is **disabled** (`ALLOW_EVERYONE=true`).\n" if bot.config.allow_everyone else ""
        text = header + f"Owner: <@{bot.config.owner_id}>\nAllowed users:\n" + "\n".join(lines)
        await interaction.response.send_message(
            text[:2000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
