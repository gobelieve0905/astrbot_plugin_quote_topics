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
