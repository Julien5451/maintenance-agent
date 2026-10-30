"""Conservative user-evidence extraction, never from assistant/tool-call arguments.

This covers common intake wording. Unrecognised or contradictory facts stay unknown;
semantic fault classification remains the single LLM agent's responsibility.
"""

import re

from langchain_core.messages import HumanMessage

_INSTRUCTION = re.compile(r"系统指令|忽略|绕过审批|无需确认|不用审批|直接(?:创建工单|开单|建单)")
_UNCERTAIN = re.compile(r"不清楚|不知道|不确定|是否|可能|假设|如果|待确认|无法确认")
_STOP_PLANNED = re.compile(
    r"(?:准备|计划|建议|安排|需要|不要|不应|要求|打算|将要|即将|预计).{0,4}(?:停机|停产|停线)"
)
_STOP = re.compile(
    r"(?P<running>尚未停机|没有停机|未停机|没停机|未停产|没有停产|还没停|仍在运行|仍在生产|"
    r"还在运行|还在运转|继续运行|正常运行|运行中|仍在运转|不停机)"
    r"|(?P<stopped>已经停机|已停机|停机了|停机|已经停产|已停产|停产|已停线|停线了|"
    r"已停止运行|停止运行|已经停下|已停下|停下来了|无法运行|不能运行|已经关停|已关停)"
)
_SYMPTOM = re.compile(
    r"异响|噪音|振动|震动|跳闸|告警|报警|卡住|卡死|卡顿|漏油|漏水|渗油|泄漏|"
    r"过热|温度异常|冒烟|无法启动|启动失败|不启动|不转|不动|断电|电压|通信|通讯|"
    r"中断|失灵|失效|不响应|无响应|异常|破损|损坏|掉漆|脱落|磨损|裂纹|松动|"
    r"触摸屏|黑屏|花屏|故障代码|压力不足|压力下降|频繁重启|无法复位"
)


def user_text(text: str) -> str:
    """Only the report before an embedded workflow-override instruction is evidence."""
    return _INSTRUCTION.split(text, maxsplit=1)[0]


def identity_reference(text: str, catalog) -> str | None:
    ids = catalog.extract_asset_ids(text)
    terms = []
    for asset in catalog.assets:
        for term in (asset["name"], *asset["aliases"], asset["location"]):
            if term in text:
                terms.append(term)
    terms += re.findall(r"(?:[A-Za-z]车间)?\d+号工位", text)
    # Preserve unfamiliar names accompanying an ID, rather than dropping them
    # just because the ledger vocabulary cannot recognise them.
    for match in catalog._id_pattern.finditer(text):
        right = text[match.end() :].lstrip(" ：:")
        noun = re.match(r"([\u4e00-\u9fff0-9]{2,}?(?:机器人|车床|机|泵|炉))", right)
        if noun:
            term = noun.group(1)
            if (
                not re.match(
                    r"的|出现|发生|需要|已经|目前|当前|属于|报修|机械|电气|控制|主轴|轴承|电机|PLC",
                    term,
                    re.I,
                )
                and not term.endswith("电机")
                and not _SYMPTOM.search(term)
                and not _STOP.search(term)
            ):
                terms.append(term)
        label = re.match(r"([^，,。；;：:\s（）()]+?)(?=的|出现|发生|[，,。；;\s]|$)", right)
        if label and not right.startswith(("的", "出现", "发生")):
            term = label.group(1)
            if (
                not re.match(
                    r"出现|发生|需要|已经|目前|当前|属于|报修|机械|电气|控制|主轴|轴承|电机|PLC",
                    term,
                    re.I,
                )
                and not _SYMPTOM.search(term)
                and not _STOP.search(term)
            ):
                terms.append(term)
        left = text[: match.start()].rstrip(" （(")
        label = re.search(r"([\u4e00-\u9fff0-9]+(?:机|车床|炉|泵|机器人|设备))$", left)
        if label and not any(left.endswith(term) for term in terms):
            term = re.sub(r"^(?:报修|设备为|设备是|这是|那台|这台)+", "", label.group(1))
            if term and not _SYMPTOM.search(term) and not _STOP.search(term):
                terms.append(term)
    terms += re.findall(r"(?:设备名称|名称)\s*[:：=]\s*([^，,。；;\s]+)", text)
    selected = []
    for term in sorted(set(terms), key=lambda v: (-len(v), v)):
        if not any(term in longer for longer in selected):
            selected.append(term)
    return " ".join([*ids, *selected]) or None


