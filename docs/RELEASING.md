# 发布维护说明

参考 [AstrBot 官方插件发布说明](https://docs.astrbot.app/dev/star/plugin-publish.html) 和 [市场格式规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)。发布前先将已检查的代码推送到 GitHub，再通过 AstrBot Cloud 提交审核；准备好安装包不代表已上架。

- 保持 `name`、`author` 稳定：当前包身份为 `gobelieve/astrbot_plugin_quote_topics`，仓库所有者为 `gobelieve0905`，二者无需相同。不要为了匹配 GitHub 用户名而修改既有插件身份。
- `version` 与提交的版本及安装包一致；`astrbot_version` 必须匹配代码中的版本检查，当前只支持4.28.0；`support_platforms` 仅列 `lark`。
- 简介须保留默认关闭、指定范围与卡片接入条件，不宣称兼容未验证的平台、版本或任意卡片插件。
- 按 [开发说明](DEVELOPMENT.md) 运行离线检查，并按 [验收清单](ACCEPTANCE.md) 记录验证范围。自动测试不能替代任意第三方插件的真实端到端验收。
- 使用 `git archive --format=zip --prefix=astrbot_plugin_quote_topics/ --output=dist/astrbot_plugin_quote_topics-v0.3.0.zip HEAD` 打包。后续版本同步修改文件名，避免上传旧包。
- 包内须包含全部运行时 Python 文件、`metadata.yaml`、`_conf_schema.json`、README、协议、升级说明、排查说明、CHANGELOG 和 LICENSE。
- 检查相对链接、配置默认值、许可证及包体积；官方要求 ZIP 不超过16MB。不要包含凭据、个人数据、数据库、Git 元数据或开发缓存。
- 本地提交说明使用简体中文。发布、部署和用户数据迁移分别确认状态，不以推送成功代替生产验收。
