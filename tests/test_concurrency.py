"""Recording ownership and command isolation without real classes or Discord."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from runner.bot import ClassPointBot, Recording
from runner.config import Config
from runner.state import Session


class ConcurrentRecordingsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bot = ClassPointBot(Config(application_id=1, guild_id=2, data_dir=str(self.root), token_file="unused",
                                        max_concurrent_sessions=2))
        self.group = self.bot.tree.get_commands(guild=SimpleNamespace(id=2))[0]
        self.bot.edit_reply = AsyncMock()
        self.joined = set()
        self.exits = {}

        async def run(recorder, stop, joined):
            self.joined.add(recorder.session.data["code"])
            await joined()
            await stop.wait()
            return "Recording stopped by request."

        self.recorder_patch = patch("runner.bot.SlideRecorder.run", run)
        self.recorder_patch.start()

    def interaction(self, user=10, manager=False):
        return SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()),
                               user=SimpleNamespace(id=user, display_name="Student"), channel_id=20,
                               permissions=SimpleNamespace(manage_guild=manager))

    async def asyncTearDown(self):
        await self.bot.close()
        self.recorder_patch.stop()
        self.tmp.cleanup()

    async def start(self, code, user=10):
        interaction = self.interaction(user)
        await self.group.start.callback(self.group, interaction, code)
        await asyncio.sleep(0)
        return interaction

    async def test_two_recordings_stop_independently_and_free_only_their_slot(self):
        await self.start("A", 10)
        await self.start("B", 11)
        self.assertEqual(self.joined, {"A", "B"})
        a, b = self.bot.recordings["A"], self.bot.recordings["B"]
        await self.group.stop.callback(self.group, self.interaction(10), "A")
        await asyncio.wait_for(a.task, 1)
        self.assertNotIn("A", self.bot.recordings)
        self.assertIs(self.bot.recordings["B"], b)
        self.assertFalse(b.stop.is_set())
        self.assertFalse(b.task.done())
        self.assertEqual(a.session.data["status"], "pending_upload")
        await self.start("C", 12)
        self.assertEqual(set(self.bot.recordings), {"B", "C"})

    async def test_start_race_duplicate_code_and_capacity(self):
        first, second = self.interaction(), self.interaction(11)
        await asyncio.gather(self.group.start.callback(self.group, first, "same"),
                             self.group.start.callback(self.group, second, " SAME "))
        self.assertEqual(list(self.bot.recordings), ["SAME"])
        self.assertEqual(first.response.defer.await_count + second.response.defer.await_count, 1)
        await self.start("B")
        blocked = await self.start("C")
        self.assertIn("slots", blocked.response.send_message.await_args.args[0])
        self.assertEqual(len(self.bot.recordings), 2)

    async def test_stop_permission_and_ambiguity(self):
        await self.start("A", 10)
        await self.start("B", 10)
        unauthorized = self.interaction(99)
        await self.group.stop.callback(self.group, unauthorized, "A")
        self.assertFalse(self.bot.recordings["A"].stop.is_set())
        self.assertIn("Only the person", unauthorized.response.send_message.await_args.args[0])
        ambiguous = self.interaction(10)
        await self.group.stop.callback(self.group, ambiguous)
        self.assertTrue(all(not r.stop.is_set() for r in self.bot.recordings.values()))
        manager = self.interaction(99, manager=True)
        await self.group.stop.callback(self.group, manager, "B")
        self.assertTrue(self.bot.recordings["B"].stop.is_set())
        self.assertFalse(self.bot.recordings["A"].stop.is_set())

    async def test_implicit_stop_selects_only_callers_recording(self):
        await self.start("A", 10)
        await self.start("B", 11)
        await self.group.stop.callback(self.group, self.interaction(11))
        self.assertTrue(self.bot.recordings["B"].stop.is_set())
        self.assertFalse(self.bot.recordings["A"].stop.is_set())

    async def test_status_lists_all_or_filters_code(self):
        await self.start("A", 10)
        await self.start("B", 11)
        all_status = self.interaction()
        await self.group.status.callback(self.group, all_status)
        text = all_status.response.send_message.await_args.args[0]
        self.assertIn("Class A", text)
        self.assertIn("Class B", text)
        self.assertIn("No image saved yet", text)
        filtered = self.interaction()
        await self.group.status.callback(self.group, filtered, "a")
        text = filtered.response.send_message.await_args.args[0]
        self.assertIn("Class A", text)
        self.assertNotIn("Class B", text)

    async def test_shutdown_finishes_all_recordings(self):
        await self.start("A")
        await self.start("B", 11)
        records = list(self.bot.recordings.values())
        await self.bot.close()
        self.assertTrue(all(r.task.done() for r in records))
        self.assertTrue(all(r.session.data["status"] == "pending_upload" for r in records))
        self.assertFalse(self.bot.recordings)
        rejected = self.interaction()
        await self.group.start.callback(self.group, rejected, "C")
        self.assertIn("restarting", rejected.response.send_message.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
