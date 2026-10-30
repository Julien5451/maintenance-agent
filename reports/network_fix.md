# 容器运行时模型网络修复

日期：2026-10-30（Asia/Shanghai）。模型保持 `gpt-4o-mini`；不新建 Key，不改 `.env`、全局网络、防火墙或原数据卷。

## 核验结果与修复

此前 API 容器直连模型：DNS 成功，IPv4 TCP 超时、IPv6 网络不可达。实际主机代理由 **Windows 的 verge-mihomo** 监听，Windows 监听仅在回环地址；WSL 的 `ss` 没有对应监听，但 WSL 可以连接该回环代理。已有代理经过检查：HTTP、无用户名/密码、无查询参数/片段。代理地址和端口未写入项目或报告。

不能把主机 `127.0.0.1` 原样配置给桥接容器：它在容器内代表容器自身。当前采用项目级 TCP 中继：

1. `scripts/runtime_proxy.py --inspect` 读取本项目 Docker network 元数据，写入不含凭证的拓扑报告。
2. `--serve` 仅监听本项目 bridge 网关的 `18081` 端口，只接受该项目子网的连接，然后转发到从当前 WSL 环境读取的原有、无凭证回环代理。它不监听局域网地址，不记录流量或目标地址，保留 TLS 端到端校验。
3. `compose.runtime-proxy.yaml` 只给 API 配置代理别名 `maintenance-proxy.host`，通过动态网关映射到中继；同时为 API/UI 设置 NO_PROXY，包含 `api`、`ui`、localhost 等内部地址。原发布端口仍仅绑定 Windows/WSL 本机，原命名卷保留。

中继连接计数只记录数量，不含请求数据。通过容器真实模型调用时计数增加，证明实际模型 HTTP 客户端使用了运行时代理配置。

## 已执行证据

- [首个修复后容器真实探针](network_fix_probe_20261030T130935762145Z.json)：**passed**，1 次真实调用，2.293 秒，`lookup_asset(query="EQ-001")` 正确，零数据库写入。
- 同一次请求明确带 `parallel_tool_calls=False`，API 接受且返回一条有效工具调用；此结果可供业务修复使用该参数。
- 中继 accepted 计数 0→1，`http_client_used_project_relay=true`。
- [内部 HTTP 检查](network_fix_internal_http.json)：API 和 UI 容器分别访问 `http://api:8000/health` 都返回 200，中继计数不增加。内部 API 通信没有误走代理。
- `network_fix_topology.json` 与 `network_fix_relay_stats.json` 不含主机代理值、模型密钥或代理凭证，但属于本机动态文件，已加入 Git 忽略规则；分别由 `--inspect` 和运行中的 `--serve` 重新生成。固定验证报告保留调用当时的脱敏结果，不依赖这些实时文件作为成功证据。
- 原失败报告 `container_model_probe.json`、`container_network_diagnosis.json` 均保留，没有被新成功报告覆盖。

## 当前机器启动方式

在 WSL 项目目录使用具备 Docker 权限的终端：

```bash
.venv/bin/python scripts/runtime_proxy.py --inspect
```

在普通 WSL 用户终端运行中继，并保持该终端：

```bash
.venv/bin/python scripts/runtime_proxy.py --serve
```

在具备 Docker 权限、同一项目目录的另一终端启动或重建 API/UI：

```bash
export MAINTENANCE_PROXY_GATEWAY="$(python3 -c 'import json; print(json.load(open("reports/network_fix_topology.json"))["gateway"])')"
docker compose -f compose.yaml -f compose.runtime-proxy.yaml up -d --force-recreate --wait api ui
.venv/bin/python scripts/probe_runtime.py
.venv/bin/python scripts/probe_runtime.py --internal-only
```

不要用仅 base Compose 的 force-recreate 覆盖运行时设置。`docker compose restart api` 不会丢失已有容器的代理环境，但代码/环境改变后应使用上述完整文件组合。

`--inspect` / `--serve` 只接受当前已核实形式的无凭证回环 HTTP 代理。若代理环境改变、项目 network 重建或网关变化，先重新 inspect，再启动中继，并从报告重新设置网关；不要照搬旧地址。该 helper 是本机演示的环境适配，不是生产代理服务。

## 修复后镜像的容器端完整业务验证

镜像 `maintenance-agent:local` 已重建为 `sha256:04148e735e3350edd6f12ba30d2f521bc42a59933a85123d4f6dd07036058fb4`，使用原卷和运行时 override 重新创建 API/UI。

- [新镜像单次真实探针](network_fix_probe_20261030T132941084382Z.json)：**passed**，1 次真实调用、1.839 秒，`parallel_tool_calls=False` 接受，中继计数 1→2；零数据库写入。
- [单次完整真实 HTTP 三场景](network_fix_business_20261030T132946738768Z.json)：**3/3 passed**，10 次模型调用、0 工具拒绝、0 重复问句、0 不必要追问尝试、0 六轮耗尽，16.444 秒。
- API/UI 内部 HTTP 在新容器再次检查：均返回 200，中继计数未增加，见 `network_fix_internal_http.json`。

| 场景 | 实际结果 | 模型调用 | 自动工具步骤 | 耗时 |
| --- | --- | --- | --- | --- |
| 正常报修并确认 | 完成，工单 `WO-D791E4E3F144` | 3 | 0 | 4.932 秒 |
| 补充停机信息后确认 | 初始追问，补充后完成，工单 `WO-1020F2BA3BEA` | 4 | 0 | 5.243 秒 |
| 取消 | 已取消，0 工单 | 3 | 0 | 4.760 秒 |

三个任务分别使用纯中文“容器真实测试：正常/补充/取消；模拟数据”标记；run_id 仅在报告中关联 task_id，避免标签被误当设备编号。确认时模型调用数不增加；重复确认仍只有一单；保存工单等于批准前服务端草案。原有 2 个任务的 tasks/drafts/events/work_orders 和 checkpoints/writes 内容摘要全部保持一致，`preexisting_rows_unchanged=true`。仅新增本轮明确标记的 3 个模拟测试任务和 2 张模拟工单。

这些结果来自实际 API 容器、HTTP、真实模型和原命名卷，不是 mock，也不是仅主机直接调用模型。旧失败及首个环境修复探针均保留；没有反复重跑选择最好成绩。
