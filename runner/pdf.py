"""Lossless PDFs: encode each captured image once, then pack cached pages."""

from dataclasses import dataclass, replace
from datetime import datetime
import io
from pathlib import Path
import time
import tempfile
import uuid
from typing import Callable

from PIL import Image, ImageChops
from pypdf import PdfReader, PdfWriter
from reportlab import rl_config
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .state import Session, atomic_write

# PDF supports binary Flate streams directly. ASCII85 only adds size and CPU work.
rl_config.useA85 = False


@dataclass(frozen=True)
class Capture:
    number: int
    path: Path
    captured_at: str


@dataclass(frozen=True)
class ExportPlan:
    code: str
    session_id: str
    started_at: str
    captures: tuple[Capture, ...]
    mode: str = "all"

    @classmethod
    def from_session(cls, session: Session, mode: str | None = None):
        data = session.data
        return cls(data["code"], data["id"], data["started_at"],
                   tuple(Capture(i, session.directory / "images" / slide["file"], slide["captured_at"])
                         for i, slide in enumerate(data["slides"], 1)),
                   mode or data.get("export_mode", "all"))


@dataclass(frozen=True)
class PDFPart:
    path: Path
    slide_numbers: tuple[int, ...]
    saved_metadata: dict | None = None

    def manifest(self):
        if self.saved_metadata is not None:
            return dict(self.saved_metadata)
        return {"file": self.path.name, "first_slide": self.slide_numbers[0],
                "last_slide": self.slide_numbers[-1], "page_count": len(self.slide_numbers),
                "slide_numbers": list(self.slide_numbers), "bytes": self.path.stat().st_size}


@dataclass(frozen=True)
class ExportResult:
    parts: tuple[PDFPart, ...]
    slide_numbers: tuple[int, ...]
    mode: str
    elapsed_seconds: float


def select_captures(plan: ExportPlan) -> tuple[Capture, ...]:
    """Opt-in sampling of long runs of rapid, large visual changes.

    This is a rate/visual-change policy, not a video classifier. A run needs
    large changes in at least 40% of its transitions; small edits alone do not
    qualify. Every PNG remains available regardless of PDF selection.
    """
    if plan.mode == "all":
        return plan.captures
    if plan.mode != "sampled":
        raise ValueError("Export mode must be 'all' or 'sampled'")
    keep = set(range(len(plan.captures)))
    run_start = 0
    large_changes = 0
    previous = None
    previous_time = None

    def finish_run(last):
        length = last - run_start + 1
        if length < 8 or large_changes < max(2, (length - 1) * 0.4):
            return
        last_kept = datetime.fromisoformat(plan.captures[run_start].captured_at)
        for index in range(run_start + 1, last):
            current = datetime.fromisoformat(plan.captures[index].captured_at)
            if (current - last_kept).total_seconds() >= 30:
                last_kept = current
            else:
                keep.discard(index)

    for index, capture in enumerate(plan.captures):
        with Image.open(capture.path) as source:
            thumbnail = source.convert("RGB").resize((64, 36))
        current_time = datetime.fromisoformat(capture.captured_at)
        if previous is not None:
            gap = (current_time - previous_time).total_seconds()
            if 0 < gap <= 6:
                pixels = ImageChops.difference(previous, thumbnail).get_flattened_data()
                changed = sum(max(pixel) > 32 for pixel in pixels)
                large_changes += changed / (64 * 36) >= 0.25
            else:
                finish_run(index - 1)
                run_start, large_changes = index, 0
        previous, previous_time = thumbnail, current_time
    finish_run(len(plan.captures) - 1)
    return tuple(c for index, c in enumerate(plan.captures) if index in keep)


def render_pdf(images: list[Path], title: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pageCompression=1, invariant=1)
    document.setTitle(title)
    document.setAuthor("ClassPoint Screenshotter")
    for path in images:
        with Image.open(path) as source:
            image = source.convert("RGB")
            width, height = 960, 960 * image.height / image.width
            document.setPageSize((width, height))
            document.drawImage(ImageReader(image), 0, 0, width=width, height=height)
        document.showPage()
    document.save()
    return output.getvalue()


