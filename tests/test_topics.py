import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from support import Event, module

Index, Topic = module("store").Index, module("store").Topic
rules = module("routing")
resolver = module("lark_resolver")


class StorageTests(unittest.TestCase):
    def test_restart_isolation_and_immutable_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.sqlite3"
            db = Index(path)
            topic = Topic("one", "owner")
            db.bind("group", "same-id", topic, "input")
            with self.assertRaises(ValueError):
                db.bind("group", "same-id", Topic("two", "owner"), "input")
            db.close()
            db = Index(path)
            self.assertEqual(db.lookup("group", "same-id"), topic)
            self.assertIsNone(db.lookup("private", "same-id"))
            db.close()

    def test_configuration_defaults_and_exclusions(self):
        event = Event()
        self.assertFalse(rules.enabled({}, event))
        cfg = dict(enabled=True, enable_group=True)
        self.assertFalse(rules.enabled(cfg, event))
        cfg["group_ids"] = ["group"]
        self.assertTrue(rules.enabled(cfg, event))
        cfg["excluded_group_ids"] = ["group"]
        cfg["group_all"] = True
        self.assertFalse(rules.enabled(cfg, event))
        self.assertFalse(rules.enabled(cfg, Event(group="")))

    def test_namespace_covers_platform_bot_chat_and_private_user(self):
        events = [
            Event(),
            Event(platform="other"),
            Event(bot="other"),
            Event(group="other"),
            Event(group="", user="alice"),
            Event(group="", user="bob"),
        ]
        self.assertEqual(len({rules.scope_of(e) for e in events}), len(events))
        self.assertEqual(rules.scope_of(Event(user="bob")), rules.scope_of(Event()))

    def test_raw_parent_survives_failed_reply_content_fetch(self):
        self.assertEqual(rules.quote_id(Event(parent="old")), "old")
        event = Event(parent="old")
        Reply = type("Reply", (), {})
        reply = Reply()
        reply.id = "different"
        event.components = [reply]
        with self.assertRaises(rules.CannotRestore):
            rules.quote_id(event)
        event = Event()
        event.components = [Reply()]
        with self.assertRaises(rules.CannotRestore):
            rules.quote_id(event)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Index(Path(self.tmp.name) / "db")
        self.event = Event()
        self.scope = rules.scope_of(self.event)
        self.topic = Topic("one", self.event.unified_msg_origin)
        self.db.bind(self.scope, "question", self.topic, "input")

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def reply(self, parent="question", chat="group", sender="cli_bot", kind="app"):
        return NS(
            parent_id=parent, chat_id=chat, sender=NS(id=sender, id_type="app_id", sender_type=kind)
        )

    async def test_native_card_and_split_replies_cache_without_card_plugin(self):
        calls = []

        async def fetch(event, mid):
            calls.append(mid)
            return self.reply("part1" if mid == "part2" else "question")

        result = await resolver.resolve(self.db, self.scope, self.event, "part2", fetch)
        self.assertEqual(result, self.topic)
        self.assertEqual(calls, ["part2", "part1"])
        calls.clear()
        self.assertEqual(
            await resolver.resolve(self.db, self.scope, self.event, "part2", fetch), self.topic
        )
        self.assertEqual(calls, [])

    async def test_foreign_chat_bot_human_and_unknown_identity_rejected(self):
        for item in [
            self.reply(chat="foreign"),
            self.reply(sender="other_bot"),
            self.reply(kind="user"),
            NS(chat_id="group", sender=None),
        ]:

            async def fetch(event, mid, item=item):
                return item

            with self.assertRaises(rules.CannotRestore):
                await resolver.resolve(self.db, self.scope, self.event, "reply", fetch)
            self.assertIsNone(self.db.lookup(self.scope, "reply"))

    async def test_open_id_bot_identity(self):
        async def fetch(event, mid):
            item = self.reply(sender="ou_bot")
            item.sender.id_type = "open_id"
            return item

        self.assertEqual(
            await resolver.resolve(self.db, self.scope, self.event, "reply", fetch), self.topic
        )

    async def test_unregistered_human_ancestor_does_not_inherit_topic(self):
        async def fetch(event, mid):
            return self.reply("human") if mid == "reply" else self.reply(kind="user")

        with self.assertRaises(rules.CannotRestore):
            await resolver.resolve(self.db, self.scope, self.event, "reply", fetch)

    async def test_cycles_and_missing_parent_fail(self):
        for parent in [None, "reply"]:

            async def fetch(event, mid, parent=parent):
                return self.reply(parent)

            with self.assertRaises(rules.CannotRestore):
                await resolver.resolve(self.db, self.scope, self.event, "reply", fetch)

    async def test_cancelled_fetch_leaves_no_binding(self):
        async def fetch(event, mid):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await resolver.resolve(self.db, self.scope, self.event, "reply", fetch)
        self.assertIsNone(self.db.lookup(self.scope, "reply"))
