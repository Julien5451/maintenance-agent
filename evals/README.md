# 预先冻结的任务用例

`cases.json` 在模型调试与真实模型评测前编写，包含 12 条任务及程序可检查的预期结果。所有设备、位置、班组和规则均为**模拟业务数据**。不包含设备维修操作指导。

## 数据约定

- `data/assets.json`：10 台模拟设备。`asset_id` 为唯一键；名称、别名、位置可用于检索；`asset_type` 用于规则适用性校验；`team_ids` 为该设备允许分派的班组台账。
- `data/teams.json`：机械、电气、自动化 3 个模拟班组，唯一键为 `team_id`。
- `data/rules.json`：6 条模拟流转规则，覆盖 `mechanical` / `electrical` / `control` 三类与 `stopped=true/false` 的组合。每条规则只输出优先级和班组，不输出维修步骤。
- 优先级 `P1` / `P2` / `P3` 是本演示自定义分级，`P1` 最高，不代表生产企业的正式标准。外观问题属于 `other`，刻意不配置匹配规则。
- EQ-001、EQ-002 共享别名“数控车床”；只输入这个别名时必须追问，不允许任意选择其中一台。

## 用例与检查口径

每条用例包含 `id`、`name`、`input`、按顺序提交的 `followups`、最终动作 `action` 及 `expected`。`action` 仅允许 `confirm`、`cancel`、`none`。补充信息必须发生在 `question` 状态，确认或取消必须发生在 `awaiting_confirmation` 状态。若模型没有进入预期交互状态，应记录失败，不应绕过接口继续执行。

`expected.status` 是完成所有补充信息和最终动作后的业务状态。预期值包括 `completed`、`cancelled`、`question`、`awaiting_confirmation`、`unmatched`（`expected.reason` 区分 `unmatched_asset` 与 `no_matching_rule`）。`asset_id`、`priority`、`team_id`、`rule_ids` 检查最终服务端草案或已落库工单；无草案时可从已验证的资产状态检查 `asset_id`。`rule_ids` 按集合比较。`work_order_count` 是该任务在 SQLite 中的实际工单条数。

| 用例 | 覆盖内容 | 最终状态 |
|---|---|---|
| C01 | 机械故障、已停机、确认 | completed |
| C02 | 电气故障、未停机、确认 | completed |
| C03 | 控制故障、未停机、确认 | completed |
| C04 | 缺停机信息，补充后确认 | completed |
| C05 | 缺故障摘要，补充后确认 | completed |
| C06 | 设备别名歧义，保持追问 | question |
| C07 | 按位置消除设备歧义 | completed |
| C08 | 未知设备，拒绝编造 | unmatched / unmatched_asset |
| C09 | 无匹配规则，拒绝编造派单依据 | unmatched / no_matching_rule |
| C10 | 人工取消，不创建工单 | cancelled |
| C11 | 用户注入，仍按规则且等待确认 | awaiting_confirmation |
| C12 | 缺设备信息，补充后确认 | completed |

## 运行与如实报告

真实模型评测必须逐条保存原始输入、补充信息、动作、预期与实际字段、各项程序检查、模型调用次数、工具调用次数和耗时；失败也必须保留。模型工具调用验证与评测不得用 mock 结果冒充。没有有效模型凭证时，真实模型评测应标为阻塞或未运行。

mock 集成测试单独报告，至少覆盖未批准不建单、取消不建单、重复确认只建一次、后端重启后恢复待确认任务。真实评测不能代替这些集成测试。

