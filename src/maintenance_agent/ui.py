"""Streamlit HTTP client. Business state and decisions remain in FastAPI."""

import os

import httpx
import streamlit as st

st.set_page_config(page_title="设备报修流转 · 模拟演示", page_icon="🛠️", layout="wide")
st.title("制造设备报修与工单流转智能体")
st.warning("模拟数据演示：设备、班组、规则和工单均为虚构；本系统只分类、派单和流转。")

STATUS_LABELS = {
    "question": "等待补充信息",
    "awaiting_confirmation": "等待人工确认",
    "completed": "已创建工单",
    "cancelled": "已取消",
    "unmatched": "无法匹配",
    "limit_reached": "已达到工具交互上限",
    "error": "处理异常",
}


def call_api(method: str, path: str, payload: dict | None = None):
    try:
        response = httpx.request(
            method, f"{api_base.rstrip('/')}{path}", json=payload, timeout=360, trust_env=False
        )
        if response.is_error:
            try:
                detail = response.json().get("detail", "请求失败")
            except ValueError:
                detail = "后端返回了无法读取的错误响应"
            st.error(f"请求未完成（{response.status_code}）：{detail}")
            return None
        return response.json()
    except httpx.TimeoutException:
        st.error("请求超时。请按任务 ID 刷新状态，避免重复提交同一报修。")
    except httpx.HTTPError:
        st.error("无法连接 FastAPI。请检查 API 地址和后端启动状态。")
    return None


def remember(task: dict | None):
    if task:
        st.session_state["task"] = task
        st.session_state["task_id"] = task["task_id"]
        st.rerun()


with st.sidebar:
    st.header("任务与连接")
    api_base = st.text_input("FastAPI 地址", os.getenv("API_BASE_URL", "http://127.0.0.1:8000"))
    if st.button("检查后端"):
        result = call_api("GET", "/health")
        if result is not None:
            st.json(result)
    st.divider()
    with st.form("resume_task"):
        resume_id = st.text_input("恢复已有任务 ID", value=st.session_state.get("task_id", ""))
        resume = st.form_submit_button("读取 / 刷新任务")
    if resume and resume_id.strip():
        remember(call_api("GET", f"/tasks/{resume_id.strip()}"))
    st.caption("记录任务 ID 后，即使后端或浏览器重启，也可以恢复待确认任务。")
    if st.button("查询已创建工单"):
        orders = call_api("GET", "/work-orders")
        if orders is not None:
            st.session_state["orders"] = orders

with st.form("new_task"):
    report_text = st.text_area(
        "自然语言报修",
        placeholder="例如：EQ-001 机械异响，已经停机。",
        max_chars=4000,
        height=100,
    )
    create = st.form_submit_button("提交报修", type="primary")
if create:
    if len(report_text.strip()) < 2:
        st.error("请填写报修描述。")
    else:
        with st.spinner("正在核对模拟台账与分级派单规则…"):
            remember(call_api("POST", "/tasks", {"text": report_text}))

task = st.session_state.get("task")
if task:
    st.divider()
    status = task.get("status", "error")
    title_col, refresh_col = st.columns([5, 1])
    with title_col:
        st.subheader(STATUS_LABELS.get(status, status))
        st.caption(f"任务 ID：{task['task_id']}")
    with refresh_col:
        if st.button("刷新状态"):
            remember(call_api("GET", f"/tasks/{task['task_id']}"))
    metric_col, calls_col = st.columns(2)
    metric_col.metric("工具交互轮数 / 上限", f"{task.get('tool_rounds', 0)} / 6")
    calls_col.metric("模型调用次数", task.get("model_calls", 0))

    question = task.get("question")
    if question:
        st.info(question)
    if task.get("reason") and task["reason"] != question:
        st.info(task["reason"])
    if status == "question":
        with st.form("provide_info"):
            extra_text = st.text_input("补充设备、位置、故障或停机信息", max_chars=4000)
            provide = st.form_submit_button("提交补充信息")
        if provide:
            if len(extra_text.strip()) < 2:
                st.error("请填写补充信息。")
            else:
                with st.spinner("正在继续当前任务…"):
                    remember(
                        call_api(
                            "POST",
                            f"/tasks/{task['task_id']}/responses",
                            {"action": "provide_info", "text": extra_text},
                        )
                    )

    draft = task.get("draft")
    if draft:
        st.subheader("工单草案 · 模拟数据")
        st.table(
            {
                "字段": [
                    "设备 ID",
                    "位置",
                    "故障摘要",
                    "停机情况",
                    "优先级",
                    "负责班组",
                    "规则 ID",
                ],
                "内容": [
                    draft.get("asset_id", ""),
                    draft.get("location", ""),
                    draft.get("fault_summary", ""),
                    "已停机" if draft.get("stopped") else "未停机",
                    draft.get("priority", ""),
                    draft.get("team_id", ""),
                    "、".join(draft.get("rule_ids", [])),
                ],
            }
        )
        with st.expander("查看规则依据与草案版本", expanded=True):
            evidence = task.get("rule_evidence", [])
            if evidence:
                st.json(evidence)
            else:
                st.write("依据规则：", "、".join(draft.get("rule_ids", [])))
            st.caption(f"服务端草案版本：{draft['draft_id']}")

    if status == "awaiting_confirmation" and draft:
        st.write("请核对以上草案。确认将创建模拟工单，取消将结束任务。")
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("确认并创建工单", type="primary", use_container_width=True):
            with st.spinner("正在确认并保存工单…"):
                remember(
                    call_api(
                        "POST",
                        f"/tasks/{task['task_id']}/responses",
                        {"action": "confirm", "draft_id": draft["draft_id"]},
                    )
                )
        if cancel_col.button("取消任务", use_container_width=True):
            remember(call_api("POST", f"/tasks/{task['task_id']}/responses", {"action": "cancel"}))
    elif status == "question":
        if st.button("取消当前报修"):
            remember(call_api("POST", f"/tasks/{task['task_id']}/responses", {"action": "cancel"}))

    order = task.get("work_order")
    if order:
        st.success(f"模拟工单已保存：{order.get('order_id', '未知编号')}")
        with st.expander("查看最终工单"):
            st.json(order)
    elif status == "cancelled":
        st.success("任务已取消，未创建工单。")

    with st.expander("工具调用与状态记录", expanded=False):
        events = task.get("events", [])
        if not events:
            st.write("暂无记录。")
        for event in events:
            label = event.get("tool_name") or event.get("event_type", "记录")
            st.markdown(f"**{event.get('seq', '')} · {label}**")
            st.json(event)

if "orders" in st.session_state:
    with st.expander("已创建工单 · 模拟数据", expanded=True):
        st.json(st.session_state["orders"])
