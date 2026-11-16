from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass
class Recording:
    session: Session
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class ClassPointCommands(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="classpoint", description="Record ClassPoint slides and receive PDFs")
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

    @app_commands.command(description="Join a class and capture slides until the session ends")
    @app_commands.describe(code="ClassPoint class code", name="Classroom name (defaults to saumil's assistant)",
                           export="All captures, or sample rapid scene changes in the PDF; all PNGs are kept")
    @app_commands.choices(export=[app_commands.Choice(name="All captures (default)", value="all"),
                                  app_commands.Choice(name="Sample rapid scene changes (30 seconds)", value="sampled")])
    async def start(self, interaction: discord.Interaction, code: str, name: str | None = None, export: str = "all"):
        bot = self.bot
        code = code.strip().upper()
        name = " ".join((name or "saumil's assistant").split())[:25]
        if not re.fullmatch(r"[A-Za-z0-9]{1,8}", code) or not name or export not in ("all", "sampled"):
            await interaction.response.send_message("Enter a code of 1-8 letters/numbers, a nonempty classroom name, and a valid export mode.", ephemeral=False)
            return
        async with bot.start_lock:
            if bot.shutting_down:
                await interaction.response.send_message("The runner is restarting; try again shortly.", ephemeral=False)
                return
            if code in bot.recordings:
                await interaction.response.send_message(f"Class {code} is already recording. Use /classpoint status code:{code}.", ephemeral=False)
                return
            if len(bot.recordings) >= bot.config.max_concurrent_sessions:
                await interaction.response.send_message(
                    f"All {bot.config.max_concurrent_sessions} recording slots are occupied. Use /classpoint status to see active classes.", ephemeral=False)
                return
            if shutil.disk_usage(bot.data_root).free < bot.config.min_free_mb * 1024 * 1024:
                await interaction.response.send_message("The runner is low on disk space. Existing recordings are safe; free space before starting.", ephemeral=False)
                return
            await interaction.response.defer(ephemeral=False, thinking=True)
            session = Session.create(bot.data_root, code, name, interaction.user.id, interaction.channel_id,
                                     export_mode=export)
            bot.sessions.append(session)
            recording = Recording(session)
            bot.recordings[code] = recording
            recording.task = asyncio.create_task(bot.record(recording, interaction), name=f"classpoint-record-{code}")
            LOG.info("Starting class %s, session %s, export %s", code, session.data["id"], export)

    @app_commands.command(description="Finish a recording and upload its PDF; specify a code when several are active")
    @app_commands.describe(code="Class to stop; optional when only one of your recordings is active")
    async def stop(self, interaction: discord.Interaction, code: str | None = None):
        bot = self.bot
        if not bot.recordings:
            await interaction.response.send_message("No class is recording right now.", ephemeral=False)
            return
        if code:
            recording = bot.recordings.get(code.strip().upper())
            if recording is None:
                await interaction.response.send_message("That class is not recording. Use /classpoint status to see active codes.", ephemeral=False)
                return
        else:
            own = [r for r in bot.recordings.values() if r.session.data["requester_id"] == interaction.user.id]
            if len(own) == 1:
                recording = own[0]
            elif len(bot.recordings) == 1:
                recording = next(iter(bot.recordings.values()))
            else:
                await interaction.response.send_message(
                    "Several classes are recording. Specify /classpoint stop code:<code>. Active codes: "
                    + ", ".join(bot.recordings) + ".", ephemeral=False)
                return
        session = recording.session
        if interaction.user.id != session.data["requester_id"] and not interaction.permissions.manage_guild:
            await interaction.response.send_message("Only the person who started this class or a server manager can stop it.", ephemeral=False)
            return
        recording.stop.set()
        await interaction.response.send_message(f"Stopping class {session.data['code']}. I'll upload its PDF here.", ephemeral=False)

    @app_commands.command(description="Show active recordings, last saved images, and PDF/upload progress")
    @app_commands.describe(code="Optional class code to inspect")
    async def status(self, interaction: discord.Interaction, code: str | None = None):
        bot = self.bot
        code = code.strip().upper() if code else None
        active = [r.session for key, r in bot.recordings.items() if code is None or key == code]
        pending = [s for s in bot.sessions if s.data["status"] == "pending_upload" and
                   (code is None or s.data["code"] == code)]
        messages = [bot.session_status(s) for s in active]
        if not active:
            messages.append("No matching class is recording." if code else "Ready for /classpoint start. No class is currently recording.")
        messages.append(f"Recording slots: {len(bot.recordings)}/{bot.config.max_concurrent_sessions}. Pending uploads: {len(pending)}.")
        messages.extend(bot.upload_status(s) for s in pending)
        matching = [s for s in bot.sessions if code is None or s.data["code"] == code]
        if not pending and matching and matching[-1].data["status"] == "uploaded":
            messages.append(f"Last class: {matching[-1].data['code']} - uploaded.")
        # Ten active sessions plus pending exports can exceed Discord's message limit.
        chunks = [""]
        for message in messages:
            if len(chunks[-1]) + len(message) + 2 > 1900:
                chunks.append("")
            chunks[-1] += ("\n\n" if chunks[-1] else "") + message
        await interaction.response.send_message(chunks[0], ephemeral=False)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=False)

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
        self.recordings: dict[str, Recording] = {}
        self.upload_task: asyncio.Task | None = None
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

    def session_status(self, session):
        started = datetime.fromisoformat(session.data["started_at"])
        slides = session.data["slides"]
        last = datetime.fromisoformat(slides[-1]["captured_at"]) if slides else started
        message = (f"Class {session.data['code']}: {session.data['phase']}; {len(slides)} unique slides saved.\n"
                   f"Started <t:{int(started.timestamp())}:R>. "
                   + (f"Last image saved <t:{int(last.timestamp())}:R>." if slides else "No image saved yet.")
                   + f"\nConnection: {session.data.get('connection_state', 'joining')}. Export: {session.data.get('export_mode', 'all')}.")
        if (datetime.now(timezone.utc) - last).total_seconds() >= self.config.inactivity_notice_seconds:
            message += "\nNo new image saved recently; the presenter may be paused or showing previously saved content. Recording continues."
        return message

    @staticmethod
    def upload_status(session):
        data = session.data
        message = f"Class {data['code']}: {data.get('export_phase', 'queued for PDF')}"
        if data.get("export_phase") == "building PDF":
            message += f"; {data.get('export_completed', 0)}/{data.get('export_total', len(data['slides']))} pages prepared"
        elif data.get("pdf_parts"):
            message += f"; {len(data['uploaded_parts'])}/{len(data['pdf_parts'])} parts uploaded"
        if data.get("upload_error"):
            message += "\nLast upload error: " + data["upload_error"][:300]
        return message

    async def periodic_status(self, recording):
        session, stop = recording.session, recording.stop
        while not stop.is_set():
            await pause_or_stop(stop, self.config.status_update_seconds)
            if stop.is_set() or self.recordings.get(session.data["code"]) is not recording or session.data["status"] != "recording":
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

    async def record(self, recording, interaction):
        session, stop = recording.session, recording.stop
        status_task = None

        async def joined():
            nonlocal status_task
            await self.edit_reply(interaction,
                f"Joined class {session.data['code']} as {discord.utils.escape_markdown(session.data['name'])}. "
                f"Capturing slides with {session.data.get('export_mode', 'all')} export; I'll post the PDF here when the class ends. "
                f"Use /classpoint status code:{session.data['code']} or /classpoint stop code:{session.data['code']}.")
            if status_task is None:
                status_task = asyncio.create_task(self.periodic_status(recording), name=f"classpoint-progress-{session.data['code']}")

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
            try:
                session.finish(locals().get("reason", "Recording was interrupted."))
            finally:
                if self.recordings.get(session.data["code"]) is recording:
                    del self.recordings[session.data["code"]]
                self.upload_wakeup.set()
            LOG.info("Finished class %s, session %s: %s", session.data["code"], session.data["id"], session.data["reason"])

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
                    session.data.update(upload_error=f"{type(error).__name__}: {str(error)[:300]}", export_phase="retry pending")
                    try:
                        session.save()
                    except OSError:
                        # Storage failure must not terminate the only upload worker.
                        LOG.exception("Could not persist upload error for session %s; retry worker remains active", session.data["id"])
            try:
                await asyncio.wait_for(self.upload_wakeup.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        async with self.start_lock:
            self.shutting_down = True
            recordings = list(self.recordings.values())
            for recording in recordings:
                recording.stop.set()
        tasks = [r.task for r in recordings if r.task and not r.task.done()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=50)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.upload_task:
            self.upload_task.cancel()
            await asyncio.gather(self.upload_task, return_exceptions=True)
        await super().close()
