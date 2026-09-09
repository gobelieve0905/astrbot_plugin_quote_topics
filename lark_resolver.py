"""Recover replies through authenticated Lark message ancestry, without send hooks."""

from __future__ import annotations

import asyncio

from .routing import CannotRestore, field


async def fetch_message(event, mid):
    from lark_oapi.api.im.v1 import GetMessageRequest

    request = GetMessageRequest.builder().message_id(mid).build()
    response = await event.bot.im.v1.message.aget(request)
    if not response.success():
        raise CannotRestore("无法读取被引用消息；请检查飞书读取消息权限，或确认消息未被删除。")
    items = field(response.data, "items") or []
    if len(items) != 1 or field(items[0], "message_id") != mid or field(items[0], "deleted", False):
        raise CannotRestore("飞书没有返回对应的引用消息，无法恢复话题。")
    return items[0]


async def resolve(index, scope, event, mid, fetch=fetch_message):
    cached = index.lookup(scope, mid)
    if cached:
        return cached
    chat_id = field(event.message_obj.raw_message, "chat_id")
    if not chat_id:
        raise CannotRestore("消息缺少飞书聊天编号，无法验证引用归属。")
    seen = []

    # Only traverse this bot's own replies; never promote an unregistered
    # human message or follow root_id (which can skip unrelated messages).
    async def walk():
        nonlocal mid
        for _ in range(8):
            if mid in seen:
                break
            item = await fetch(event, mid)
            sender = field(item, "sender")
            id_type = field(sender, "id_type")
            app_id = field(field(event.bot, "_config"), "app_id")
            own_sender = (id_type == "open_id" and field(sender, "id") == event.get_self_id()) or (
                id_type == "app_id" and app_id and field(sender, "id") == app_id
            )
            if (
                field(item, "chat_id") != chat_id
                or field(sender, "sender_type") != "app"
                or not own_sender
            ):
                raise CannotRestore("引用消息没有本会话的话题关联，或无法验证为当前机器人的回复。")
            parent = field(item, "parent_id")
            if not parent:
                break
            seen.append(mid)
            topic = index.lookup(scope, parent)
            if topic:
                for reply in seen:
                    index.bind(scope, reply, topic, "reply")
                return topic
            mid = parent
        raise CannotRestore(
            "找不到引用消息的话题关联；安装前的旧消息或主动发送的消息可能无法恢复。"
        )

    return await asyncio.wait_for(walk(), timeout=12)
