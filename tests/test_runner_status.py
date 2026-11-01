"""Public command replies and timed progress posts; no live Discord writes."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from runner.bot import ClassPointBot
from runner.config import Config
from runner.state import Session


class StatusUpdatesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(application_id=1, guild_id=2, data_dir=str(self.root), token_file="unused")
        self.bot = ClassPointBot(self.config)
        self.session = Session.create(self.root, "12345", "Test", 10, 20)
        self.session.data["phase"] = "waiting for presenter/slides"
        self.bot.active = self.session
        self.channel = SimpleNamespace(id=20, send=AsyncMock())
        self.bot.output_channel = lambda: self.channel
        self.stop = asyncio.Event()
        self.group = self.bot.tree.get_commands(guild=SimpleNamespace(id=2))[0]
        self.interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            user=SimpleNamespace(id=10, display_name="Test"), channel_id=20,
            permissions=SimpleNamespace(manage_guild=False),
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_waits_ten_minutes_and_posts_repeated_public_updates(self):
        self.assertEqual(self.config.status_update_seconds, 600)
        calls = 0

        async def tick(stop, seconds):
            nonlocal calls
            self.assertEqual(seconds, 600)
            # The send always follows a wait, never immediately on joining.
            self.assertEqual(self.channel.send.await_count, calls)
            calls += 1
            if calls == 3:
                stop.set()

        with patch("runner.bot.pause_or_stop", side_effect=tick):
            await self.bot.periodic_status(self.session, self.stop)
        self.assertEqual(self.channel.send.await_count, 2)
        for call in self.channel.send.await_args_list:
            self.assertIn("12345", call.args[0])
            self.assertIn("0 unique slides", call.args[0])
            self.assertIn("waiting for presenter/slides", call.args[0])
            self.assertIn("Started <t:", call.args[0])
            self.assertNotIn("ephemeral", call.kwargs)
            self.assertFalse(call.kwargs["allowed_mentions"].everyone)

    async def test_stop_interrupts_wait_without_posting(self):
        task = asyncio.create_task(self.bot.periodic_status(self.session, self.stop))
        await asyncio.sleep(0)
        self.stop.set()
        await asyncio.wait_for(task, 0.5)
        self.channel.send.assert_not_awaited()

    async def test_finished_or_replaced_session_does_not_post(self):
        for transition in (lambda: self.session.finish("Done"), lambda: setattr(self.bot, "active", None)):
            self.session.data["status"] = "recording"
            self.bot.active = self.session
            async def tick(*_args):
                transition()
            with patch("runner.bot.pause_or_stop", side_effect=tick):
                await self.bot.periodic_status(self.session, self.stop)
        self.channel.send.assert_not_awaited()

    async def test_status_failure_does_not_end_recording(self):
        self.channel.send.side_effect = [OSError("offline"), None]
        calls = 0
        async def tick(stop, _seconds):
            nonlocal calls
            calls += 1
            if calls == 3:
                stop.set()
        with patch("runner.bot.pause_or_stop", side_effect=tick), self.assertLogs("runner.bot", level="WARNING"):
            await self.bot.periodic_status(self.session, self.stop)
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.session.data["status"], "recording")

    async def test_no_updates_in_a_different_channel(self):
        self.channel.id = 999
        calls = 0
        async def tick(stop, _seconds):
            nonlocal calls
            calls += 1
            if calls == 2:
                stop.set()
        with patch("runner.bot.pause_or_stop", side_effect=tick), self.assertLogs("runner.bot", level="WARNING"):
            await self.bot.periodic_status(self.session, self.stop)
        self.channel.send.assert_not_awaited()

    async def test_status_stop_and_retry_replies_are_public(self):
        await self.group.status.callback(self.group, self.interaction)
        await self.group.stop.callback(self.group, self.interaction)
        self.bot.recording_task = asyncio.get_running_loop().create_future()
        try:
            await self.group.stop.callback(self.group, self.interaction)
            self.assertTrue(self.bot.stop_event.is_set())
        finally:
            self.bot.recording_task.cancel()
        await self.group.retry.callback(self.group, self.interaction)
        self.assertEqual(self.interaction.response.send_message.await_count, 4)
        self.assertTrue(all(call.kwargs["ephemeral"] is False
                            for call in self.interaction.response.send_message.await_args_list))

    async def test_start_defers_publicly(self):
        self.bot.active = None
        self.bot.record = AsyncMock()
        await self.group.start.callback(self.group, self.interaction, "12345")
        await self.bot.recording_task
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)

    async def test_progress_task_is_cleaned_up_at_end(self):
        progress_started, progress_cancelled = asyncio.Event(), asyncio.Event()
        async def progress(*_args):
            progress_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                progress_cancelled.set()
        async def capture(_stop, joined):
            await joined()
            await progress_started.wait()
            return "Class ended"
        self.bot.edit_reply = AsyncMock()
        self.bot.periodic_status = progress
        with patch("runner.bot.SlideRecorder") as recorder:
            recorder.return_value.run.side_effect = capture
            await asyncio.wait_for(self.bot.record(self.session, self.interaction), 1)
        self.assertTrue(progress_cancelled.is_set())
        self.assertIsNone(self.bot.active)
        self.assertEqual(self.session.data["status"], "pending_upload")
        self.assertTrue(self.bot.upload_wakeup.is_set())


if __name__ == "__main__":
    unittest.main()
