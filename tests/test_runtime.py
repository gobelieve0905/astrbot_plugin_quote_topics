import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Event, build_request, module, runtime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, self.manager, self.key = runtime(Path(self.tmp.name))

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def run_turn(self, event, before_save=None):
        # A task per incoming message matches AstrBot 4.28.0 EventBus.
        async def pipeline():
            await self.plugin.waiting(event)
            if event.stopped:
                return None
            req = await build_request(self.manager, event)
            await self.plugin.request(event, req)
            if event.stopped:
                return None
            if before_save:
                await before_save(req)
            req.conversation.history = json.dumps(req.contexts + [req.prompt])
            return req

        result = await asyncio.create_task(pipeline())
        await asyncio.sleep(0)
        if self.plugin.cleanups:
            await asyncio.gather(*list(self.plugin.cleanups))
        return result

    async def test_new_topics_resume_old_and_keep_legacy_selection(self):
        original = await self.manager.new_conversation(Event().unified_msg_origin)
        self.manager.conversations[original].history = '["legacy secret"]'
        first = await self.run_turn(Event("q1"))
        second = await self.run_turn(Event("q2"))
        resumed = await self.run_turn(Event("q3", parent="q1", user="bob"))
        self.assertNotEqual(first.conversation.cid, second.conversation.cid)
        self.assertEqual(resumed.conversation.cid, first.conversation.cid)
        self.assertEqual(first.contexts, [])
        self.assertNotIn("legacy secret", str(resumed.contexts))
        self.assertIn('"id": "bob"', resumed.prompt)
        self.assertEqual(
            await self.manager.get_curr_conversation_id(Event().unified_msg_origin), original
        )

    async def test_same_group_waits_until_history_is_written(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(req):
            entered.set()
            await release.wait()

        first = asyncio.create_task(self.run_turn(Event("q1"), wait))
        await entered.wait()
        second = asyncio.create_task(self.run_turn(Event("q2", parent="q1")))
        await asyncio.sleep(0.01)
        self.assertFalse(second.done())
        other = await self.run_turn(Event("other", group="other"))
        self.assertIsNotNone(other)
        release.set()
        req1, req2 = await asyncio.gather(first, second)
        self.assertEqual(req2.contexts, [req1.prompt])
        self.assertFalse(self.plugin.locks)

    async def test_duplicate_delivery_is_suppressed(self):
        await self.run_turn(Event("q1"))
        duplicate = Event("q1")
        self.assertIsNone(await self.run_turn(duplicate))
        self.assertTrue(duplicate.stopped)
        self.assertEqual(len(self.manager.conversations), 1)

    async def test_group_sharing_when_core_uses_member_specific_sessions(self):
        alice = Event("q1")
        alice.unified_msg_origin += ":alice"
        first = await self.run_turn(alice)
        bob = Event("q2", parent="q1", user="bob")
        bob.unified_msg_origin += ":bob"
        second = await self.run_turn(bob)
        self.assertEqual(first.conversation.cid, second.conversation.cid)
        self.assertEqual(second.conversation.user_id, alice.unified_msg_origin)
        self.assertEqual(second.contexts, [first.prompt])

    async def test_cancelled_waiter_does_not_release_active_owner_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(req):
            entered.set()
            await release.wait()

        owner = asyncio.create_task(self.run_turn(Event("q1"), wait))
        await asyncio.wait_for(entered.wait(), 2)
        waiter = asyncio.create_task(self.run_turn(Event("q2", parent="q1")))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        entry = next(iter(self.plugin.locks.values()))
        self.assertTrue(entry[0].locked())
        self.assertEqual(entry[1], 1)
        release.set()
        await owner
        self.assertFalse(self.plugin.locks)

    async def test_private_reference_cannot_access_other_user(self):
        await self.run_turn(Event("q1", group="", user="alice"))
        event = Event("q2", group="", user="bob", parent="q1")

        async def fail(*a):
            raise module("routing").CannotRestore("无法恢复")

        with patch.object(module("main"), "resolve", fail):
            await self.run_turn(event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(self.manager.conversations), 1)

    async def test_deleted_conversation_is_not_recreated(self):
        req = await self.run_turn(Event("q1"))
        del self.manager.conversations[req.conversation.cid]
        event = Event("q2", parent="q1")
        await self.run_turn(event)
        self.assertTrue(event.stopped)
        self.assertIn("已删除", event.sent[0])

    async def test_cancelled_generation_releases_lock_and_retains_input(self):
        entered = asyncio.Event()

        async def wait(req):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.run_turn(Event("q1"), wait))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        req = await self.run_turn(Event("q2", parent="q1"))
        self.assertIsNotNone(req)
        self.assertEqual(req.contexts, [])

    async def test_refusal_send_failure_still_stops_request(self):
        event = Event(parent="unknown")

        async def fail(*a):
            raise RuntimeError("offline")

        event.send = fail
        with patch.object(module("main"), "resolve", fail):
            with self.assertRaises(RuntimeError):
                await self.run_turn(event)
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.locks)

    async def test_disabled_does_not_touch_selection_or_database(self):
        self.plugin.config["enabled"] = False
        await self.plugin.waiting(Event())
        self.assertFalse(self.manager.conversations)
        self.assertFalse(self.plugin.locks)

    async def test_foreign_request_and_wrong_cid_fail_closed(self):
        event = Event()
        event.set_extra("provider_request", object())
        await self.plugin.waiting(event)
        self.assertTrue(event.stopped)
        event2 = Event("q2")

        async def pipeline():
            await self.plugin.waiting(event2)
            req = await build_request(self.manager, event2)
            req.conversation = None
            await self.plugin.request(event2, req)

        await asyncio.create_task(pipeline())
        await asyncio.sleep(0)
        self.assertTrue(event2.stopped)
