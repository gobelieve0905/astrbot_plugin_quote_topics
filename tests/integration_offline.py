"""Run explicitly in an environment with AstrBot 4.28.0 installed.

Uses real AstrBot/Lark classes and SQLite, denies outbound socket connects.
Does not start an adapter, connect to Feishu, or invoke a model.
"""

import asyncio
import importlib
import json
import os
import socket
import sys
import tempfile
import types
from pathlib import Path


def deny_network(*args, **kwargs):
    raise AssertionError("Network connections are forbidden in this integration test")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
root = tempfile.TemporaryDirectory(prefix="quote-topics-integration-")
os.environ["ASTRBOT_ROOT"] = root.name


async def main():
    import lark_oapi
    from astrbot.core import db_helper
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from astrbot.core.provider.entities import ProviderRequest
    from lark_oapi.api.im.v1 import Message

    package = types.ModuleType("quote_topics_integration_plugin")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    plugin_module = importlib.import_module(package.__name__ + ".main")
    resolver = importlib.import_module(package.__name__ + ".lark_resolver")
    routing = importlib.import_module(package.__name__ + ".routing")
    await db_helper.initialize()
    manager = ConversationManager(db_helper)
    plugin = plugin_module.QuoteTopics(
        types.SimpleNamespace(conversation_manager=manager),
        dict(enabled=True, enable_group=True, group_all=True),
    )
    bot = lark_oapi.Client.builder().app_id("cli_test").app_secret("unused").build()

    def event(mid, parent=None, user="ou_alice"):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = "ou_bot"
        msg.group_id = "oc_test"
        msg.sender = MessageMember(user_id=user, nickname=user)
        msg.message_id = mid
        msg.message_str = "hello"
        msg.message = [Plain("hello")]
        msg.raw_message = types.SimpleNamespace(chat_id="oc_test", parent_id=parent)
        result = LarkMessageEvent(
            "hello", msg, PlatformMetadata("lark", "test", "test-instance"), "oc_test", bot
        )

        async def send(chain):
            raise AssertionError("Unexpected error reply: " + str(chain))

        result.send = send
        return result

    async def turn(evt):
        async def pipeline():
            await plugin.waiting(evt)
            assert not evt.is_stopped()
            cid = await manager.get_curr_conversation_id(evt.unified_msg_origin)
            conv = await manager.get_conversation(evt.unified_msg_origin, cid)
            req = ProviderRequest(
                prompt="hello", conversation=conv, contexts=json.loads(conv.history)
            )
            await plugin.request(evt, req)
            assert not evt.is_stopped()
            await manager.update_conversation(
                evt.unified_msg_origin,
                req.conversation.cid,
                history=req.contexts
                + [
                    {"role": "user", "content": req.prompt},
                    {"role": "assistant", "content": "answer"},
                ],
            )
            return req

        req = await asyncio.create_task(pipeline())
        await asyncio.sleep(0)
        if plugin.cleanups:
            await asyncio.gather(*list(plugin.cleanups))
        return req

    try:
        one = await turn(event("q1"))
        two = await turn(event("q2"))
        assert one.conversation.cid != two.conversation.cid
        assert one.contexts == two.contexts == []
        three = await turn(event("q3", "q1", "ou_bob"))
        assert three.conversation.cid == one.conversation.cid
        assert len(three.contexts) == 2
        assert "ou_bob" in three.prompt
        assert await manager.get_curr_conversation_id(event("x").unified_msg_origin) is None

        # Real SDK response model, including app_id (not bot open_id) sender.
        quoted = Message(
            {
                "message_id": "card1",
                "chat_id": "oc_test",
                "parent_id": "q1",
                "sender": {"sender_type": "app", "id_type": "app_id", "id": "cli_test"},
            }
        )

        async def fetch(evt, mid):
            return quoted

        evt = event("q4", "card1")
        resolved = await resolver.resolve(plugin.index, routing.scope_of(evt), evt, "card1", fetch)
        assert resolved.cid == one.conversation.cid
        await plugin.terminate()
        plugin = plugin_module.QuoteTopics(
            types.SimpleNamespace(conversation_manager=manager),
            dict(enabled=True, enable_group=True, group_all=True),
        )
        four = await turn(evt)
        assert four.conversation.cid == one.conversation.cid
        assert len(four.contexts) == 4
        # Run the optional title path with real persisted history and manager APIs.
        from unittest.mock import AsyncMock

        plugin.context.get_config = lambda **kw: {}
        plugin.config["auto_topic_title"] = True
        provider = types.SimpleNamespace(
            text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="群聊话题测试"))
        )
        plugin.context.get_using_provider_async = AsyncMock(return_value=provider)
        five = await turn(event("q5", "q1"))
        if plugin.titles.tasks:
            await asyncio.gather(*list(plugin.titles.tasks.values()))
        saved = await manager.get_conversation(five.conversation.user_id, five.conversation.cid)
        assert saved.title == "群聊话题测试"
        assert len(json.loads(saved.history)) == 8
        other = await manager.get_conversation(two.conversation.user_id, two.conversation.cid)
        assert other.title == "引用话题 q2"
        provider.text_chat.assert_awaited_once()
        # Real scheduler and native Lark send paths, with only SDK I/O mocked.
        from unittest.mock import patch

        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.utils.metrics import Metric

        plugin.config["auto_topic_title"] = False
        sent_parents = {}

        async def reply(request):
            assert request.message_id == "q1", "Synthetic IDs must never reach reply API"
            mid = "native-reply-" + str(len(sent_parents))
            sent_parents[mid] = request.message_id
            return types.SimpleNamespace(success=lambda: True)

        bot.im.v1.message.areply = reply
        seen_extras = []

        class RouteStage:
            async def process(self, current):
                seen_extras.append(current.get_extra("conversation_continuation_v1"))
                await plugin.interaction_entry(current)
                if not current.is_stopped():
                    await plugin.waiting(current)

        class GenerateStage:
            async def process(self, current):
                assert current.message_obj.message_id == "q1"
                cid = await manager.get_curr_conversation_id(current.unified_msg_origin)
                assert cid == one.conversation.cid
                conv = await manager.get_conversation(current.unified_msg_origin, cid)
                req = ProviderRequest(prompt="button input", conversation=conv)
                await plugin.request(current, req)
                assert not current.is_stopped()
                assert "ou_bob" in req.prompt
                seen_extras.append(current.get_extra("conversation_continuation_v1"))
                await manager.update_conversation(
                    current.unified_msg_origin,
                    cid,
                    history=json.loads(conv.history)
                    + [
                        {"role": "user", "content": req.prompt},
                        {"role": "assistant", "content": "interaction answer"},
                    ],
                )

        class SendStage:
            async def process(self, current):
                await LarkMessageEvent.send(current, current.plain_result("native answer"))
                current._create_streaming_card = AsyncMock(return_value="card-kit-1")
                current._update_streaming_text = AsyncMock()
                current._close_streaming_mode = AsyncMock()

                async def chunks():
                    yield current.plain_result("stream answer")

                await current.send_streaming(chunks())
                seen_extras.append(current.get_extra("conversation_continuation_v1"))

        from astrbot.core.star.star_handler import star_handlers_registry

        handler = star_handlers_registry.get_handler_by_full_name(
            plugin_module.QuoteTopics.interaction_entry.__module__ + ".interaction_entry"
        )
        # Use the real registry to catch decorator order dropping priority.
        if handler is None:
            handler = next(
                h
                for h in star_handlers_registry
                if h.handler == plugin_module.QuoteTopics.interaction_entry
            )
        assert handler.extras_configs["priority"] == 10000
        assert not plugin_module.ContinuationEventFilter().filter(event("plain"), {})

        scheduler = PipelineScheduler.__new__(PipelineScheduler)
        scheduler.stages = [RouteStage(), GenerateStage(), SendStage()]
        contract = {
            "version": 1,
            "source": "card_interaction",
            "event_id": "offline-click-1",
            "origin_message_id": "q1",
        }
        click = event("synthetic-offline-click", user="ou_bob")
        click.set_extra("conversation_continuation_v1", contract)
        with patch.object(Metric, "upload", new=AsyncMock()):
            await asyncio.create_task(scheduler.execute(click))
            await asyncio.sleep(0)
            if plugin.cleanups:
                await asyncio.gather(*list(plugin.cleanups))
        assert len(sent_parents) == 2
        assert seen_extras == [contract, contract, contract]
        assert click.message_obj.message_id == "synthetic-offline-click"
        assert not plugin.locks

        # Actual core follow-up capture must run only AFTER the early handler's
        # physical-chat lock releases the previous B generation.
        from unittest.mock import Mock

        from astrbot.core.pipeline.process_stage import follow_up

        entered, release = asyncio.Event(), asyncio.Event()
        busy_event = event("busy-B", user="ou_bob")
        fake_runner = types.SimpleNamespace(
            run_context=types.SimpleNamespace(context=types.SimpleNamespace(event=busy_event)),
            request_stop=lambda: None,
            follow_up=Mock(side_effect=AssertionError("Interaction captured into B")),
        )

        async def busy_pipeline():
            await plugin.waiting(busy_event)
            follow_up.register_active_runner(busy_event.unified_msg_origin, fake_runner)
            entered.set()
            try:
                await release.wait()
            finally:
                follow_up.unregister_active_runner(busy_event.unified_msg_origin, fake_runner)

        busy = asyncio.create_task(busy_pipeline())
        await entered.wait()
        queued = event("synthetic-queued", user="ou_bob")
        queued.set_extra("conversation_continuation_v1", {**contract, "event_id": "queued"})

        async def queued_pipeline():
            await plugin.interaction_entry(queued)
            assert not queued.is_stopped()
            assert follow_up.try_capture_follow_up(queued) is None
            assert (
                await manager.get_curr_conversation_id(queued.unified_msg_origin)
                == one.conversation.cid
            )

        queued_task = asyncio.create_task(queued_pipeline())
        await asyncio.sleep(0.01)
        assert not queued_task.done()
        fake_runner.follow_up.assert_not_called()
        release.set()
        await asyncio.gather(busy, queued_task)
        await asyncio.sleep(0)
        if plugin.cleanups:
            await asyncio.gather(*list(plugin.cleanups))
        assert not plugin.locks

        # Reopen the index before quoting actual replies: no process-local bridge.
        await plugin.terminate()
        plugin = plugin_module.QuoteTopics(
            types.SimpleNamespace(conversation_manager=manager),
            dict(enabled=True, enable_group=True, group_all=True),
        )
        fetched = []

        async def get_message(request):
            mid = request.message_id
            fetched.append(mid)
            assert mid in sent_parents, "Never query synthetic interaction IDs"
            message = Message(
                {
                    "message_id": mid,
                    "chat_id": "oc_test",
                    "parent_id": sent_parents[mid],
                    "sender": {"sender_type": "app", "id_type": "app_id", "id": "cli_test"},
                }
            )
            return types.SimpleNamespace(
                success=lambda: True, data=types.SimpleNamespace(items=[message])
            )

        bot.im.v1.message.aget = get_message
        for number, mid in enumerate(sent_parents):
            resumed = await turn(event("after-interaction-" + str(number), mid))
            assert resumed.conversation.cid == one.conversation.cid
        assert fetched == list(sent_parents)

        # A second, distinct card action also routes to A and remains quotable.
        second_click = event("synthetic-offline-click-2", user="ou_bob")
        second_click.set_extra(
            "conversation_continuation_v1", {**contract, "event_id": "offline-click-2"}
        )
        assert (await turn(second_click)).conversation.cid == one.conversation.cid
        duplicate = event("new-envelope-same-action", user="ou_bob")
        duplicate.set_extra("conversation_continuation_v1", contract)

        async def duplicate_pipeline():
            await plugin.waiting(duplicate)
            assert duplicate.is_stopped()

        await asyncio.create_task(duplicate_pipeline())
        await asyncio.sleep(0)
        assert not plugin.locks
        print(
            "PASS: real AstrBot 4.28.0 + Lark SDK + SQLite; new/resume/shared/restart/auto-title/card-interaction/native-send; no network"
        )
    finally:
        await plugin.terminate()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        root.cleanup()
