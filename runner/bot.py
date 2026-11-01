from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import re
import shutil

import discord
from discord import app_commands

from .capture import SlideRecorder, pause_or_stop
from .config import Config
from .delivery import deliver_session
from .state import Session, recover_sessions

LOG = logging.getLogger(__name__)


class ClassPointCommands(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="classpoint", description="Record unique ClassPoint slides and receive a PDF")
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot = self.bot
        if interaction.guild_id != bot.config.guild_id:
            await interaction.response.send_message("This runner belongs to a different server.", ephemeral=False)
            return False
        if bot.config.allowed_user_ids and interaction.user.id not in bot.config.allowed_user_ids:
            await interaction.response.send_message("You aren't on this runner's allowed-user list.", ephemeral=False)
            return False
        channel = bot.output_channel()
        if channel is None:
            await interaction.response.send_message(
                f"I can't find a unique #{bot.config.channel_name} text channel. Check the runner configuration and my access.", ephemeral=False)
            return False
        if interaction.channel_id != channel.id:
            await interaction.response.send_message(f"Use this command in {channel.mention}.", ephemeral=False)
            return False
        permissions = channel.permissions_for(interaction.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.attach_files, permissions.read_message_history)):
            await interaction.response.send_message(
                "I need View Channel, Send Messages, Attach Files and Read Message History in this channel.", ephemeral=False)
            return False
        return True

    @app_commands.command(description="Join a class and capture unique slides until the session ends")
    @app_commands.describe(code="ClassPoint class code", name="Your classroom name (defaults to your Discord display name)")
    async def start(self, interaction: discord.Interaction, code: str, name: str | None = None):
        bot = self.bot
        code = code.strip().upper()
        name = " ".join((name or interaction.user.display_name).split())[:25]
        if not re.fullmatch(r"[A-Za-z0-9]{1,8}", code) or not name:
            await interaction.response.send_message("Enter a code of 1-8 letters/numbers and a nonempty classroom name.", ephemeral=False)
            return
        async with bot.start_lock:
            if bot.shutting_down:
                await interaction.response.send_message("The runner is restarting; try again shortly.", ephemeral=False)
                return
            if bot.recording_task and not bot.recording_task.done():
                await interaction.response.send_message("A class is already recording. Use /classpoint status or /classpoint stop first.", ephemeral=False)
                return
            if shutil.disk_usage(bot.data_root).free < bot.config.min_free_mb * 1024 * 1024:
                await interaction.response.send_message("The runner is low on disk space. Existing recordings are safe; free space before starting.", ephemeral=False)
                return
            await interaction.response.defer(ephemeral=False, thinking=True)
            session = Session.create(bot.data_root, code, name, interaction.user.id, interaction.channel_id)
            bot.sessions.append(session)
            bot.active = session
            bot.stop_event = asyncio.Event()
            bot.recording_task = asyncio.create_task(bot.record(session, interaction), name="classpoint-record")

    @app_commands.command(description="Finish the current recording and upload its PDF")
    async def stop(self, interaction: discord.Interaction):
        bot = self.bot
        if not bot.active or not bot.recording_task or bot.recording_task.done():
            await interaction.response.send_message("No class is recording right now.", ephemeral=False)
            return
        if interaction.user.id != bot.active.data["requester_id"] and not interaction.permissions.manage_guild:
            await interaction.response.send_message("Only the person who started this class or a server manager can stop it.", ephemeral=False)
            return
        bot.stop_event.set()
        await interaction.response.send_message("Stopping. I'll upload the captured slides as a PDF here.", ephemeral=False)

    @app_commands.command(description="Show the current recording and pending PDF uploads")
    async def status(self, interaction: discord.Interaction):
        bot = self.bot
        if bot.active:
            message = bot.session_status(bot.active)
        else:
            message = "Ready for /classpoint start. No class is currently recording."
        pending = [s for s in bot.sessions if s.data["status"] == "pending_upload"]
        message += f"\nPending uploads: {len(pending)}."
        if pending and pending[-1].data.get("upload_error"):
            message += "\nLast upload error: " + pending[-1].data["upload_error"][:300]
        elif bot.sessions and bot.sessions[-1].data["status"] == "uploaded":
            message += f"\nLast class: {bot.sessions[-1].data['code']} - uploaded."
        await interaction.response.send_message(message, ephemeral=False)

    @app_commands.command(description="Retry finished recordings whose PDF upload failed")
    async def retry(self, interaction: discord.Interaction):
        self.bot.upload_wakeup.set()
        await interaction.response.send_message("Retrying pending PDF uploads. Already uploaded parts will be skipped.", ephemeral=False)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        LOG.error("Slash command failed", exc_info=error)
        message = "The command failed. Existing slides are safe; check the runner logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=False)
        else:
            await interaction.response.send_message(message, ephemeral=False)


class ClassPointBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, application_id=config.application_id,
                         allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(ClassPointCommands(self), guild=discord.Object(id=config.guild_id))
        self.data_root = Path(config.data_dir).expanduser()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sessions = recover_sessions(self.data_root)
        self.active: Session | None = None
        self.recording_task: asyncio.Task | None = None
        self.upload_task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.upload_wakeup = asyncio.Event()
        self.start_lock = asyncio.Lock()
        self.shutting_down = False

    async def setup_hook(self):
        if self.user.id != self.config.application_id:
            raise ValueError("Bot token belongs to a different Discord application")
        await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        self.upload_task = asyncio.create_task(self.upload_loop(), name="classpoint-uploads")

    def output_channel(self):
        guild = self.get_guild(self.config.guild_id)
        if not guild:
            return None
        if self.config.channel_id:
            channel = guild.get_channel(self.config.channel_id)
            return channel if isinstance(channel, discord.TextChannel) else None
        matches = [channel for channel in guild.text_channels if channel.name == self.config.channel_name]
        return matches[0] if len(matches) == 1 else None

    async def on_ready(self):
        channel = self.output_channel()
        if channel:
            LOG.info("Ready as %s in guild %s, channel #%s (%s)", self.user, self.config.guild_id, channel.name, channel.id)
        else:
            LOG.error("Bot connected, but configured guild/channel is unavailable or ambiguous")

    async def edit_reply(self, interaction, message):
        try:
            await interaction.edit_original_response(content=message)
        except discord.HTTPException:
            LOG.warning("Could not update the start reply")

    @staticmethod
    def session_status(session):
        started = int(datetime.fromisoformat(session.data["started_at"]).timestamp())
        return (f"Class {session.data['code']}: {session.data['phase']}; "
                f"{len(session.data['slides'])} unique slides saved.\nStarted <t:{started}:R>.")

    async def periodic_status(self, session, stop):
        """Use channel messages so progress can outlive the start interaction."""
        while not stop.is_set():
            await pause_or_stop(stop, self.config.status_update_seconds)
            if stop.is_set() or self.active is not session or session.data["status"] != "recording":
                return
            channel = self.output_channel()
            if not channel or channel.id != session.data["channel_id"]:
                LOG.warning("Skipping progress update: original channel is unavailable")
                continue
            try:
                await channel.send("Recording update\n" + self.session_status(session),
                                   allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, OSError):
                LOG.warning("Could not post progress update for %s; will retry next interval", session.data["id"])

    async def record(self, session, interaction):
        status_task = None
        stop = self.stop_event

        async def joined():
            nonlocal status_task
            await self.edit_reply(interaction,
                f"Joined class {session.data['code']} as {discord.utils.escape_markdown(session.data['name'])}. "
                "Capturing unique slides; I'll post the PDF here when the class ends. "
                "Use /classpoint status or /classpoint stop any time.")
            if status_task is None:
                status_task = asyncio.create_task(self.periodic_status(session, stop), name="classpoint-progress")

        try:
            reason = await SlideRecorder(self.config, session).run(stop, joined)
        except asyncio.CancelledError:
            reason = "Runner shutdown interrupted the recording; captured slides are preserved."
        except (ValueError, InterruptedError, TimeoutError) as error:
            reason = str(error)
            await self.edit_reply(interaction, reason)
        except Exception:
            LOG.exception("Recording failed for session %s", session.data["id"])
            reason = "Recorder encountered an error; captured slides are preserved. Check the runner logs."
            await self.edit_reply(interaction, reason)
        finally:
            if status_task is not None:
                status_task.cancel()
                await asyncio.gather(status_task, return_exceptions=True)
            # Finalize saved slides even if capture failed.
            session.finish(locals().get("reason", "Recording was interrupted."))
            self.active = None
            self.upload_wakeup.set()

    async def upload_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            self.upload_wakeup.clear()
            for session in list(self.sessions):
                if session.data["status"] != "pending_upload":
                    continue
                try:
                    channel = self.output_channel()
                    if not channel or channel.id != session.data["channel_id"]:
                        raise ValueError("The original output channel is unavailable. Restore its configuration/access to retry.")
                    await deliver_session(session, channel, self.user.id)
                    LOG.info("Delivered session %s", session.data["id"])
                except Exception as error:
                    LOG.exception("Upload pending for session %s", session.data["id"])
                    session.data["upload_error"] = f"{type(error).__name__}: {str(error)[:300]}"
                    session.save()
            try:
                await asyncio.wait_for(self.upload_wakeup.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.shutting_down = True
        self.stop_event.set()
        if self.recording_task and not self.recording_task.done():
            try:
                await asyncio.wait_for(self.recording_task, timeout=50)
            except asyncio.TimeoutError:
                pass
        if self.upload_task:
            self.upload_task.cancel()
            await asyncio.gather(self.upload_task, return_exceptions=True)
        await super().close()
