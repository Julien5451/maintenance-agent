import json
import sqlite3
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .diagnostics import safe_model_error
from .domain import Intake
from .facts import collect_facts, missing_fields, required_question
from .llm import SYSTEM, build_model, restrict_tools

MAX_TOOL_ROUNDS = 6


class ToolValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class State(TypedDict, total=False):
    task_id: str
    messages: Annotated[list[BaseMessage], add_messages]
    status: str
    question: str | None
    reason: str | None
    draft: dict | None
    work_order: dict | None
    tool_rounds: int
    model_calls: int
    resolved_asset_id: str | None
    rules_key: list | None
    unmatched_reason: str | None
    approved: bool
    facts: dict
    verified_asset: dict | None


class Workflow:
    def __init__(self, settings, catalog, store, model=None):
        self.settings, self.catalog, self.store, self.model = settings, catalog, store, model
        self.connection = sqlite3.connect(
            settings.database_dir / "checkpoints.sqlite", check_same_thread=False, timeout=30
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        g = StateGraph(State)
        g.add_node("reconcile", self.reconcile)
        g.add_node("agent", self.agent)
        g.add_node("tools", self.tools)
        g.add_node("wait_info", self.wait_info)
        g.add_node("approval", self.approval)
        g.add_node("execute", self.execute)
        g.add_edge(START, "reconcile")
        g.add_conditional_edges(
            "reconcile",
            lambda s: {"running": "agent", "question": "wait_info"}.get(s["status"], END),
        )
        g.add_conditional_edges("agent", lambda s: "tools" if s["status"] == "running" else END)
        g.add_conditional_edges(
            "tools",
            lambda s: {
                "running": "agent",
                "question": "wait_info",
                "awaiting_confirmation": "approval",
            }.get(s["status"], END),
        )
        g.add_conditional_edges(
            "wait_info", lambda s: "reconcile" if s["status"] == "running" else END
        )
        g.add_conditional_edges("approval", lambda s: "execute" if s.get("approved") else END)
        g.add_edge("execute", END)
        self.graph = g.compile(checkpointer=self.saver)

    def automatic_asset(self, state, query, source):
        result = self.catalog.lookup_asset(query)
        self.store.event(
            state["task_id"],
            "automatic_tool",
            tool_name="lookup_asset",
            source=source,
            args={"query": query},
            result_summary=json.dumps(result, ensure_ascii=False),
            status="ok",
        )
        return result

    def asset_fields(self, result):
        status = result.get("status") or (
            "found" if result["count"] == 1 else "ambiguous" if result["count"] > 1 else "not_found"
        )
        fields = {
            "resolved_asset_id": None,
            "verified_asset": None,
            "rules_key": None,
            "unmatched_reason": None,
            "reason": None,
        }
        if status == "found":
            fields.update(
                resolved_asset_id=result["matches"][0]["asset_id"],
                verified_asset=result["matches"][0],
            )
        elif status in {"ambiguous", "conflict"}:
            candidates = result.get("candidates") or result["matches"]
            detail = "；".join(
                f"{a['asset_id']} {a['name']}（{a['location']}）" for a in candidates
            )
            fields.update(
                status="question", question="设备信息有歧义或冲突，请澄清编号和位置：" + detail
            )
        else:
            fields.update(
                status="unmatched",
                reason="unmatched_asset",
                unmatched_reason="unmatched_asset",
                question="设备台账无匹配，无法受理；未创建工单",
            )
        return fields

    def reconcile(self, state):
        old = state.get("facts", {})
        facts = collect_facts(state["messages"], self.catalog)
        fields = {"facts": facts, "status": "running", "question": None, "reason": None}
        identity_changed = old.get("identity_query") != facts["identity_query"]
        intake_changed = any(old.get(k) != facts.get(k) for k in ("fault_summary", "stopped"))
        if intake_changed or identity_changed:
            fields.update(rules_key=None, draft=None, unmatched_reason=None)
        # Follow-up identity/position changes are verified before the model runs.
        # Initial lookup is still an agent tool call; no model requests are hidden here.
        legacy_resume = not old and state.get("tool_rounds", 0) > 0
        if legacy_resume or (
            old and facts["user_message_count"] > old.get("user_message_count", 0)
        ):
            if identity_changed or not state.get("resolved_asset_id"):
                fields.update(resolved_asset_id=None, verified_asset=None)
                if facts["identity_query"]:
                    result = self.automatic_asset(
                        state, facts["identity_query"], "graph_prerequisite"
                    )
                    fields.update(self.asset_fields(result))
        self.store.event(
            state["task_id"],
            "fact_update",
            status="ok",
            facts=facts,
            device_revalidation=bool(old and identity_changed),
            rules_invalidated=intake_changed or identity_changed,
        )
        if fields["status"] != "running":
            self.transition(state, fields["status"], reason=fields.get("reason"))
        return fields

    @staticmethod
    def legal_tools(state):
        missing = missing_fields(state)
        if "asset" in missing:
            return (
                ["lookup_asset"] if state.get("facts", {}).get("identity_query") else ["ask_user"]
            )
        if missing:
            return ["ask_user"]
        key = state.get("rules_key")
        if (
            not key
            or key[0] != state.get("resolved_asset_id")
            or key[2] != state["facts"]["stopped"]
        ):
            return ["lookup_rules"]
        return ["propose_draft"]

    def transition(self, state, status, **fields):
        self.store.event(
            state["task_id"],
            "status_change",
            previous=state.get("status"),
            status=status,
            **{k: v for k, v in fields.items() if k == "reason"},
        )
        return {"status": status, **fields}

    def agent(self, state):
        if state.get("tool_rounds", 0) >= MAX_TOOL_ROUNDS:
            return self.transition(state, "limit_reached", reason="已达到6轮工具交互上限，任务结束")
        if self.model is None and not self.settings.model_ready:
            return self.transition(state, "error", reason="模型配置缺失；没有执行真实模型调用")
        started = time.perf_counter()
        calls = state.get("model_calls", 0) + 1
        try:
            if self.model is None:
                self.model = build_model(self.settings)
            context = "\n服务端当前状态（事实含用户原文来源；不要重复询问已知事实）：" + json.dumps(
                {
                    "facts": state.get("facts"),
                    "resolved_asset_id": state.get("resolved_asset_id"),
                    "rules_key": state.get("rules_key"),
                    "missing_fields": missing_fields(state),
                    "legal_tools": self.legal_tools(state),
                },
                ensure_ascii=False,
            )
            model = restrict_tools(self.model, self.legal_tools(state))
            message = model.invoke([SystemMessage(SYSTEM + context), *state["messages"]])
            if not isinstance(message, AIMessage):
                raise TypeError("model must return AIMessage")
            self.store.event(
                state["task_id"],
                "model_call",
                status="ok",
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            if not message.tool_calls or message.invalid_tool_calls:
                return {
                    **self.transition(state, "error", reason="模型未返回工具调用，任务结束"),
                    "model_calls": calls,
                    "messages": [message],
                }
            return {"messages": [message], "model_calls": calls, "status": "running"}
        except Exception as exc:
            # Provider exception strings may include credentials, request URLs or headers.
            self.store.event(
                state["task_id"],
                "model_call",
                status="error",
                error_type=type(exc).__name__,
                diagnostic=safe_model_error(exc),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            return {
                **self.transition(state, "error", reason="模型调用失败，请检查配置或服务状态"),
                "model_calls": calls,
            }

    def tools(self, state):
        update = {"status": "running", "tool_rounds": state.get("tool_rounds", 0) + 1}
        messages = []
        calls = state["messages"][-1].tool_calls
        for call in calls:
            name, args = call["name"], call["args"]
            before = {**state, **update}
            rejection_code = None
            try:
                if update["status"] != "running":
                    raise ValueError("本轮任务已暂停或结束，剩余工具调用不执行")
                if len(calls) > 1:
                    raise ToolValidationError(
                        "parallel_batch", "每轮只允许一个工具调用；请按legal_tools逐步执行"
                    )
                if name == "ask_user" and not missing_fields(before):
                    raise ToolValidationError(
                        "redundant_question",
                        "设备、故障、停机情况均已知；不要再次追问，按legal_tools继续",
                    )
                if name not in self.legal_tools(before):
                    raise ToolValidationError(
                        "illegal_action",
                        "当前前置条件未满足；只允许 " + ", ".join(self.legal_tools(before)),
                    )
                result, fields = self.dispatch(name, args, before)
                update.update(fields)
                status = "ok"
            except Exception as exc:
                # Validation errors are local; never echo provider exceptions or arbitrary args.
                result = {
                    "error": str(exc)
                    if isinstance(exc, ToolValidationError)
                    else "参数或事实校验失败；请使用服务端facts和legal_tools",
                    "type": type(exc).__name__,
                    "legal_tools": self.legal_tools(before),
                    "missing_fields": missing_fields(before),
                }
                status = "rejected"
                rejection_code = getattr(exc, "code", "validation")
            summary = json.dumps(result, ensure_ascii=False)
            self.store.event(
                state["task_id"],
                "tool_call",
                tool_name=name,
                args=args,
                result_summary=summary[:4000],
                status=status,
                rejection_code=rejection_code,
            )
            messages.append(ToolMessage(content=summary, tool_call_id=call["id"], name=name))
        if update["status"] != "running":
            self.transition(state, update["status"], reason=update.get("reason"))
        return {**update, "messages": messages}

    def dispatch(self, name, args, state):
        if name == "lookup_asset":
            if set(args) != {"query"} or not isinstance(args["query"], str):
                raise ValueError("invalid query")
            query = "".join(args["query"].casefold().split())
            human_text = "\n".join(
                str(m.content) for m in state["messages"] if isinstance(m, HumanMessage)
            )
            grounded = "".join(human_text.casefold().split())
            reference = state.get("facts", {}).get("identity_query") or ""
            reference_query = "".join(reference.casefold().split())
            if not query or (
                query not in grounded
                and query != reference_query
                and args["query"] != state.get("resolved_asset_id")
            ):
                raise ValueError("查询词必须来自用户提供的设备事实，不得猜测编号")
            result = self.catalog.lookup_asset(args["query"])
            reference = state.get("facts", {}).get("identity_query")
            if reference and reference.casefold() != args["query"].strip().casefold():
                # Do not let a shortened model query hide conflicting user identity evidence.
                canonical = self.automatic_asset(state, reference, "user_evidence_consistency")
                if canonical.get("status") in {"ambiguous", "conflict", "not_found"}:
                    result = {
                        **canonical,
                        "model_query": args["query"],
                        "evidence_query": reference,
                    }
                elif result.get("status") == "found" and (
                    canonical["matches"][0]["asset_id"] != result["matches"][0]["asset_id"]
                ):
                    raise ToolValidationError(
                        "identity_conflict",
                        "查询设备与当前用户身份事实不一致，请使用当前identity_query",
                    )
            return result, self.asset_fields(result)
        if name == "lookup_rules":
            if (
                set(args) != {"asset_id", "category", "stopped"}
                or type(args["stopped"]) is not bool
            ):
                raise ValueError("invalid rule args")
            if args["asset_id"] != state.get("resolved_asset_id"):
                raise ToolValidationError("asset_unverified", "先通过lookup_asset核实当前设备")
            if args["stopped"] is not state.get("facts", {}).get("stopped"):
                raise ToolValidationError(
                    "ungrounded_stopped",
                    "停机值必须等于用户消息确认的facts.stopped；模型不能自行改变",
                )
            result = self.catalog.lookup_rules(**args)
            fields = {
                "rules_key": [args["asset_id"], args["category"], args["stopped"]],
                "unmatched_reason": "no_matching_rule" if not result["count"] else None,
            }
            if not result["count"]:
                fields.update(
                    status="unmatched",
                    reason="no_matching_rule",
                    question="无匹配分派规则，无法自动派单；未创建工单",
                )
            return result, fields
        if name == "propose_draft":
            intake = Intake.model_validate(args)
            if intake.stopped is not state.get("facts", {}).get("stopped"):
                raise ToolValidationError("ungrounded_stopped", "草案停机值与用户事实不一致")
            if intake.asset_id != state.get("resolved_asset_id") or [
                intake.asset_id,
                intake.category,
                intake.stopped,
            ] != state.get("rules_key"):
                raise ValueError("必须先查询并唯一确认设备及相应规则")
            intake = intake.model_copy(update={"fault_summary": state["facts"]["fault_summary"]})
            draft = self.catalog.make_draft(state["task_id"], intake).model_dump()
            self.store.save_draft(state["task_id"], draft)
            return {"draft": draft, "requires_confirmation": True}, {
                "draft": draft,
                "status": "awaiting_confirmation",
                "question": None,
            }
        if name == "ask_user":
            question = args.get("question", "").strip()
            if set(args) != {"question"} or not question or len(question) > 1000:
                raise ValueError("invalid question")
            actual = required_question(state)
            return {
                "question": actual,
                "requested_question": question,
                "missing_fields": missing_fields(state),
            }, {"status": "question", "question": actual}
        if name == "report_unmatched":
            reason = state.get("unmatched_reason")
            if not reason:
                raise ValueError("没有零匹配的查询依据")
            explanation = (
                "设备台账无匹配，无法受理"
                if reason == "unmatched_asset"
                else "无匹配分派规则，无法自动派单"
            )
            return {"reason": reason, "message": explanation}, {
                "status": "unmatched",
                "reason": reason,
                "question": explanation,
            }
        raise ValueError("工具未授权")

    def wait_info(self, state):
        answer = interrupt(
            {"kind": "provide_info", "question": state["question"], "simulated": True}
        )
        if answer["action"] == "cancel":
            return self.transition(state, "cancelled")
        return {
            **self.transition(state, "running"),
            "question": None,
            "messages": [HumanMessage(content=answer["text"])],
        }

    def approval(self, state):
        answer = interrupt({"kind": "approval", "draft": state["draft"], "simulated": True})
        if answer["action"] == "cancel":
            return {**self.transition(state, "cancelled"), "approved": False}
        if answer["draft_id"] != state["draft"]["draft_id"]:
            raise ValueError("草案不匹配")
        return {**self.transition(state, "approved"), "approved": True}

    def execute(self, state):
        if not state.get("approved"):
            raise ValueError("执行节点需要批准")
        order = self.store.create_work_order(state["task_id"], state["draft"])
        self.store.event(
            state["task_id"],
            "tool_call",
            tool_name="create_work_order",
            args={"task_id": state["task_id"], "draft": state["draft"]},
            result_summary=json.dumps({"order_id": order["order_id"], "simulated": True}),
            status="ok",
        )
        return {**self.transition(state, "completed"), "work_order": order}

    def close(self):
        self.connection.close()
