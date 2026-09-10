"""Platform-neutral scope and opt-in rules."""

from __future__ import annotations

import json


class CannotRestore(Exception):
    pass


def field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def scope_of(event) -> str:
    group = event.get_group_id()
    parts = [
        event.get_platform_id(),
        event.get_self_id(),
        "group" if group else "private",
        group or event.get_sender_id(),
    ]
    if not all(isinstance(p, str) and p for p in parts):
        raise CannotRestore("平台未提供完整的会话身份，无法安全管理话题。")
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def enabled(config, event) -> bool:
    if not config.get("enabled", False):
        return False
    platforms = config.get("platform_ids", [])
    if platforms and event.get_platform_id() not in platforms:
        return False
    group = event.get_group_id()
    kind = "group" if group else "private"
    peer = group or event.get_sender_id()
    if not config.get(f"enable_{kind}", False):
        return False
    if peer in config.get(f"excluded_{kind}_ids", []):
        return False
    return config.get(f"{kind}_all", False) or peer in config.get(f"{kind}_ids", [])


def quote_id(event) -> str | None:
    # Lark may fail to construct Reply when fetching quoted content fails.
    # Never interpret such a reply as an unquoted new-topic request.
    parent = field(event.message_obj.raw_message, "parent_id")
    ids = {
        str(field(c, "id"))
        for c in event.get_messages()
        if c.__class__.__name__ == "Reply" and field(c, "id")
    }
    if parent:
        ids.add(str(parent))
    if len(ids) > 1:
        raise CannotRestore("这条消息包含不一致的引用目标，无法恢复话题。")
    if ids:
        return ids.pop()
    if any(c.__class__.__name__ == "Reply" for c in event.get_messages()):
        raise CannotRestore("引用缺少消息编号，无法恢复话题。")
    return None


CONTINUATION_KEY = "conversation_continuation_v1"
_ABSENT = object()


def continuation(event):
    """Only trusted producer-supplied extras can request interaction routing."""
    value = event.get_extra(CONTINUATION_KEY, _ABSENT)
    if value is _ABSENT:
        return None
    if not isinstance(value, dict):
        raise CannotRestore("卡片续聊元数据必须是结构化对象。")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise CannotRestore("不支持的卡片续聊协议版本。")
    if value.get("source") != "card_interaction":
        raise CannotRestore("不支持的续聊来源。")
    for name in ("event_id", "origin_message_id"):
        ident = value.get(name)
        if (
            not isinstance(ident, str)
            or not ident.strip()
            or ident != ident.strip()
            or len(ident) > 512
            or any(ord(char) < 32 for char in ident)
        ):
            raise CannotRestore("卡片续聊编号字段缺失或格式错误。")
    actor = event.get_sender_id()
    if not isinstance(actor, str) or not actor.strip():
        raise CannotRestore("交互事件缺少实际操作人身份。")
    # Snapshot the contract before waiting; do not retain a mutable producer dict.
    return value["event_id"], value["origin_message_id"], actor
