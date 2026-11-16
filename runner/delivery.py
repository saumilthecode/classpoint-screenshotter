import asyncio
from dataclasses import replace
from datetime import datetime
import logging
import uuid

import discord

from .pdf import ExportPlan, ExportResult, PDFPart, build_export, commit_export
from .state import Session, utc_now

LOG = logging.getLogger(__name__)


async def recent_messages(channel, started: str):
    # after= defaults to oldest-first in discord.py; explicitly request recent messages.
    return [message async for message in channel.history(
        limit=200, after=datetime.fromisoformat(started), oldest_first=False)]


async def existing_message(channel, bot_id: int, marker: str, filename: str | None, started: str, messages=None):
    """Reconcile an acknowledged upload after a crash before its manifest was saved."""
    if messages is None:
        messages = await recent_messages(channel, started)
    for message in messages:
        if message.author.id == bot_id and marker in message.content:
            if filename is None or any(attachment.filename == filename for attachment in message.attachments):
                return message.id
    return None


async def prepare_export(session, limit):
    """One worker at a time via upload_loop; the event loop owns all session state."""
    plan = ExportPlan.from_session(session)
    # Every attempt gets immutable filenames, including repeated rebuilds at the same limit.
    plan = replace(plan, session_id=f"{plan.session_id}-export-{uuid.uuid4().hex[:8]}")
    # Already published parts stay immutable if an attachment limit changes.
    published = []
    published_numbers = set()
    for part in session.data.get("pdf_parts", []):
        if part["file"] in session.data["uploaded_parts"]:
            numbers = tuple(part.get("slide_numbers", range(part["first_slide"], part["last_slide"] + 1)))
            published.append(PDFPart(session.directory / "pdfs" / part["file"], numbers, dict(part)))
            published_numbers.update(numbers)
    if published:
        plan = replace(plan, captures=tuple(c for c in plan.captures if c.number not in published_numbers),
                       # Preserve the original selection when rebuilding a sampled export.
                       mode="all")
        selected = session.data.get("pdf_slide_numbers")
        if selected is not None:
            plan = replace(plan, captures=tuple(c for c in plan.captures if c.number in selected))
    session.data.update(export_phase="building PDF", export_completed=0, export_total=len(plan.captures))
    session.save()
    loop = asyncio.get_running_loop()

    def update(completed, total):
        session.data.update(export_completed=completed, export_total=total)

    def progress(completed, total):
        loop.call_soon_threadsafe(update, completed, total)

    worker = asyncio.create_task(asyncio.to_thread(build_export, plan, session.directory / "pdfs", limit, progress))
    cancelled = False
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling to_thread doesn't stop its thread. Join it before committing or exiting.
        cancelled = True
        try:
            result = await worker
        except Exception:
            LOG.exception("PDF worker failed while shutting down session %s", session.data["id"])
            raise asyncio.CancelledError from None
    try:
        if published:
            parts = tuple(sorted((*published, *result.parts), key=lambda part: part.slide_numbers[0]))
            result = ExportResult(parts, tuple(number for part in parts for number in part.slide_numbers),
                                  session.data.get("pdf_export_mode", session.data.get("export_mode", "all")),
                                  result.elapsed_seconds)
        commit_export(session, result)
    except Exception:
        if not cancelled:
            raise
        LOG.exception("Could not commit PDF export while shutting down session %s", session.data["id"])
    finally:
        if cancelled:
            raise asyncio.CancelledError



async def deliver_session(session: Session, channel, bot_id: int):
    if session.data["status"] != "pending_upload":
        return
    count = len(session.images)
    marker = f"Session `{session.data['id']}`"
    # One history fetch per attempt, shared by every PDF part.
    messages = await recent_messages(channel, session.data["started_at"])
    if not count:
        description = f"ClassPoint {session.data['code']} - no slides.\n{session.data['reason']}"
        empty_marker = marker + " - no slides"
        found = await existing_message(channel, bot_id, empty_marker, None, session.data["started_at"], messages)
        if not found:
            message = await channel.send(description + "\nNo PDF was created because no slides were visible.\n" + empty_marker,
                                         allowed_mentions=discord.AllowedMentions.none())
            found = message.id
        session.data["result_message_id"] = found
    else:
        limit = int(channel.guild.filesize_limit * 0.95)
        existing = session.data.get("pdf_parts", [])
        # Reconcile before rebuilding so acknowledged parts retain their contents and names.
        for part in existing:
            if part["file"] not in session.data["uploaded_parts"]:
                found = await existing_message(channel, bot_id, marker, part["file"], session.data["started_at"], messages)
                if found:
                    session.data["uploaded_parts"][part["file"]] = found
        pending = [part for part in existing if part["file"] not in session.data["uploaded_parts"]]
        paths = [session.directory / "pdfs" / part["file"] for part in pending]
        if not existing or any(not path.exists() or path.stat().st_size > limit for path in paths):
            await prepare_export(session, limit)
        session.data["export_phase"] = "uploading PDFs"
        session.save()
        parts = session.data["pdf_parts"]
        pages = session.data.get("pdf_page_count", count)
        description = (f"ClassPoint {session.data['code']} - {count} unique slide{'s' if count != 1 else ''} captured; "
                       f"{pages} PDF page{'s' if pages != 1 else ''}.\n{session.data['reason']}")
        if session.data.get("pdf_export_mode") == "sampled":
            description += "\nRapid scene changes were sampled in this PDF; all original PNGs are retained."
        for index, part in enumerate(parts, 1):
            filename = part["file"]
            if filename in session.data["uploaded_parts"]:
                continue
            path = session.directory / "pdfs" / filename
            message_id = await existing_message(channel, bot_id, marker, filename, session.data["started_at"], messages)
            if not message_id:
                label = f"Captures {part['first_slide']}-{part['last_slide']}"
                if "page_count" in part:
                    label += f" ({part['page_count']} pages)"
                content = description + f"\n{label}.\n{marker} - PDF {index}/{len(parts)}"
                attachment = discord.File(path, filename=filename)
                try:
                    message = await channel.send(content, file=attachment, allowed_mentions=discord.AllowedMentions.none())
                finally:
                    attachment.close()
                message_id = message.id
            session.data["uploaded_parts"][filename] = message_id
            session.save()
    session.data.update(status="uploaded", uploaded_at=utc_now(), export_phase="uploaded")
    session.data.pop("upload_error", None)
    session.save()
