from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import uuid

from PIL import Image


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, contents: bytes):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(contents)
    temporary.replace(path)


def pixel_hash(png: bytes):
    with Image.open(io.BytesIO(png)) as image:
        image = image.convert("RGB")
        return hashlib.sha256(f"{image.width}x{image.height}:".encode() + image.tobytes()).hexdigest()


class Session:
    def __init__(self, directory: Path, data: dict):
        self.directory = directory
        self.data = data
        self.hashes = {s["hash"] for s in data.get("slides", [])}

    @classmethod
    def create(cls, root: Path, code: str, name: str, requester_id: int, channel_id: int):
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        directory = root / f"{code}-{session_id}"
        (directory / "images").mkdir(parents=True, mode=0o700)
        session = cls(directory, {
            "id": session_id, "code": code, "name": name,
            "requester_id": requester_id, "channel_id": channel_id,
            "started_at": utc_now(), "status": "recording", "phase": "joining",
            "slides": [], "uploaded_parts": {}, "pdf_parts": [],
        })
        session.save()
        return session

    @classmethod
    def load(cls, path: Path):
        return cls(path.parent, json.loads(path.read_text()))

    def save(self):
        atomic_write(self.directory / "manifest.json", json.dumps(self.data, indent=2).encode())

    def add_slide(self, png: bytes):
        digest = pixel_hash(png)
        if digest in self.hashes:
            return False
        filename = f"slide-{len(self.data['slides']) + 1:04d}.png"
        atomic_write(self.directory / "images" / filename, png)
        self.data["slides"].append({"file": filename, "hash": digest, "captured_at": utc_now()})
        self.hashes.add(digest)
        self.save()
        return True

    def finish(self, reason: str):
        self.data.update(status="pending_upload", phase="finished", reason=reason, ended_at=utc_now())
        self.save()

    @property
    def images(self):
        return [self.directory / "images" / slide["file"] for slide in self.data["slides"]]


def recover_sessions(root: Path):
    """Preserve and publish partial recordings after a crash; never silently lose them."""
    sessions = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            session = Session.load(path)
            if session.data["status"] == "recording":
                session.finish("Runner restarted; these are the slides captured before interruption.")
            sessions.append(session)
        except (ValueError, KeyError, OSError):
            # Leave corrupt data untouched for manual recovery.
            import logging
            logging.exception("Could not recover session manifest %s", path)
    return sessions