def _pack(pages: list[tuple[Capture, bytes]], title: str) -> bytes:
    with PdfWriter() as writer:
        writer.add_metadata({"/Title": title, "/Author": "ClassPoint Screenshotter"})
        for _capture, page in pages:
            writer.append(PdfReader(io.BytesIO(page)), import_outline=False)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()


def build_export(plan: ExportPlan, output: Path, max_bytes: int,
                 progress: Callable[[int, int], None] | None = None) -> ExportResult:
    """Worker-safe: reads an immutable plan, writes PDFs, never mutates a Session.

    Cache at most roughly one attachment's worth of one-page PDFs. Group using
    their actual encoded sizes, then enforce the final serialized size exactly.
    Any fallback splitting reuses page bytes instead of recompressing pixels.
    """
    if max_bytes < 2048:
        raise ValueError("Attachment limit is too small")
    started = time.monotonic()
    captures = select_captures(plan)
    title = f"ClassPoint {plan.code} - {plan.started_at}"
    output.mkdir(parents=True, exist_ok=True)
    parts = []
    promoted = []
    # Publish only after every page is valid. A failed attempt leaves no prefix
    # of orphan PDFs for the minute-by-minute retry loop to duplicate.
    with tempfile.TemporaryDirectory(prefix=".building-", dir=output) as staging:
        stage = Path(staging)
        pages = []
        budget = 1024

        def emit(group):
            blob = _pack(group, title)
            if len(blob) > max_bytes:
                if len(group) == 1:
                    raise ValueError(f"Slide {group[0][0].number} exceeds the upload limit; original PNG is retained.")
                middle = len(group) // 2
                emit(group[:middle])
                emit(group[middle:])
                return
            filename = f"classpoint-{plan.code}-{plan.session_id}-part-{len(parts) + 1:02d}.pdf"
            atomic_write(stage / filename, blob)
            parts.append(PDFPart(output / filename, tuple(capture.number for capture, _blob in group)))

        if progress:
            progress(0, len(captures))
        for index, capture in enumerate(captures, 1):
            page = render_pdf([capture.path], title)
            if pages and budget + len(page) > max_bytes:
                emit(pages)
                pages, budget = [], 1024
            pages.append((capture, page))
            budget += len(page)
            if progress:
                progress(index, len(captures))
        if pages:
            emit(pages)
        try:
            for part in parts:
                if part.path.exists():
                    raise FileExistsError(f"Refusing to replace an existing PDF: {part.path}")
                (stage / part.path.name).replace(part.path)
                promoted.append(part.path)
        except BaseException:
            for path in promoted:
                path.unlink(missing_ok=True)
            raise
    return ExportResult(tuple(parts), tuple(c.number for c in captures), plan.mode,
                        round(time.monotonic() - started, 3))


def commit_export(session: Session, result: ExportResult):
    """Commit on the owning event loop after the worker has finished."""
    previous = dict(session.data)
    try:
        session.data.update(pdf_parts=[part.manifest() for part in result.parts],
                            pdf_export_mode=result.mode, pdf_slide_numbers=list(result.slide_numbers),
                            pdf_page_count=len(result.slide_numbers), export_seconds=result.elapsed_seconds,
                            export_phase="uploading PDFs")
        session.save()
    except BaseException:
        session.data.clear()
        session.data.update(previous)
        for part in result.parts:
            if part.saved_metadata is None:
                part.path.unlink(missing_ok=True)
        raise


def build_pdfs(session: Session, max_bytes: int) -> list[Path]:
    """Synchronous convenience API for local callers."""
    plan = ExportPlan.from_session(session)
    plan = replace(plan, session_id=f"{plan.session_id}-export-{uuid.uuid4().hex[:8]}")
    result = build_export(plan, session.directory / "pdfs", max_bytes)
    commit_export(session, result)
    return [part.path for part in result.parts]
