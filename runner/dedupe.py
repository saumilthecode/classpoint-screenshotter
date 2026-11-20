"""Pixel fingerprints that account for verified PowerPoint toolbar variants."""

from functools import lru_cache
import hashlib
import io
from pathlib import Path

from PIL import Image

COMPARISON_VERSION = 2

# These on/off pairs were confirmed to be identical everywhere outside the patch.
# Only digests of the exact RGB patches are stored, including the footer beneath
# the toolbar. A change to even one pixel invalidates the match.
TOOLBAR_PAIRS = {
    (968, 544): ((235, 28), frozenset({
        "5d8d176035f34d41cb63d55d168e14d6a6c0058ee90bd43023b938ceb978656a",
        "de83b8f2586028fd8fbee81b548c80dbe0e10b4a7235688ddde1dcc104f904db",
    })),
    (858, 482): ((208, 25), frozenset({
        "156b57b5f1bb1b79447dc7ef20fa7b70a7d6955c22f6f63d088c452b69db337d",
        "9e9081e19b824dae0961d593e152a885c53b1e7f1ea29cd6a1ade2244b55073b",
    })),
}


@lru_cache(maxsize=1)
def _controls():
    with Image.open(Path(__file__).with_name("powerpoint-controls.png")) as source:
        image = source.convert("RGB")
    return image.size, image.tobytes()


def _rgb(png):
    with Image.open(io.BytesIO(png)) as source:
        return source.convert("RGB")


def _pixel_hash(image, namespace=b""):
    prefix = f"{image.width}x{image.height}:".encode()
    digest = hashlib.sha256(namespace + prefix)
    digest.update(image.tobytes())
    return digest.hexdigest()


def _comparison(image, exact):
    pair = TOOLBAR_PAIRS.get(image.size)
    if pair:
        (width, height), digests = pair
        box = (0, image.height - height, width, image.height)
        if hashlib.sha256(image.crop(box).tobytes()).hexdigest() in digests:
            image.paste((0, 0, 0), box)
            return _pixel_hash(image, namespace=b"verified-footer:")
    (width, height), pixels = _controls()
    if image.width >= width * 3 and image.height >= height * 8:
        box = (0, image.height - height, width, image.height)
        if image.crop(box).tobytes() == pixels:
            image.paste((255, 255, 255), box)
            return _pixel_hash(image)
    return exact


def pixel_hash(png):
    return _pixel_hash(_rgb(png))


def comparison_hash(png):
    return fingerprints(png)[1]


def fingerprints(png):
    """Calculate both fingerprints with one image decode."""
    image = _rgb(png)
    exact = _pixel_hash(image)
    return exact, _comparison(image, exact)
