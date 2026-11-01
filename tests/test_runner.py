"""Offline tests: no real class is joined and no Discord messages are sent."""

import asyncio
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from PIL import Image
from playwright.async_api import async_playwright
from pypdf import PdfReader

from runner.bot import ClassPointBot
from runner.capture import EndDetector, OBSERVE, READ_STATE, SlideRecorder, join_class
from runner.config import Config
from runner.delivery import deliver_session
from runner.pdf import build_pdfs
from runner.state import Session, recover_sessions

FIXTURE = Path(__file__).parent / "fixtures/classpoint.html"


def configuration(root, **changes):
    options = dict(application_id=1, guild_id=2, data_dir=str(root), token_file=str(root / "token"),
                   poll_seconds=0.1, settle_seconds=0, end_grace_seconds=0.2,
                   disconnect_grace_seconds=0.4)
    options.update(changes)
    return Config(**options)


def png(color, size=(640, 360)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


class StateAndPDFTest(unittest.TestCase):
    def test_content_dedup_recovery_and_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp), "12345", "Student", 10, 20)
            self.assertTrue(session.add_slide(png("red")))
            self.assertTrue(session.add_slide(png("blue")))
            self.assertFalse(session.add_slide(png("red")))
            recovered = recover_sessions(Path(tmp))[0]
            self.assertEqual(recovered.data["status"], "pending_upload")
            self.assertIn("restarted", recovered.data["reason"])
            self.assertFalse(recovered.add_slide(png("blue")))
            paths = build_pdfs(recovered, 1_000_000)
            self.assertEqual(len(paths), 1)
            reader = PdfReader(paths[0])
            self.assertEqual(len(reader.pages), 2)
            for page in reader.pages:
                self.assertAlmostEqual(float(page.mediabox.width / page.mediabox.height), 16 / 9)
                self.assertEqual(len(page.images), 1)
            self.assertEqual(reader.pages[0].images[0].image.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(reader.pages[1].images[0].image.getpixel((0, 0)), (0, 0, 255))

    def test_pdf_splits_without_losing_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp), "TEST", "Student", 10, 20)
            for offset in range(4):
                image = Image.effect_noise((200, 100), 80).convert("RGB")
                image.putpixel((0, 0), (offset, 10, 20))
                output = io.BytesIO()
                image.save(output, "PNG")
                session.add_slide(output.getvalue())
            one = build_pdfs(session, 1_000_000)[0]
            limit = one.stat().st_size // 2
            paths = build_pdfs(session, limit)
            self.assertGreater(len(paths), 1)
            self.assertTrue(all(path.stat().st_size <= limit for path in paths))
            self.assertEqual(sum(len(PdfReader(path).pages) for path in paths), 4)

    def test_slide_pause_is_not_session_end(self):
        detector = EndDetector(15, 120)
        waiting = dict(classroom=True, join=False, ended=False, disconnected=False, slide=None)
        self.assertIsNone(detector.update(waiting, 0))
        self.assertIsNone(detector.update(waiting, 5000))
        ended = dict(waiting, classroom=False, join=True)
        self.assertIsNone(detector.update(ended, 5001))
        self.assertIsNone(detector.update(waiting, 5008))
        self.assertIsNone(detector.update(ended, 5010))
        self.assertIsNone(detector.update(ended, 5024))
        self.assertIn("ended", detector.update(ended, 5025))

    def test_network_reconnect_grace(self):
        detector = EndDetector(15, 120)
        state = dict(classroom=True, disconnected=True)
        self.assertIsNone(detector.update(state, 0))
        self.assertIsNone(detector.update(state, 119))
        self.assertIsNone(detector.update(dict(state, disconnected=False), 120))
        self.assertIsNone(detector.update(state, 121))
        self.assertIn("Connection", detector.update(state, 241))

    def test_token_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = configuration(Path(tmp))
            path = Path(config.token_file)
            path.write_text("dummy-test-value")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                config.token()
            path.chmod(0o600)
            self.assertEqual(config.token(), "dummy-test-value")


class RunnerBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(viewport={"width": 1600, "height": 1000})
        await self.context.add_init_script(OBSERVE)
        self.page = await self.context.new_page()
        colors = {"a": "#f3e8d0", "a-copy": "#f3e8d0", "b": "#2864dc", "c": "#29a36a"}

        async def route_classpoint(route):
            path = route.request.url.rsplit("/", 1)[-1]
            if path.startswith("test-slide-"):
                name = path.removeprefix("test-slide-").removesuffix(".svg")
                body = (f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360">'
                        f'<rect width="640" height="360" fill="{colors[name]}"/>'
                        '<rect x="32" y="32" width="80" height="60" fill="#ba3322"/></svg>')
                await route.fulfill(content_type="image/svg+xml", body=body)
            else:
                await route.fulfill(content_type="text/html", body=FIXTURE.read_text())
        await self.context.route("https://www.classpoint.app/**", route_classpoint)
        self.stop = asyncio.Event()
        await join_class(self.page, "12345", "Local test", self.stop)
        self.session = Session.create(self.root, "12345", "Local test", 10, 20)
        self.recorder = SlideRecorder(configuration(self.root), self.session)
        self.cdp = await self.context.new_cdp_session(self.page)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.tmp.cleanup()

    async def show(self, name):
        await self.page.evaluate("name => showSlide(name)", name)
        await self.page.wait_for_function("() => {const i=document.querySelector('.presenter_img img'); return i?.complete && i.naturalWidth > 0;}")

    async def test_slide_only_session_wide_dedup_and_annotations(self):
        self.assertFalse(await self.recorder.capture_once(self.page, self.cdp, 0))
        await self.show("a")
        self.assertTrue(await self.recorder.capture_once(self.page, self.cdp, 1))
        await self.page.evaluate("document.querySelector('#sidebar').textContent='Stars: 100'")
        self.assertFalse(await self.recorder.capture_once(self.page, self.cdp, 2))
        await self.show("b")
        self.assertTrue(await self.recorder.capture_once(self.page, self.cdp, 3))
        await self.show("a-copy")
        self.assertFalse(await self.recorder.capture_once(self.page, self.cdp, 4))
        await self.page.evaluate("annotate()")
        self.assertTrue(await self.recorder.capture_once(self.page, self.cdp, 5))
        with Image.open(self.session.images[-1]) as image:
            self.assertEqual(image.size, (640, 360))
            self.assertEqual(image.getpixel((205, 205))[:3], (255, 0, 255))
        await self.page.evaluate("document.querySelector('.annotation').remove(); document.querySelector('.presenter_img').style.marginTop='1500px'")
        await self.show("c")
        self.assertTrue(await self.recorder.capture_once(self.page, self.cdp, 6))
        with Image.open(self.session.images[-1]) as image:
            self.assertEqual(image.size, (640, 360))
            self.assertEqual(image.getpixel((639, 359))[:3], (41, 163, 106))
        self.assertEqual(len(self.session.images), 4)

    async def test_stability_placeholder_dialog_and_letterboxing(self):
        self.recorder.config.settle_seconds = 0.6
        await self.show("a")
        self.assertFalse(await self.recorder.capture_once(self.page, self.cdp, 0))
        self.assertFalse(await self.recorder.capture_once(self.page, self.cdp, 0.5))
        self.assertTrue(await self.recorder.capture_once(self.page, self.cdp, 0.7))
        await self.page.evaluate("document.querySelector('.presenter_img').classList.add('audienceimage')")
        self.assertIsNone((await self.page.evaluate(READ_STATE))["slide"])
        await self.page.evaluate("document.querySelector('.presenter_img').classList.remove('audienceimage'); document.body.insertAdjacentHTML('beforeend','<div role=dialog>Question</div>')")
        self.assertIsNone((await self.page.evaluate(READ_STATE))["slide"])
        await self.page.evaluate("document.querySelector('[role=dialog]').remove(); const i=document.querySelector('.presenter_img img'); i.style.height='500px'; i.style.objectFit='contain';")
        self.recorder.config.settle_seconds = 0
        await self.show("b")
        self.assertTrue(await self.recorder.capture_once(self.page, self.cdp, 1))
        with Image.open(self.session.images[-1]) as image:
            self.assertEqual(image.size, (640, 360))
            self.assertEqual(image.getpixel((0, 0))[:3], (40, 100, 220))

    async def test_detect_end_and_manual_stop(self):
        task = asyncio.create_task(self.recorder.watch(self.page, self.stop))
        await self.show("a")
        for _ in range(30):
            if self.session.images:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(len(self.session.images), 1)
        await self.page.evaluate("document.querySelector('.presenter_img').innerHTML='Presenter is not in slideshow'")
        await asyncio.sleep(0.6)
        self.assertFalse(task.done())
        await self.page.evaluate("document.body.innerHTML='<input placeholder=\"Class code\">'")
        self.assertIn("ended", await asyncio.wait_for(task, 5))
        self.session.finish("Class ended")
        self.assertEqual(len(PdfReader(build_pdfs(self.session, 1_000_000)[0]).pages), 1)
        self.stop.set()
        self.assertIn("stopped", await self.recorder.watch(self.page, self.stop))

    async def test_invalid_code_and_name(self):
        async def invalid_code(route):
            await route.fulfill(content_type="text/html", body='''<input placeholder="Class code"><button onclick="document.querySelector('input').setAttribute('aria-invalid','true')"><svg data-testid="ArrowForwardIcon"></svg></button>''')
        await self.context.unroute("https://www.classpoint.app/**")
        await self.context.route("https://www.classpoint.app/**", invalid_code)
        with self.assertRaisesRegex(ValueError, "invalid"):
            await join_class(self.page, "12345", "Local", self.stop)


