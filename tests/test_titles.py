import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import module, runtime


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin, self.manager, _ = runtime(Path(self.tmp.name))
        self.provider = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="飞书插件安装排查"))
        )
        self.plugin.context.get_using_provider_async = AsyncMock(return_value=self.provider)
        self.cid = await self.manager.new_conversation("owner", title="引用话题 q1")
        self.topic = module("store").Topic(self.cid, "owner")
        self.conv = self.manager.conversations[self.cid]
        self.history = json.dumps(
            [
                {"role": "user", "content": "安装插件失败了"},
                {"role": "tool", "content": "TOOL_SECRET"},
                {"role": "assistant", "content": "检查网络连接"},
            ]
        )
        self.conv.history = self.history

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def drain(self):
        if self.plugin.titles.tasks:
            await asyncio.gather(*list(self.plugin.titles.tasks.values()))
        await asyncio.sleep(0)

    async def test_default_off(self):
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.provider.text_chat.assert_not_called()

    async def test_once_only_and_no_history_changes(self):
        self.plugin.config["auto_topic_title"] = True
        other = await self.manager.new_conversation("other", title="OTHER_SECRET")
        self.plugin.titles.schedule(self.topic, "owner")
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "飞书插件安装排查")
        self.assertEqual(self.conv.history, self.history)
        self.assertEqual(self.manager.conversations[other].title, "OTHER_SECRET")
        prompt = self.provider.text_chat.call_args.kwargs["prompt"]
        self.assertNotIn("SECRET", prompt)
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.provider.text_chat.assert_awaited_once()

    async def test_failure_retries_next_turn(self):
        self.plugin.config["auto_topic_title"] = True
        self.provider.text_chat.side_effect = TimeoutError
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "引用话题 q1")
        self.provider.text_chat.side_effect = None
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "飞书插件安装排查")

    async def test_manual_rename_delete_disable_during_generation(self):
        for change in ("rename", "delete", "disable"):
            with self.subTest(change=change):
                self.plugin.config["auto_topic_title"] = True
                self.manager.conversations[self.cid] = self.conv
                self.conv.title = "引用话题 q1"

                async def completion(change=change, **kwargs):
                    if change == "rename":
                        self.conv.title = "我自己起的标题"
                    elif change == "delete":
                        self.manager.conversations.pop(self.cid)
                    else:
                        self.plugin.config["auto_topic_title"] = False
                    return SimpleNamespace(completion_text="不应保存")

                self.provider.text_chat.side_effect = completion
                self.plugin.titles.schedule(self.topic, "owner")
                await self.drain()
                self.assertNotEqual(self.conv.title, "不应保存")

    async def test_incomplete_or_wrong_owner_skipped(self):
        self.plugin.config["auto_topic_title"] = True
        self.conv.history = '[{"role":"user","content":"hello"}]'
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.conv.history = self.history
        self.conv.user_id = "someone_else"
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.provider.text_chat.assert_not_called()

    async def test_unload_cancels_background_calls(self):
        self.plugin.config["auto_topic_title"] = True
        started = asyncio.Event()

        async def completion(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.provider.text_chat.side_effect = completion
        self.plugin.titles.schedule(self.topic, "owner")
        await asyncio.wait_for(started.wait(), 1)
        await self.plugin.titles.close()
        self.assertFalse(self.plugin.titles.tasks)
        self.assertEqual(self.conv.title, "引用话题 q1")
