import asyncio
from datetime import datetime

import discord

from .pdf import build_pdfs
from .state import Session, utc_now


async def existing_message(channel, bot_id: int, marker: str, filename: str | None, started: str):
    """Reconcile an acknowledged upload after a crash before its manifest was saved."""
    async for message in channel.history(limit=200, after=datetime.fromisoformat(started)):
        if message.author.id == bot_id and marker in message.content:
            if filename is None or any(attachment.filename == filename for attachment in message.attachments):
                return message.id
    return None


async def deliver_session(session: Session, channel, bot_id: int):
    if session.data["status"] != "pending_upload":
        return
    count = len(session.images)
    description = (f"ClassPoint {session.data['code']} - {count} unique slide{'s' if count != 1 else ''}.\n"
                   f"{session.data['reason']}")
    if not count:
        marker = f"Session `{session.data['id']}` - no slides"
        found = await existing_message(channel, bot_id, marker, None, session.data["started_at"])
        if not found:
            message = await channel.send(description + "\nNo PDF was created because no slides were visible.\n" + marker,
                                         allowed_mentions=discord.AllowedMentions.none())
            found = message.id
        session.data["result_message_id"] = found
    else:
        existing = session.data.get("pdf_parts", [])
        paths = [session.directory / "pdfs" / part["file"] for part in existing]
        if not paths or not all(path.exists() for path in paths):
            # PDFs are sent as channel messages, using the guild's upload limit.
            limit = int(channel.guild.filesize_limit * 0.95)
            paths = await asyncio.to_thread(build_pdfs, session, limit)
        for index, path in enumerate(paths, 1):
            if path.name in session.data["uploaded_parts"]:
                continue
            marker = f"Session `{session.data['id']}` - PDF {index}/{len(paths)}"
            message_id = await existing_message(channel, bot_id, marker, path.name, session.data["started_at"])
            if not message_id:
                part = session.data["pdf_parts"][index - 1]
                content = (description + f"\nSlides {part['first_slide']}-{part['last_slide']}.\n" + marker)
                attachment = discord.File(path, filename=path.name)
                try:
                    message = await channel.send(content, file=attachment, allowed_mentions=discord.AllowedMentions.none())
                finally:
                    attachment.close()
                message_id = message.id
            session.data["uploaded_parts"][path.name] = message_id
            session.save()
    session.data.update(status="uploaded", uploaded_at=utc_now())
    session.data.pop("upload_error", None)
    session.save()