class FakeChannel:
    def __init__(self):
        self.guild = SimpleNamespace(filesize_limit=10_000_000)
        self.messages = []
        self.fail = False

    async def history(self, **_kwargs):
        for message in reversed(self.messages):
            yield message

    async def send(self, content, **kwargs):
        if self.fail:
            raise OSError("Simulated temporary upload failure")
        file = kwargs.get("file")
        message = SimpleNamespace(id=len(self.messages) + 100, author=SimpleNamespace(id=1), content=content,
                                  attachments=[SimpleNamespace(filename=file.filename)] if file else [])
        self.messages.append(message)
        return message


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_upload_retry_and_crash_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp), "12345", "Student", 10, 20)
            session.add_slide(png("green"))
            session.finish("Class ended")
            channel = FakeChannel()
            channel.fail = True
            with self.assertRaises(OSError):
                await deliver_session(session, channel, 1)
            self.assertEqual(session.data["status"], "pending_upload")
            channel.fail = False
            await deliver_session(session, channel, 1)
            self.assertEqual(session.data["status"], "uploaded")
            self.assertEqual(len(channel.messages), 1)
            session.data.update(status="pending_upload", uploaded_parts={})
            await deliver_session(session, channel, 1)
            self.assertEqual(len(channel.messages), 1)
            self.assertEqual(len(session.data["uploaded_parts"]), 1)

    async def test_no_slides_makes_no_empty_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp), "12345", "Student", 10, 20)
            session.finish("No slides shared")
            channel = FakeChannel()
            await deliver_session(session, channel, 1)
            self.assertIn("No PDF", channel.messages[0].content)
            self.assertEqual(channel.messages[0].attachments, [])

    async def test_commands_reject_other_guild_and_wrong_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = ClassPointBot(configuration(Path(tmp)))
            group = bot.tree.get_commands(guild=SimpleNamespace(id=2))[0]
            interaction = SimpleNamespace(guild_id=3, response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await group.interaction_check(interaction))
            interaction.guild_id = 2
            interaction.channel_id = 21
            bot.output_channel = lambda: SimpleNamespace(id=20, mention="#classpoint-screenshotter")
            self.assertFalse(await group.interaction_check(interaction))
            self.assertEqual(interaction.response.send_message.await_count, 2)
            self.assertFalse(bot.intents.message_content)


if __name__ == "__main__":
    unittest.main()
