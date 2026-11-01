"""Exact slide fingerprints with a check for a known slideshow control strip."""

from functools import lru_cache
import hashlib
import io
from pathlib import Path

from PIL import Image

COMPARISON_VERSION = 1


@lru_cache(maxsize=1)
def _controls():
    with Image.open(Path(__file__).with_name("powerpoint-controls.png")) as source:
        image = source.convert("RGB")
    return image.size, image.tobytes()


def _hash(png, ignore_controls):
    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
    if ignore_controls:
        (width, height), pixels = _controls()
        if image.width >= width * 3 and image.height >= height * 8:
            box = (0, image.height - height, width, image.height)
            # Match the whole patch, so even a one-pixel addition keeps the slide.
            if image.crop(box).tobytes() == pixels:
                image.paste((255, 255, 255), box)
    return hashlib.sha256(f"{image.width}x{image.height}:".encode() + image.tobytes()).hexdigest()


def pixel_hash(png):
    return _hash(png, False)


def comparison_hash(png):
    return _hash(png, True)
