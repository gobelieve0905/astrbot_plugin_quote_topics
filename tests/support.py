"""Minimal AstrBot doubles: no production credentials, network or model calls."""

import importlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("quote_topics_test_plugin")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package


def module(name):
    return importlib.import_module(f"{package.__name__}.{name}")


class Event:
    def __init__(
        self,
        mid="question",
        group="group",
        user="alice",
        parent=None,
        platform="instance",
        bot="ou_bot",
    ):
        self.mid, self.group, self.user = mid, group, user
        self.platform, self.bot_id = platform, bot
        self.unified_msg_origin = (
            f"{platform}:GroupMessage:{group}" if group else f"{platform}:FriendMessage:{user}"
        )
        self.message_obj = types.SimpleNamespace(
            message_id=mid,
            raw_message=types.SimpleNamespace(parent_id=parent, chat_id=group or f"dm_{user}"),
        )
        self.bot = types.SimpleNamespace(_config=types.SimpleNamespace(app_id="cli_bot"))
        self.extras, self.sent, self.components = {}, [], []
        self.stopped = False

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return "lark"

    def get_self_id(self):
        return self.bot_id

    def get_sender_id(self):
        return self.user

    def get_sender_name(self):
        return "Name " + self.user

    def get_group_id(self):
        return self.group

    def get_messages(self):
        return self.components

    def get_extra(self, key=None, default=None):
        return self.extras if key is None else self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    async def send(self, message):
        self.sent.append(message)


class Manager:
    def __init__(self):
        self.session_conversations, self.conversations = {}, {}

    async def get_curr_conversation_id(self, umo):
        return self.session_conversations.get(umo)

    async def switch_conversation(self, umo, cid):
        self.session_conversations[umo] = cid

    async def new_conversation(self, umo, platform_id=None, persona_id=None, title=None):
        cid = f"topic-{len(self.conversations)}"
        self.conversations[cid] = types.SimpleNamespace(
            cid=cid, user_id=umo, persona_id=persona_id, history="[]", title=title
        )
        self.session_conversations[umo] = cid
        return cid

    async def get_conversation(self, umo, cid):
        return self.conversations.get(cid)

    async def update_conversation(self, umo, conversation_id=None, title=None):
        self.conversations[conversation_id].title = title


def runtime(data_dir):
    from unittest.mock import Mock

    astrbot = types.ModuleType("astrbot")
    astrbot.__version__ = "4.28.0"
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig, api.logger = dict, Mock()
    events = types.ModuleType("astrbot.api.event")
    events.AstrMessageEvent = Event
    events.filter = types.SimpleNamespace(
        **{
            name: lambda *a, **kw: lambda f: f
            for name in [
                "on_waiting_llm_request",
                "on_llm_request",
                "command",
                "event_message_type",
                "custom_filter",
            ]
        }
    )
    events.filter.CustomFilter = object
    events.filter.EventMessageType = types.SimpleNamespace(ALL=object())
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star, star.Context = Star, object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda *a: data_dir)
    core = types.ModuleType("astrbot.core")

    async def remove(*a):
        pass

    core.sp = types.SimpleNamespace(session_remove=remove)
    for m in [astrbot, api, events, star, core]:
        sys.modules[m.__name__] = m
    main = importlib.reload(module("main"))
    config = dict(
        enabled=True, enable_group=True, group_all=True, enable_private=True, private_all=True
    )
    manager = Manager()
    plugin = main.QuoteTopics(
        types.SimpleNamespace(conversation_manager=manager, get_config=lambda **kw: {}), config
    )
    return plugin, manager, main.KEY


async def build_request(manager, event):
    cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
    conversation = await manager.get_conversation(event.unified_msg_origin, cid)
    return types.SimpleNamespace(
        conversation=conversation, contexts=json.loads(conversation.history), prompt="hello"
    )