def descriptive_clause(clause, catalog):
    """Accept user descriptions without a closed symptom vocabulary.

    Strip identity, operational status and administrative filler. A substantive
    remaining assertion is evidence, including novel fault wording; this never
    infers an unstated stop value or repairs an ID.
    """
    if re.search(r"无法提供|补充信息|提供信息|更多信息|再试|重试|暂不补充", clause):
        return False
    if re.search(r"优先级|班组|审批|确认|开单|建单|工单", clause) and not _SYMPTOM.search(clause):
        return False
    rest = catalog._id_pattern.sub("", clause)
    for asset in catalog.assets:
        for term in sorted(
            [asset["name"], *asset["aliases"], asset["location"]], key=len, reverse=True
        ):
            rest = rest.replace(term, "")
    rest = re.sub(r"(?:[A-Za-z]车间)?\d+号工位", "", rest)
    rest = _STOP_PLANNED.sub("", rest)
    rest = _STOP.sub("", rest)
    rest = re.sub(
        r"需要登记报修|需要报修|登记报修|报修|更正|而且|实际上|实际|目前|已经|当前|"
        r"这台设备|该设备|设备|这台机|这台|那台|机器|机台|属于|的是|的是|的|是|"
        r"需要|请|另|生产已暂停",
        "",
        rest,
    )
    return len(re.findall(r"[\u4e00-\u9fff]", rest)) >= 2


def collect_facts(messages, catalog) -> dict:
    facts = {
        "identity_query": None,
        "fault_summary": None,
        "stopped": None,
        "stopped_conflict": False,
        "evidence": {},
    }
    user_count = 0
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        user_count += 1
        text = user_text(str(message.content))
        reference = identity_reference(text, catalog)
        if reference:
            facts["identity_query"] = reference
            facts["evidence"]["identity"] = {"message": user_count, "quote": text}
        stops = []
        unknown_stop = False
        symptoms = []
        for sentence in re.split(r"[。；;！!？?\n]", text):
            has_description = False
            for clause in re.split(r"[，,]|但是|不过|但", sentence):
                clause = clause.strip()
                uncertain = bool(_UNCERTAIN.search(clause))
                if uncertain and re.search(r"停机|停产|运行", clause):
                    unknown_stop = True
                if not uncertain:
                    planned = [m.span() for m in _STOP_PLANNED.finditer(clause)]
                    stops.extend(
                        (bool(m.group("stopped")), clause)
                        for m in _STOP.finditer(clause)
                        if not any(start <= m.start() < end for start, end in planned)
                    )
                if (
                    (_SYMPTOM.search(clause) or descriptive_clause(clause, catalog))
                    and not uncertain
                    and not re.search(r"^(?:没有|无|未出现|不是).*(?:故障|告警|异常|异响)", clause)
                ):
                    has_description = True
            if has_description:
                # Keep the sentence intact so commas do not detach negation scope.
                symptoms.append(sentence.strip())
        if stops:
            values = {value for value, _ in stops}
            corrected = bool(re.search(r"更正|现在|目前|实际|其实|改为", text))
            if len(values) == 1 or corrected:
                facts["stopped"] = stops[-1][0]
                facts["stopped_conflict"] = False
                facts["evidence"]["stopped"] = {"message": user_count, "quote": stops[-1][1]}
            else:
                facts.update(stopped=None, stopped_conflict=True)
                facts["evidence"]["stopped"] = {
                    "message": user_count,
                    "quotes": [q for _, q in stops],
                }
        elif unknown_stop:
            facts.update(stopped=None, stopped_conflict=True)
            facts["evidence"]["stopped"] = {"message": user_count, "quote": text}
        if symptoms:
            facts["fault_summary"] = "；".join(symptoms)[:500]
            facts["evidence"]["fault_summary"] = {"message": user_count, "quotes": symptoms}
    facts["user_message_count"] = user_count
    return facts


def missing_fields(state: dict) -> list[str]:
    facts = state.get("facts", {})
    return [
        name
        for name, present in (
            ("asset", bool(state.get("resolved_asset_id"))),
            ("fault_summary", bool(facts.get("fault_summary"))),
            ("stopped", type(facts.get("stopped")) is bool),
        )
        if not present
    ]


def required_question(state: dict) -> str:
    names = {
        "asset": "设备编号或具体位置",
        "fault_summary": "具体故障现象",
        "stopped": "当前是否停机",
    }
    return "请补充或澄清：" + "、".join(names[name] for name in missing_fields(state)) + "。"
