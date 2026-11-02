# 开发说明

运行时复用 AstrBot 已安装的飞书 SDK 和 Python 标准库，无额外依赖。

本机仅执行不运行插件的静态检查：

```sh
ruff check .
ruff format --check .
```

单元及组件集成检查均在服务器 AstrBot 4.29.4 镜像的断网隔离容器执行，不启动第二个机器人实例：

```sh
python -B -m unittest discover -s tests -v
python -B tests/integration_offline.py
```

集成检查使用临时数据目录、真实对话管理器、SQLite 和飞书 SDK，并禁止网络连接。它验证组件协作，不替代完整消息管道与线上模型验收。

- [实现与兼容约束](ARCHITECTURE.md)
- [功能验收清单](ACCEPTANCE.md)

发布安装包可使用 `git archive`；`.gitattributes` 会排除开发资料和测试文件。

- [发布维护说明](RELEASING.md)
