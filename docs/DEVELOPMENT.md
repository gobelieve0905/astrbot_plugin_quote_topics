# 开发说明

运行时复用 AstrBot 已安装的飞书 SDK 和 Python 标准库，无额外依赖。

在仓库根目录运行本地检查：

```sh
python -B -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

在安装了 AstrBot 4.28.0 的隔离环境运行组件集成检查：

```sh
python -B tests/integration_offline.py
```

集成检查使用临时数据目录、真实对话管理器、SQLite 和飞书 SDK，并禁止网络连接。它验证组件协作，不替代完整消息管道与线上模型验收。

- [实现与兼容约束](ARCHITECTURE.md)
- [功能验收清单](ACCEPTANCE.md)

发布安装包可使用 `git archive`；`.gitattributes` 会排除开发资料和测试文件。
