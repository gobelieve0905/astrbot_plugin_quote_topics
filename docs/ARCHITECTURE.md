# 实现与兼容约束

基线：AstrBot v4.28.0，commit `a412146401426c0cdff8bbefb8627a03da519da8`。

- `main.py`：在高优先级 `on_waiting_llm_request` 中选题，这发生在内置 Agent 构建请求、读取历史与人格之前。不能只在 `on_llm_request` 中改历史，后者发生在请求装饰之后。
- 每个物理群/私聊持有一把插件锁，所有启用请求遵循它；同群不同成员的 UMO 也使用同一把锁。AstrBot EventBus 为每条消息建立独立任务，任务完成回调在历史写入之后恢复原选择并释放锁。
- 不改 `event.unified_msg_origin`，保留原有平台发送地址、配置匹配和工具事件身份。创建话题沿用发起者的 UMO，后续共享恢复校验存储的 owner。
- 低优先级 `on_llm_request` 再次检查 `req.conversation.cid`，并在群聊用户提示中添加 JSON 编码的账号和昵称。内置 Agent 使用绑定的 conversation ID 保存历史。
- AstrBot 会捕获 hook 抛出的异常。因此错误路径必须先 `stop_event()` 再发送提示，不能单靠抛异常阻止调用模型。
- `routing.py`：显式启用范围、复合身份范围、读取 Reply/原始 parent_id。飞书引用内容拉取失败时仍识别原始 parent_id，不把失败当成无引用。
- `lark_resolver.py`：仅回溯当前机器人的已认证消息，最多 8 层、总计 12 秒。每一层检查 chat_id 和发送者，遇到未登记的用户消息立即停止；不能穿过它继承旧话题。仅成功找到本范围内映射后缓存机器人回复。
- `store.py`：SQLite 事务及复合主键保证消息不会被重新绑定到其他话题；数据库版本不认识时拒绝加载。正文不复制，数据在插件代码目录之外。

没有发送方法替换、SDK 全局补丁或卡片插件私有接口依赖。SDK `_config.app_id` 是受版本约束的读取点：用于核验飞书以 `app_id` 表示的应用发送者，缺失时不会跳过身份校验。

## 已知边界

当前锁覆盖标准内置 Agent 请求，不覆盖核心管理指令、绕过管道的外部模型调用、其他插件的任意数据库修改或多进程部署。不要将这些场景标记为已保证隔离。
强制杀进程可能留下临时原生会话选择，但已持久化的消息索引仍可恢复；新消息仍根据引用重新选题。
通过按钮构造的请求可按 [交互约定](../CONTINUATION_PROTOCOL.md) 接入；其他请求修改与额外历史注入仍需单独验证。不得因为能发送卡片就宣传支持它的全部交互。

## 上游依据

- [对话管理器](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/conversation_mgr.py)
- [内置 Agent 管道](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py)
- [Agent 请求构建](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/astr_main_agent.py)
- [逐消息任务生命周期](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/event_bus.py)
- [飞书消息发送](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.0/astrbot/core/platform/sources/lark/lark_event.py)
- [飞书获取指定消息](https://open.feishu.cn/document/server-docs/im-v1/message/get)

## 交互入口

`interaction_entry` 在普通插件处理阶段取得会话锁，早于内置 Agent 的 follow-up 捕获；`waiting` 复用已有绑定。交互记录使用独立表，原提问只参与定位。原始合成编号登记别名，发送期间使用真实原提问作为原生飞书回复目标，完成后恢复。协议与生产端责任见 [约定 v1](../CONTINUATION_PROTOCOL.md)。
