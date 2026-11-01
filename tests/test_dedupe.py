import base64
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw

from runner.capture import SlideRecorder
from runner.config import Config
from runner.dedupe import COMPARISON_VERSION, comparison_hash, pixel_hash
from runner.state import Session

CONTROLS = Path(__file__).parents[1] / "runner/powerpoint-controls.png"


def encode(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def slide(controls=False):
    image = Image.new("RGB", (968, 544), "white")
    drawing = ImageDraw.Draw(image)
    drawing.text((300, 80), "A slide with text and a diagram", fill="black")
    drawing.rectangle((350, 150, 600, 350), fill="navy")
    if controls:
        with Image.open(CONTROLS) as toolbar:
            image.paste(toolbar, (0, image.height - toolbar.height))
    return image


class DedupeTest(unittest.TestCase):
    def test_only_known_controls_are_ignored(self):
        plain, controls = encode(slide()), encode(slide(True))
        self.assertNotEqual(pixel_hash(plain), pixel_hash(controls))
        self.assertEqual(comparison_hash(plain), comparison_hash(controls))

    def test_one_pixel_changes_survive_inside_and_outside_controls(self):
        for controls in (False, True):
            original = slide(controls)
            for point in ((14, 530), (30, 530), (0, 517), (231, 530), (600, 100)):
                with self.subTest(controls=controls, point=point):
                    edited = original.copy()
                    color = edited.getpixel(point)
                    edited.putpixel(point, tuple(channel - 1 if channel else 1 for channel in color))
                    self.assertNotEqual(comparison_hash(encode(original)),
                                        comparison_hash(encode(edited)))

    def test_different_controls_and_footer_diagrams_are_kept(self):
        plain = slide()
        circles = plain.copy()
        drawing = ImageDraw.Draw(circles)
        for index in range(8):
            x = 3 + index * 29
            drawing.ellipse((x, 519, x + 22, 541), outline=(225, 225, 225))
        shifted = plain.copy()
        with Image.open(CONTROLS) as toolbar:
            shifted.paste(toolbar, (1, shifted.height - toolbar.height))
        for image in (circles, shifted, plain.resize((967, 544))):
            with self.subTest(size=image.size):
                self.assertNotEqual(comparison_hash(encode(plain)), comparison_hash(encode(image)))

    def test_session_keeps_original_png_and_exact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp), "TEST", "Student", 1, 2)
            original = encode(slide(True))
            self.assertTrue(session.add_slide(original))
            self.assertFalse(session.add_slide(encode(slide())))
            self.assertEqual(session.images[0].read_bytes(), original)
            self.assertEqual(session.data["slides"][0]["hash"], pixel_hash(original))
            self.assertEqual(session.data["slides"][0]["comparison_version"], COMPARISON_VERSION)

    def test_legacy_and_stale_indexes_rebuild_on_demand(self):
        for version in (None, COMPARISON_VERSION - 1):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                session = Session.create(Path(tmp), "TEST", "Student", 1, 2)
                session.add_slide(encode(slide(True)))
                entry = session.data["slides"][0]
                entry.pop("comparison_version")
                entry.pop("comparison_hash")
                if version is not None:
                    entry.update(comparison_version=version, comparison_hash="stale")
                session.save()
                recovered = Session.load(session.directory / "manifest.json")
                self.assertIsNone(recovered._comparison_hashes)
                self.assertFalse(recovered.add_slide(encode(slide())))
                self.assertEqual(len(recovered.images), 1)

    def test_missing_or_unreadable_legacy_image_keeps_exact_dedupe(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                session = Session.create(Path(tmp), "TEST", "Student", 1, 2)
                original = encode(slide(True))
                session.add_slide(original)
                entry = session.data["slides"][0]
                entry.pop("comparison_version")
                entry.pop("comparison_hash")
                session.save()
                if missing:
                    session.images[0].unlink()
                else:
                    session.images[0].write_bytes(b"unreadable image")
                recovered = Session.load(session.directory / "manifest.json")
                self.assertTrue(recovered.is_duplicate(original, comparison_hash(original)))
                self.assertFalse(recovered.add_slide(original))

    def test_saved_comparison_index_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp), "TEST", "Student", 1, 2)
            session.add_slide(encode(slide(True)))
            recovered = Session.load(session.directory / "manifest.json")
            self.assertFalse(recovered.add_slide(encode(slide())))


class CaptureDedupeTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_toggle_settles_and_stays_duplicate_at_slide_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = Session.create(root, "TEST", "Student", 1, 2)
            config = Config(application_id=1, guild_id=2, data_dir=tmp, token_file="unused",
                            max_slides=1, settle_seconds=0.6)
            recorder = SlideRecorder(config, session)
            state = dict(slide={"src": "fixture", "clip": {}, "revision": 0},
                         ended=False, disconnected=False)
            page = SimpleNamespace(evaluate=AsyncMock(return_value=state))
            cdp = SimpleNamespace(send=AsyncMock())

            def screenshot(image):
                cdp.send.return_value = {"data": base64.b64encode(encode(image)).decode()}

            screenshot(slide())
            self.assertFalse(await recorder.capture_once(page, cdp, 0))
            screenshot(slide(True))
            self.assertTrue(await recorder.capture_once(page, cdp, 0.7))
            screenshot(slide())
            self.assertFalse(await recorder.capture_once(page, cdp, 2))

            edited = slide()
            edited.putpixel((600, 100), (0, 0, 0))
            screenshot(edited)
            self.assertFalse(await recorder.capture_once(page, cdp, 3))
            with self.assertRaisesRegex(RuntimeError, "Slide safety limit"):
                await recorder.capture_once(page, cdp, 4)
            self.assertEqual(len(session.images), 1)
