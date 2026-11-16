from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import uuid

from .dedupe import COMPARISON_VERSION, comparison_hash, pixel_hash


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, contents: bytes):
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_bytes(contents)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Session:
    def __init__(self, directory: Path, data: dict):
        self.directory = directory
        self.data = data
        self.hashes = {s["hash"] for s in data.get("slides", [])}
        self._comparison_hashes = None

    @property
    def comparison_hashes(self):
        if self._comparison_hashes is None:
            self._comparison_hashes = set()
            for slide in self.data.get("slides", []):
                if slide.get("comparison_version") == COMPARISON_VERSION:
                    self._comparison_hashes.add(slide["comparison_hash"])
                else:
                    try:
                        png = (self.directory / "images" / slide["file"]).read_bytes()
                        self._comparison_hashes.add(comparison_hash(png))
                    except OSError:
                        # Exact hashes still work if an old image is missing or unreadable.
                        continue
        return self._comparison_hashes

    def is_duplicate(self, png: bytes, comparison: str):
        return comparison in self.comparison_hashes or pixel_hash(png) in self.hashes

    @classmethod
    def create(cls, root: Path, code: str, name: str, requester_id: int, channel_id: int, export_mode: str = "all"):
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        directory = root / f"{code}-{session_id}"
        (directory / "images").mkdir(parents=True, mode=0o700)
        session = cls(directory, {
            "id": session_id, "code": code, "name": name,
            "requester_id": requester_id, "channel_id": channel_id,
            "started_at": utc_now(), "status": "recording", "phase": "joining",
            "connection_state": "joining", "export_mode": export_mode,
            "slides": [], "uploaded_parts": {}, "pdf_parts": [],
        })
        session.save()
        return session

    @classmethod
    def load(cls, path: Path):
        return cls(path.parent, json.loads(path.read_text()))

    def save(self):
        atomic_write(self.directory / "manifest.json", json.dumps(self.data, indent=2).encode())

    def add_slide(self, png: bytes, digest: str | None = None, comparison: str | None = None):
        digest = digest or pixel_hash(png)
        if digest in self.hashes:
            return False
        comparison = comparison or comparison_hash(png)
        if comparison in self.comparison_hashes:
            return False
        filename = f"slide-{len(self.data['slides']) + 1:04d}.png"
        atomic_write(self.directory / "images" / filename, png)
        self.data["slides"].append({"file": filename, "hash": digest,
                                    "comparison_hash": comparison,
                                    "comparison_version": COMPARISON_VERSION,
                                    "captured_at": utc_now()})
        self.hashes.add(digest)
        self.comparison_hashes.add(comparison)
        self.save()
        return True

    def finish(self, reason: str):
        self.data.update(status="pending_upload", phase="finished", reason=reason, ended_at=utc_now(),
                         connection_state="finished", export_phase="queued for PDF")
        self.save()

    @property
    def images(self):
        return [self.directory / "images" / slide["file"] for slide in self.data["slides"]]


def recover_sessions(root: Path):
    """Queue interrupted recordings for upload."""
    sessions = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            session = Session.load(path)
            started = datetime.fromisoformat(session.data["started_at"])
            if started.tzinfo is None:
                raise ValueError("Session start time must include a timezone")
            if session.data["status"] == "recording":
                session.finish("Runner restarted; these are the slides captured before interruption.")
            sessions.append(session)
        except (ValueError, KeyError, OSError):
            # Leave corrupt data untouched for manual recovery.
            logging.exception("Could not recover session manifest %s", path)
    # Directory names begin with class codes; their order is not chronological.
    sessions.sort(key=lambda session: (datetime.fromisoformat(session.data["started_at"]), session.data.get("id", "")))
    return sessions
