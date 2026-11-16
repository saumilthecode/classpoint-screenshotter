"""Export fidelity, sampling, bounded packing, and interruption recovery."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from pypdf import PdfReader

from runner.delivery import deliver_session, prepare_export
from runner.pdf import ExportPlan, build_export, build_pdfs, render_pdf, select_captures
from runner.state import Session


def png(color, size=(320, 180), annotation=None):
    image = Image.new("RGB", size, color)
    if annotation:
        ImageDraw.Draw(image).rectangle(annotation, fill="magenta")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = Session.create(self.root, "12345", "Student", 10, 20)

    def tearDown(self):
        self.tmp.cleanup()

    def rapid_frames(self, small=False):
        started = datetime(2026, 11, 16, tzinfo=timezone.utc)
        for index in range(15):
            color = "white" if small else ((200 + index, 0, 0) if index % 2 else (0, 0, 200 + index))
            self.assertTrue(self.session.add_slide(png(color, annotation=(index * 4, 10, index * 4 + 2, 12) if small else None)))
            self.session.data["slides"][-1]["captured_at"] = (started + timedelta(seconds=index * 3)).isoformat()
        self.session.save()

    def test_large_change_sampling_preserves_boundaries_and_all_originals(self):
        self.rapid_frames()
        all_plan = ExportPlan.from_session(self.session)
        self.assertEqual(len(select_captures(all_plan)), 15)
        plan = ExportPlan.from_session(self.session, "sampled")
        self.assertEqual([capture.number for capture in select_captures(plan)], [1, 11, 15])
        before = (self.session.directory / "manifest.json").read_bytes()
        result = build_export(plan, self.root / "export", 100_000)
        self.assertEqual(result.slide_numbers, (1, 11, 15))
        self.assertEqual(len(self.session.images), 15)
        self.assertEqual((self.session.directory / "manifest.json").read_bytes(), before)
        self.assertEqual(sum(len(PdfReader(part.path).pages) for part in result.parts), 3)

    def test_sampling_preserves_small_annotations(self):
        self.rapid_frames(small=True)
        self.assertEqual(len(select_captures(ExportPlan.from_session(self.session, "sampled"))), 15)

    def test_sampling_retains_short_runs_and_slow_changes(self):
        self.rapid_frames()
        for slide in self.session.data["slides"]:
            slide["captured_at"] = (datetime(2026, 11, 16, tzinfo=timezone.utc) +
                                    timedelta(seconds=slide_number(slide) * 10)).isoformat()
        self.assertEqual(len(select_captures(ExportPlan.from_session(self.session, "sampled"))), 15)
        self.session.data["slides"] = self.session.data["slides"][:7]
        self.assertEqual(len(select_captures(ExportPlan.from_session(self.session, "sampled"))), 7)

    def test_each_image_encoded_once_and_final_size_and_pixels_preserved(self):
        for index in range(10):
            image = Image.effect_noise((200, 100), 80).convert("RGB")
            image.putpixel((0, 0), (index, 10, 20))
            output = io.BytesIO()
            image.save(output, "PNG")
            self.session.add_slide(output.getvalue())
        one_size = len(render_pdf([self.session.images[0]], "Test"))
        limit = one_size * 3
        with patch("runner.pdf.render_pdf", wraps=render_pdf) as render:
            paths = build_pdfs(self.session, limit)
        self.assertEqual(render.call_count, 10)
        self.assertTrue(all(path.stat().st_size <= limit for path in paths))
        pages = [page for path in paths for page in PdfReader(path).pages]
        self.assertEqual(len(pages), 10)
        self.assertEqual(self.session.data["pdf_slide_numbers"], list(range(1, 11)))
        for index, page in enumerate(pages):
            with Image.open(self.session.images[index]) as original:
                self.assertEqual(page.images[0].image.convert("RGB").tobytes(), original.convert("RGB").tobytes())
            filters = page.images[0].indirect_reference.get_object()["/Filter"]
            self.assertNotIn("/ASCII85Decode", filters)

    def test_single_oversized_page_fails_without_changing_manifest(self):
        image = Image.effect_noise((320, 180), 80).convert("RGB")
        output = io.BytesIO()
        image.save(output, "PNG")
        self.session.add_slide(output.getvalue())
        before = (self.session.directory / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "exceeds the upload limit"):
            build_pdfs(self.session, 2048)
        self.assertEqual((self.session.directory / "manifest.json").read_bytes(), before)

    def test_late_failure_cleans_staged_parts_and_preserves_previous_export(self):
        for index in range(6):
            self.session.add_slide(png((index * 20, 10, 20)))
        output = self.root / "export"
        output.mkdir()
        sentinel = output / "previous.pdf"
        sentinel.write_bytes(b"existing export")
        original = render_pdf
        calls = 0
        def corrupt_later(*args):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError("Corrupt fifth image")
            return original(*args)
        with patch("runner.pdf.render_pdf", side_effect=corrupt_later), self.assertRaises(OSError):
            build_export(ExportPlan.from_session(self.session), output, 3500)
        self.assertEqual(list(output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"existing export")


def slide_number(slide):
    return int(slide["file"].removeprefix("slide-").removesuffix(".png"))


class Channel:
    def __init__(self, limit=100_000):
        self.guild = SimpleNamespace(filesize_limit=limit)
        self.messages = []
        self.history_calls = []

    async def history(self, **kwargs):
        self.history_calls.append(kwargs)
        messages = self.messages if kwargs.get("oldest_first", True) else list(reversed(self.messages))
        for message in messages[:kwargs["limit"]]:
            yield message

    async def send(self, content, **kwargs):
        attachment = kwargs.get("file")
        message = SimpleNamespace(id=len(self.messages) + 100, author=SimpleNamespace(id=1), content=content,
                                  attachments=[SimpleNamespace(filename=attachment.filename)] if attachment else [])
        self.messages.append(message)
        return message


class ExportRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = Session.create(self.root, "12345", "Student", 10, 20)
        for index in range(8):
            image = Image.effect_noise((200, 100), 80).convert("RGB")
            image.putpixel((0, 0), (index, 10, 20))
            output = io.BytesIO()
            image.save(output, "PNG")
            self.session.add_slide(output.getvalue())
        self.session.finish("Class ended")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_reconciliation_uses_recent_messages_once_and_skips_existing_upload(self):
        channel = Channel()
        for _ in range(210):
            await channel.send("Recording update")
        await deliver_session(self.session, channel, 1)
        count = len(channel.messages)
        self.session.data.update(status="pending_upload", uploaded_parts={})
        channel.history_calls.clear()
        await deliver_session(self.session, channel, 1)
        self.assertEqual(len(channel.messages), count)
        self.assertEqual(len(channel.history_calls), 1)
        self.assertFalse(channel.history_calls[0]["oldest_first"])

    async def test_rebuild_at_same_limit_retains_published_files_and_missing_published_metadata(self):
        limit = len(render_pdf([self.session.images[0]], "Test")) * 2
        await prepare_export(self.session, limit)
        first = self.session.data["pdf_parts"][0]
        first_path = self.session.directory / "pdfs" / first["file"]
        before = first_path.read_bytes()
        self.session.data["uploaded_parts"][first["file"]] = 100
        await prepare_export(self.session, limit)
        self.assertEqual(first_path.read_bytes(), before)
        second = self.session.data["pdf_parts"][1]
        second_path = self.session.directory / "pdfs" / second["file"]
        second_before = second_path.read_bytes()
        self.session.data["uploaded_parts"][second["file"]] = 101
        first_path.unlink()
        await prepare_export(self.session, limit)
        self.assertEqual(second_path.read_bytes(), second_before)
        self.assertEqual(self.session.data["pdf_parts"][0], first)
        self.assertEqual(self.session.data["pdf_parts"][1], second)
        self.assertEqual(self.session.data["pdf_slide_numbers"], list(range(1, 9)))
        self.assertEqual(len({part["file"] for part in self.session.data["pdf_parts"]}), len(self.session.data["pdf_parts"]))

    async def test_rebuild_for_smaller_limit_preserves_uploaded_pages(self):
        old_limit = len(render_pdf([self.session.images[0]], "Test")) * 3
        await prepare_export(self.session, old_limit)
        first = self.session.data["pdf_parts"][0]
        self.session.data["uploaded_parts"][first["file"]] = 999
        channel = Channel(limit=int(old_limit * 0.75))
        await deliver_session(self.session, channel, 1)
        self.assertEqual(self.session.data["status"], "uploaded")
        self.assertEqual(self.session.data["pdf_parts"][0], first)
        self.assertTrue(all((self.session.directory / "pdfs" / part["file"]).stat().st_size <= channel.guild.filesize_limit * 0.95
                            for part in self.session.data["pdf_parts"] if part["file"] != first["file"]))
        self.assertEqual(self.session.data["pdf_slide_numbers"], list(range(1, 9)))

    async def cancellation_case(self, fail, commit_fails=False):
        entered = threading.Event()
        released = threading.Event()
        finished = threading.Event()
        original = build_export
        def worker(*args, **kwargs):
            entered.set()
            released.wait(5)
            try:
                if fail:
                    raise ValueError("worker failed during cancellation")
                return original(*args, **kwargs)
            finally:
                finished.set()
        with patch("runner.delivery.build_export", side_effect=worker):
            task = asyncio.create_task(prepare_export(self.session, 100_000))
            await asyncio.to_thread(entered.wait, 2)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            released.set()
            if fail or commit_fails:
                with self.assertLogs("runner.delivery", level="ERROR"), self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(self.session.data["pdf_parts"])
            self.assertTrue(finished.is_set())

    async def test_cancellation_joins_successful_worker_before_committing(self):
        await self.cancellation_case(False)

    async def test_cancellation_remains_cancelled_when_worker_fails(self):
        await self.cancellation_case(True)

    async def test_cancellation_remains_cancelled_when_manifest_commit_fails(self):
        original_save = self.session.save
        def fail_after_build():
            if self.session.data.get("export_phase") == "uploading PDFs":
                raise OSError("Simulated full disk during manifest commit")
            return original_save()
        with patch.object(self.session, "save", side_effect=fail_after_build):
            await self.cancellation_case(False, commit_fails=True)
        self.assertFalse(list((self.session.directory / "pdfs").glob("*.pdf")))
        self.assertEqual(self.session.data["pdf_parts"], [])


if __name__ == "__main__":
    unittest.main()
