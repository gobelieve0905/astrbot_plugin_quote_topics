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

    async def test_configured_fallback_after_primary_404(self):
        self.plugin.config["auto_topic_title"] = True
        backup = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="备用模型生成的标题"))
        )
        self.plugin.context.get_config = lambda **kw: {
            "agent_runner": {
                "runner_type": "local",
                "config": {
                    "model": {"fallback_provider_ids": ["missing", "primary", "backup", "backup"]}
                },
            }
        }
        providers = {"primary": self.provider, "backup": backup}
        self.plugin.context.get_provider_by_id = providers.get
        self.provider.text_chat.side_effect = type("NotFoundError", (Exception,), {})()
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "备用模型生成的标题")
        self.assertEqual(self.conv.history, self.history)
        self.provider.text_chat.assert_awaited_once()
        backup.text_chat.assert_awaited_once()
        self.assertEqual(
            backup.text_chat.call_args.kwargs, self.provider.text_chat.call_args.kwargs
        )

    async def test_all_candidates_fail_keep_placeholder(self):
        self.plugin.config["auto_topic_title"] = True
        backup = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(role="err")))
        self.plugin.context.get_config = lambda **kw: {
            "agent_runner": {
                "runner_type": "local",
                "config": {"model": {"fallback_provider_ids": ["backup"]}},
            }
        }
        self.plugin.context.get_provider_by_id = lambda _: backup
        self.provider.text_chat.side_effect = TimeoutError
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "引用话题 q1")
        backup.text_chat.assert_awaited_once()

    async def test_timeout_budget_for_reasoning_models(self):
        self.assertEqual(self.plugin.titles.request_timeout(), 120)
        for value, expected in [(1, 20), (999, 240), (180, 180), ("bad", 120)]:
            self.plugin.config["auto_topic_title_timeout"] = value
            self.assertEqual(self.plugin.titles.request_timeout(), expected)

    async def test_mixed_language_title_is_saved_in_full(self):
        self.plugin.config["auto_topic_title"] = True
        title = "Idol Empire 昨日 AppLovin 消耗分析"
        self.provider.text_chat.return_value.completion_text = title
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, title)
        self.provider.text_chat.assert_awaited_once()

    async def test_long_title_rewritten_once(self):
        self.plugin.config["auto_topic_title"] = True
        self.provider.text_chat.side_effect = [
            SimpleNamespace(completion_text="分析" * 30),
            SimpleNamespace(completion_text="Idol Empire 投放消耗分析"),
        ]
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "Idol Empire 投放消耗分析")
        self.assertEqual(self.provider.text_chat.await_count, 2)
        self.assertEqual(self.conv.history, self.history)

    async def test_rewrite_still_long_never_saves_cutoff(self):
        self.plugin.config["auto_topic_title"] = True
        self.provider.text_chat.return_value.completion_text = "分析" * 30
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "引用话题 q1")
        self.assertEqual(self.provider.text_chat.await_count, 2)

    async def test_invalid_output_does_not_trigger_rewrite(self):
        self.plugin.config["auto_topic_title"] = True
        self.provider.text_chat.return_value.completion_text = "标题\n解释"
        self.plugin.titles.schedule(self.topic, "owner")
        await self.drain()
        self.assertEqual(self.conv.title, "引用话题 q1")
        self.provider.text_chat.assert_awaited_once()
