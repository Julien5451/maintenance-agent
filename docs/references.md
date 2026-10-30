# 参考来源与设计取舍

查阅日期：2026-10-30。以下是模式参考，未将参考项目整段源码复制到本项目。所有设备、班组、规则和工单业务数据均为模拟数据。

## Agents From Scratch

参考仓库：[langchain-ai/agents-from-scratch](https://github.com/langchain-ai/agents-from-scratch)。为便于复核，阅读了固定提交 [`603fc7a4ac6119004f43894395e504a1fefcc6c0`](https://github.com/langchain-ai/agents-from-scratch/commit/603fc7a4ac6119004f43894395e504a1fefcc6c0) 的以下文件；该提交用作参考快照，不声称它是上游当前最新版本。

| 文件 | 观察到的模式 | 本项目的取舍 |
| --- | --- | --- |
| [基础 Agent](https://github.com/langchain-ai/agents-from-scratch/blob/603fc7a4ac6119004f43894395e504a1fefcc6c0/src/email_assistant/email_assistant.py) | 显式定义模型节点、工具执行节点、条件边；将工具结果带回模型 | 保持小型显式 StateGraph；只用单 Agent，设置工具轮数上限 |
| [Human-in-the-loop](https://github.com/langchain-ai/agents-from-scratch/blob/603fc7a4ac6119004f43894395e504a1fefcc6c0/src/email_assistant/email_assistant_hitl.py) | 用 `interrupt` 暂停需要人工审阅的工具调用；接受时使用已有参数，忽略时不执行 | 将已校验草案保存后暂停；批准恢复只消费服务端草案；写工单放在独立执行节点 |

本项目不迁移邮件分类、日历、长期记忆、子 Agent 或外部邮件服务。报修语义由模型提取，设备与流转事实由模拟台账和规则校验。

上游采用 [MIT License](https://github.com/langchain-ai/agents-from-scratch/blob/603fc7a4ac6119004f43894395e504a1fefcc6c0/LICENSE)，版权声明为 `Copyright (c) 2026 LangChain, Inc.`。若以后复制或实质改编上游代码，应在分发内容中保留完整版权与许可文本，并标记对应文件；仅注明参考链接不能替代该义务。

## LangGraph 与工具调用官方资料

- [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：`interrupt` 的载荷应可序列化；用同一 `thread_id` 和 `Command(resume=...)` 恢复；恢复时整个中断节点重新开始执行。不要吞掉中断异常，或在同一节点里以不稳定顺序安排多个中断。
- [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：内存 checkpointer 随进程退出丢失；SQLite 文件可用于本地持久化演示。
- [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)：SQLite 适配器是独立的 `langgraph-checkpoint-sqlite` 包。`get_state(config)` 可读取当前值、下一节点及中断；同步图使用同步 saver，异步图使用异步 saver。
- [SqliteSaver 官方源码](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py)：定位于轻量同步场景；`from_conn_string` 是上下文管理器，连接需覆盖图使用期。源码演示 `check_same_thread=False`，内部 saver 锁不等于应用层“每任务只能同时恢复一次”的保证。
- [Models / tool calling](https://docs.langchain.com/oss/python/langchain/models#tool-calling)：模型返回工具调用请求，应用负责执行并传回结果；结果的 `tool_call_id` 对应原请求。不同 provider 的强制调用与并行参数需要真实探测。
- [Tools](https://docs.langchain.com/oss/python/langchain/tools)：工具描述和输入 schema 是模型选择工具的接口；使用明确的类型和 Pydantic schema，并在服务端再次验证。

官方在线文档会更新；不直接假定最新文档中的新增参数适用于安装版本。最终兼容性以本项目锁定依赖、实际 import、最小中断恢复验证和集成测试为准。

## 对本项目的具体约束

以下是结合需求作出的工程设计推断，不是上游示例已经提供的业务保证。

1. 模型可调用查询工具，不能直接获得建单能力。设备零匹配、多匹配、缺停机信息、无规则匹配都有显式业务结果。
2. 模型提出的优先级、班组和规则 ID 必须重新对台账与规则验证；提示词本身不作为权限或事实边界。
3. 草案先写入状态，再进入单一确认节点；恢复批准时校验草案版本或摘要。执行节点读取该草案，不再次调用模型生成内容。
4. `create_work_order` 只有已批准路径可达。取消直接终止。数据库唯一请求键保证执行重试仍返回同一工单。
5. 中断前不执行不可重复的副作用；业务写库依旧需要幂等，因为崩溃可能发生在业务提交后、图 checkpoint 提交前。
6. 每任务工具交互计数保存在状态里，补充信息或进程重启不能绕过六轮上限；达到上限后返回明确终止状态。
7. 初版单服务进程加任务锁；SQLite checkpointer 与业务表不天然构成同一事务。并发批准、崩溃重放和重启恢复需要专门验证。

推荐按职责分成 schema、模拟目录/规则、模型适配、graph、存储、service、API、UI、评测和测试几个小模块，避免引入通用插件框架。

## Docker 与 uv

[uv 官方 Docker 指南](https://docs.astral.sh/uv/guides/integration/docker/)：本项目按指南采用官方 GHCR Python 3.12 派生镜像，固定 uv 版本，并用 `uv sync --frozen --no-dev` 安装锁定依赖。初次 Docker Hub 拉取超时后改用 GHCR；构建和运行结果单独记录在验证报告。

[Docker 官方代理构建参数](https://docs.docker.com/build/building/variables/#proxy-arguments)：预定义代理参数无需在 Dockerfile 声明，默认排除构建缓存及 history。当前机器只在确认代理为无认证信息的本机端点后，使用可选构建网络覆盖和预定义参数；项目不保存代理值。

[Microsoft WSL systemd 说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)：仅 systemd 服务不会保持 WSL 实例存活。本机演示保持普通 Ubuntu 会话，不修改全局网络或休眠配置。
