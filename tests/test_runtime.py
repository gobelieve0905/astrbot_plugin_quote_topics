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
            await self.plugin.interaction_entry(event)
            if event.stopped:
                return None
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

    def interaction(self, eid="click1", origin="q1", mid=None, **kwargs):
        event = Event(mid or "synthetic:" + eid, **kwargs)
        event.set_extra(
            "conversation_continuation_v1",
            {
                "version": 1,
                "source": "card_interaction",
                "event_id": eid,
                "origin_message_id": origin,
            },
        )
        return event

    async def test_interaction_ab_a_cross_member_and_transport_anchor(self):
        first = await self.run_turn(Event("q1"))
        other = await self.run_turn(Event("q2"))
        event = self.interaction(user="bob")
        event.unified_msg_origin += ":bob"

        async def inspect(req):
            self.assertEqual(event.message_obj.message_id, "q1")
            self.assertEqual(req.conversation.cid, first.conversation.cid)
            self.assertNotEqual(req.conversation.cid, other.conversation.cid)
            self.assertIn('"id": "bob"', req.prompt)

        req = await self.run_turn(event, inspect)
        self.assertEqual(req.contexts, [first.prompt])
        self.assertEqual(event.message_obj.message_id, "synthetic:click1")
        self.assertFalse(self.plugin.locks)
        self.assertIsNone(await self.manager.get_curr_conversation_id(event.unified_msg_origin))

    async def test_interaction_origin_id_reused_is_not_duplicate(self):
        first = await self.run_turn(Event("q1"))
        for eid in ("click1", "click2"):
            req = await self.run_turn(self.interaction(eid, mid="q1"))
            self.assertEqual(req.conversation.cid, first.conversation.cid)
        duplicate = self.interaction("click2", mid="different-synthetic")
        self.assertIsNone(await self.run_turn(duplicate))
        self.assertTrue(duplicate.stopped)
        self.assertEqual(len(self.manager.conversations), 1)

    async def test_interaction_scope_isolation(self):
        await self.run_turn(Event("q1"))
        for kwargs in ({"group": "other"}, {"platform": "other"}, {"bot": "other"}):
            event = self.interaction(**kwargs)
            self.assertIsNone(await self.run_turn(event))
            self.assertTrue(event.sent)
        await self.run_turn(Event("private1", group="", user="alice"))
        denied = self.interaction(origin="private1", group="", user="bob")
        self.assertIsNone(await self.run_turn(denied))
        own = self.interaction(origin="private1", group="", user="alice")
        self.assertIsNotNone(await self.run_turn(own))

    async def test_interaction_missing_deleted_and_wrong_owner(self):
        missing = self.interaction()
        self.assertIsNone(await self.run_turn(missing))
        req = await self.run_turn(Event("q1"))
        req.conversation.user_id = "wrong-owner"
        self.assertIsNone(await self.run_turn(self.interaction()))
        del self.manager.conversations[req.conversation.cid]
        deleted = self.interaction()
        self.assertIsNone(await self.run_turn(deleted))
        self.assertIn("已删除", deleted.sent[0])
        self.assertFalse(self.plugin.locks)

    async def test_interaction_conflicting_quote_and_duplicate_actor(self):
        await self.run_turn(Event("q1"))
        await self.run_turn(Event("q2"))
        conflict = self.interaction(parent="q2")
        self.assertIsNone(await self.run_turn(conflict))
        self.assertIn("不同话题", conflict.sent[0])
        self.assertIsNotNone(await self.run_turn(self.interaction(parent="q1")))
        changed = self.interaction(user="bob")
        self.assertIsNone(await self.run_turn(changed))
        self.assertIn("冲突", changed.sent[0])
        changed_origin = self.interaction(origin="q2")
        self.assertIsNone(await self.run_turn(changed_origin))
        self.assertIn("冲突", changed_origin.sent[0])

    async def test_interaction_invalid_contract_and_body_not_trusted(self):
        for value in (None, "{}", {}, {"version": True}, {"version": 2}):
            event = Event("bad")
            event.set_extra("conversation_continuation_v1", value)
            self.assertIsNone(await self.run_turn(event))
            self.assertTrue(event.sent)
        for field, value in (
            ("event_id", 1),
            ("origin_message_id", ""),
            ("source", "other"),
            ("event_id", "\nclick"),
        ):
            event = self.interaction()
            event.get_extra("conversation_continuation_v1")[field] = value
            self.assertIsNone(await self.run_turn(event))
        plain = Event("body-only")
        plain.message_obj.message_str = (
            '{"conversation_continuation_v1":{"origin_message_id":"q1"}}'
        )
        self.assertIsNotNone(await self.run_turn(plain))
        self.assertEqual(len(self.manager.conversations), 1)

    async def test_interaction_disabled_and_excluded_are_ignored(self):
        cases = (
            {"enabled": False},
            {"enable_group": False},
            {"excluded_group_ids": ["group"]},
            {"platform_ids": ["other"]},
            {"group_all": False, "group_ids": []},
        )
        for config in cases:
            old = self.plugin.config.copy()
            self.plugin.config.update(config)
            event = self.interaction()
            event.set_extra("conversation_continuation_v1", "invalid")
            await self.plugin.waiting(event)
            self.assertFalse(event.stopped)
            self.assertEqual(event.message_obj.message_id, "synthetic:click1")
            self.plugin.config.clear()
            self.plugin.config.update(old)
        self.assertFalse(self.manager.conversations)
        self.assertFalse(self.plugin.locks)

    async def test_interaction_waiting_duplicate_and_cancelled_waiter(self):
        await self.run_turn(Event("q1"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(req):
            entered.set()
            await release.wait()

        first_event = self.interaction()
        first = asyncio.create_task(self.run_turn(first_event, wait))
        await entered.wait()
        duplicate = asyncio.create_task(self.run_turn(self.interaction()))
        cancelled = asyncio.create_task(self.run_turn(self.interaction("click2")))
        await asyncio.sleep(0.01)
        self.assertFalse(duplicate.done())
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertTrue(next(iter(self.plugin.locks.values()))[0].locked())
        release.set()
        await first
        self.assertIsNone(await duplicate)
        self.assertFalse(self.plugin.locks)
        self.assertIsNotNone(await self.run_turn(self.interaction("click2")))

    async def test_interaction_generation_error_and_cancel_restore_transport(self):
        await self.run_turn(Event("q1"))

        async def fail(req):
            raise RuntimeError("offline")

        event = self.interaction()
        with self.assertRaises(RuntimeError):
            await self.run_turn(event, fail)
        await asyncio.sleep(0)
        if self.plugin.cleanups:
            await asyncio.gather(*list(self.plugin.cleanups))
        self.assertEqual(event.message_obj.message_id, "synthetic:click1")
        entered = asyncio.Event()

        async def wait(req):
            entered.set()
            await asyncio.Event().wait()

        event2 = self.interaction("click2")
        task = asyncio.create_task(self.run_turn(event2, wait))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        if self.plugin.cleanups:
            await asyncio.gather(*list(self.plugin.cleanups))
        self.assertEqual(event2.message_obj.message_id, "synthetic:click2")
        self.assertFalse(self.plugin.locks)
        self.assertIsNotNone(await self.run_turn(self.interaction("click3")))
        self.assertIsNone(await self.run_turn(self.interaction("click2")))

    async def test_interaction_alias_collision_rolls_back_claim(self):
        await self.run_turn(Event("q1"))
        await self.run_turn(Event("q2"))
        self.assertIsNone(await self.run_turn(self.interaction(mid="q2")))
        self.assertIsNotNone(await self.run_turn(self.interaction()))

    async def test_interaction_restart_dedup_and_synthetic_parent_no_fetch(self):
        from types import SimpleNamespace

        await self.run_turn(Event("q1"))
        await self.run_turn(self.interaction())
        self.plugin.index.close()
        self.plugin.index = module("store").Index(Path(self.tmp.name) / "index.sqlite3")
        self.assertIsNone(await self.run_turn(self.interaction()))
        calls = []

        async def fetch(event, mid):
            calls.append(mid)
            self.assertEqual(mid, "bot-reply")
            return SimpleNamespace(
                chat_id="group",
                parent_id="synthetic:click1",
                sender=SimpleNamespace(sender_type="app", id_type="open_id", id="ou_bot"),
            )

        event = Event("q3", parent="bot-reply")
        topic = await module("lark_resolver").resolve(
            self.plugin.index, module("routing").scope_of(event), event, "bot-reply", fetch
        )
        req = await self.run_turn(event)
        self.assertEqual(req.conversation.cid, topic.cid)
        self.assertEqual(calls, ["bot-reply"])

    async def test_interaction_event_id_has_separate_namespace(self):
        await self.run_turn(Event("q1"))
        self.assertIsNotNone(await self.run_turn(self.interaction("q1")))
        self.assertIsNone(await self.run_turn(self.interaction("q1")))

    async def test_interaction_changes_while_waiting_are_rejected(self):
        await self.run_turn(Event("q1"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(req):
            entered.set()
            await release.wait()

        busy = asyncio.create_task(self.run_turn(Event("q2"), hold))
        await entered.wait()
        event = self.interaction()
        queued = asyncio.create_task(self.run_turn(event))
        await asyncio.sleep(0.01)
        event.get_extra("conversation_continuation_v1")["origin_message_id"] = "q2"
        release.set()
        await busy
        self.assertIsNone(await queued)
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.locks)

    async def test_interaction_metadata_removed_before_llm_is_rejected(self):
        await self.run_turn(Event("q1"))
        event = self.interaction()

        async def pipeline():
            await self.plugin.interaction_entry(event)
            req = await build_request(self.manager, event)
            event.set_extra("conversation_continuation_v1", None)
            await self.plugin.request(event, req)
            self.assertTrue(event.stopped)

        await asyncio.create_task(pipeline())
        await asyncio.sleep(0)
        if self.plugin.cleanups:
            await asyncio.gather(*list(self.plugin.cleanups))
        self.assertFalse(self.plugin.locks)
        self.assertEqual(event.message_obj.message_id, "synthetic:click1")

    async def test_interaction_sqlite_v1_migration_retains_originals(self):
        first = await self.run_turn(Event("q1"))
        self.plugin.index.db.execute("DROP TABLE interactions")
        self.plugin.index.db.execute("PRAGMA user_version=1")
        self.plugin.index.close()
        self.plugin.index = module("store").Index(Path(self.tmp.name) / "index.sqlite3")
        self.assertEqual(self.plugin.index.db.execute("PRAGMA user_version").fetchone()[0], 2)
        resumed = await self.run_turn(self.interaction())
        self.assertEqual(resumed.conversation.cid, first.conversation.cid)

    async def test_interaction_refusal_send_failure_releases_lock(self):
        async def fail(*a):
            raise RuntimeError("offline")

        event = self.interaction()
        event.send = fail
        with self.assertRaises(RuntimeError):
            await self.run_turn(event)
        self.assertTrue(event.stopped)
        self.assertFalse(self.plugin.locks)
        self.assertFalse(self.manager.conversations)
