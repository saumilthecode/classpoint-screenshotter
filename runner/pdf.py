"""Build slide PDFs within the channel's attachment limit."""

import io
from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .state import Session, atomic_write


def render_pdf(images: list[Path], title: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pageCompression=1, invariant=1)
    document.setTitle(title)
    document.setAuthor("ClassPoint Screenshotter")
    for path in images:
        with Image.open(path) as source:
            image = source.convert("RGB")
            # A fixed 960-point width preserves each slide's aspect ratio.
            width, height = 960, 960 * image.height / image.width
            document.setPageSize((width, height))
            document.drawImage(ImageReader(image), 0, 0, width=width, height=height)
        document.showPage()
    document.save()
    return output.getvalue()


def build_pdfs(session: Session, max_bytes: int) -> list[Path]:
    if max_bytes < 2048:
        raise ValueError("Attachment limit is too small")
    title = f"ClassPoint {session.data['code']} - {session.data['started_at']}"
    images = session.images
    if not images:
        return []
    output = session.directory / "pdfs"
    output.mkdir(exist_ok=True)
    # Split by encoded PDF size, which can differ from the source PNG sizes.
    parts: list[tuple[bytes, int, int]] = []

    def split(first, last):
        blob = render_pdf(images[first:last], title)
        if len(blob) <= max_bytes:
            parts.append((blob, first + 1, last))
        elif last - first == 1:
            raise ValueError(f"Slide {first + 1} exceeds the upload limit; original PNG is retained.")
        else:
            middle = (first + last) // 2
            split(first, middle)
            split(middle, last)

    split(0, len(images))
    result = []
    for index, (blob, first, last) in enumerate(parts, 1):
        filename = f"classpoint-{session.data['code']}-{session.data['id']}-part-{index:02d}.pdf"
        path = output / filename
        atomic_write(path, blob)
        result.append(path)
    session.data["pdf_parts"] = [
        {"file": path.name, "first_slide": part[1], "last_slide": part[2]}
        for path, part in zip(result, parts)
    ]
    session.save()
    return result
