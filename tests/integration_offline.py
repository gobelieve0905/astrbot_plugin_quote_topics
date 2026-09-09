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
        print(
            "PASS: real AstrBot 4.28.0 + Lark SDK + SQLite; new/resume/shared/restart; no network"
        )
    finally:
        await plugin.terminate()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        root.cleanup()
