# 阶段记录与协作安排

建议将约 13 个有效工作小时分配为：环境与模型探针 1 小时；命令行 Agent 与用例 3 小时；确认、持久化和幂等 3 小时；API 与界面 2 小时；Docker、真实评测和演示准备 4 小时。这是工作分配估算，不是已经投入的时间记录。

| 阶段 | 已实现与关键文件 | 验证方式 | 需要理解的设计 |
| --- | --- | --- | --- |
| 1 环境和模型 | `pyproject.toml`、`uv.lock`、`.env.example`、`scripts/probe_model.py` | 已复用 RAG 模型配置；WSL 真实工具调用探针通过 | 模型兼容性必须真实调用验证；不能用 mock 结果代替 |
| 2 命令行受理 | `cli.py`、`llm.py`、`graph.py`、`domain.py`、`data/`；调试前写好 `evals/cases.json` | mock 模型驱动真实 LangGraph 的查询、歧义、追问、草案及六轮上限 | 模型提取语义，服务端提供台账/规则事实并决定派单字段 |
| 3 确认和存储 | `approval`/`execute` 节点、`store.py`、`service.py` | SQLite、独立进程重启、并发重复确认、建单提交后 checkpoint 前故障恢复 | interrupt 恢复会重跑节点；草案绑定和业务唯一键必须独立保证 |
| 4 API 和界面 | `api.py`、`ui.py`、`tests/test_api.py`、`tests/test_ui.py` | HTTP 测试；AppTest 通过真实 HTTP 操作界面，仅模型脚本化 | UI 不直接访问图或数据库；批准只发送服务端草案版本 |
| 5 交付验证 | `Dockerfile`、`compose.yaml`、`scripts/container_smoke.py`、`scripts/evaluate.py`、`docs/demo.md` | 实际执行结果见 `reports/validation.md` | 容器验证、mock 集成测试和真实模型业务评测是三种不同证据 |

历史配置复用基线为6/12；本轮已修复状态/事实依赖、合法工具批次与组合设备查询，114测试通过。原12真实评测单次12/12、另四条不同表述单次4/4；容器经项目级中继打通，真实探针和HTTP正常确认/补充确认/取消三场景通过。历史失败见 `reports/model_api_result.md`，本轮改动、指标口径和局限见 `reports/repair_report.md`，各类证据分开报告。
