"""AstrBot 4.28.0 quote topics. No dependency on a card plugin."""

from __future__ import annotations

import asyncio
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .lark_resolver import resolve
from .routing import CannotRestore, enabled, quote_id, scope_of
from .store import Index, Topic
from .titles import Titles

KEY = "quote_topics.binding.v1"


class QuoteTopics(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.index = Index(StarTools.get_data_dir("astrbot_plugin_quote_topics") / "index.sqlite3")
        self.locks = {}
        self.cleanups = set()
        self.active = set()
        self.closed = False
        self.titles = Titles(context, config)

    async def refuse(self, event, message):
        # AstrBot catches hook exceptions; stop FIRST so failures cannot fall
        # through to the previous conversation, including send failures.
        event.stop_event()
        await event.send(
            event.plain_result(
                f"引用续聊：{message}\n请引用已登记的消息，或不引用重新提问以新开话题。"
            )
        )

    async def restore_selection(self, umo, cid, previous):
        manager = self.context.conversation_manager
        if await manager.get_curr_conversation_id(umo) != cid:
            return  # Do not undo an explicit external change.
        if previous:
            await manager.switch_conversation(umo, previous)
        else:
            from astrbot.core import sp

            manager.session_conversations.pop(umo, None)
            await sp.session_remove(umo, "sel_conv_id")

    async def cleanup(self, event, binding, task):
        try:
            await self.restore_selection(binding["umo"], binding["topic"].cid, binding["previous"])
        except Exception as exc:
            logger.error("Quote topics selection cleanup failed (%s)", type(exc).__name__)
        finally:
            self.active.discard(task)
            binding["lock"].release()
            entry = self.locks[binding["scope"]]
            entry[1] -= 1
            if entry[1] == 0:
                self.locks.pop(binding["scope"], None)

        if not self.closed and not task.cancelled() and task.exception() is None:
            if enabled(self.config, event):
                self.titles.schedule(binding["topic"], binding["umo"])

    @filter.on_waiting_llm_request(priority=10000)
    async def waiting(self, event: AstrMessageEvent):
        if self.closed or not enabled(self.config, event) or event.get_extra(KEY):
            return
        lock = None
        locked = False
        registered = False
        scope = None
        topic = None
        previous = None
        umo = event.unified_msg_origin
        try:
            if event.get_platform_name() != "lark":
                raise CannotRestore("当前版本仅适配飞书，请将其他平台排除在启用范围外。")
            from astrbot import __version__

            if __version__ != "4.28.0":
                raise CannotRestore("当前版本仅验证 AstrBot 4.28.0，请禁用插件或使用兼容版本。")
            if event.get_extra("provider_request") is not None:
                raise CannotRestore("其他插件接管了本次模型请求，无法保证话题隔离。")
            scope = scope_of(event)
            mid = str(event.message_obj.message_id or "")
            if not mid:
                raise CannotRestore("消息缺少编号，无法登记话题。")
            target = quote_id(event)
            entry = self.locks.setdefault(scope, [asyncio.Lock(), 0])
            entry[1] += 1
            lock = entry[0]
            # The full pipeline task owns this lock, including history writes.
            # Waiting is bounded, but NEVER releases an in-flight owner's lock.
            await asyncio.wait_for(lock.acquire(), timeout=120)
            locked = True
            if self.closed:
                raise CannotRestore("插件正在卸载，请稍后重试。")
            if self.index.lookup(scope, mid):
                event.stop_event()  # Platform duplicate; do not answer twice.
                return
            manager = self.context.conversation_manager
            previous = await manager.get_curr_conversation_id(umo)
            if target:
                topic = await resolve(self.index, scope, event, target)
                conversation = await manager.get_conversation(topic.owner, topic.cid)
                if not conversation or conversation.user_id != topic.owner:
                    raise CannotRestore("对应话题已删除或归属不符，无法恢复。")
                await manager.switch_conversation(umo, topic.cid)
            else:
                # Carry the chosen persona, but never the previous chat history.
                persona_id = None
                if previous:
                    old = await manager.get_conversation(umo, previous)
                    if old and old.user_id == umo:
                        persona_id = old.persona_id
                cid = await manager.new_conversation(
                    umo,
                    event.get_platform_id(),
                    persona_id=persona_id,
                    title="引用话题 " + mid[-12:],
                )
                topic = Topic(cid, umo)
            self.index.bind(scope, mid, topic, "input")
            owner = asyncio.current_task()
            if owner is None:
                raise CannotRestore("无法确认请求生命周期，已停止处理。")
            binding = dict(scope=scope, topic=topic, previous=previous, lock=lock, umo=umo)
            event.set_extra(KEY, binding)
            self.active.add(owner)

            def done(task):
                cleanup = asyncio.create_task(self.cleanup(event, binding, task))
                self.cleanups.add(cleanup)
                cleanup.add_done_callback(self.cleanups.discard)

            owner.add_done_callback(done)
            registered = True
        except asyncio.CancelledError:
            event.stop_event()
            raise
        except TimeoutError:
            await self.refuse(event, "等待当前话题处理或读取引用消息超时，请稍后重试。")
        except CannotRestore as exc:
            await self.refuse(event, str(exc))
        except Exception as exc:
            logger.error("Quote topics routing failed (%s)", type(exc).__name__)
            await self.refuse(event, "话题登记或恢复失败，本次请求已停止。")
        finally:
            if lock is not None and not registered:
                try:
                    if topic is not None:
                        await self.restore_selection(umo, topic.cid, previous)
                finally:
                    if locked:
                        lock.release()
                    entry = self.locks[scope]
                    entry[1] -= 1
                    if entry[1] == 0:
                        self.locks.pop(scope, None)

    @filter.on_llm_request(priority=-10000)
    async def request(self, event: AstrMessageEvent, req):
        if not enabled(self.config, event):
            return
        binding = event.get_extra(KEY)
        if not binding or not req.conversation or req.conversation.cid != binding["topic"].cid:
            await self.refuse(event, "模型请求未绑定到正确话题，可能存在其他会话插件冲突。")
            return
        if event.get_group_id():
            # JSON prevents nickname newlines from masquerading as metadata.
            identity = json.dumps(
                {"id": event.get_sender_id(), "name": event.get_sender_name()}, ensure_ascii=False
            )
            req.prompt = f"[本次群聊发言人 {identity}]\n" + (req.prompt or "")

    @filter.command("话题状态")
    async def status(self, event: AstrMessageEvent):
        state = "已启用" if enabled(self.config, event) else "未启用"
        yield event.plain_result(
            f"引用续聊：本会话{state}。\n"
            "规则：引用续聊，不引用新开。支持 AstrBot 4.28.0 的飞书内置 Agent。\n"
            "恢复机器人回复所属话题需要飞书读取消息权限。"
        )

    async def terminate(self):
        self.closed = True
        # Do not cancel user generations or close their database underneath them.
        active = list(self.active)
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await asyncio.sleep(0)  # allow owner done callbacks to enqueue cleanup
        if self.cleanups:
            await asyncio.gather(*list(self.cleanups), return_exceptions=True)
        await self.titles.close()
        self.index.close()
