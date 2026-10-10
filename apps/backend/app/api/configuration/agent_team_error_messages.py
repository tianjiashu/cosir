"""把 Agent Team 配置领域校验错误转换为可供本机用户修改的提示。"""

from pydantic import ValidationError


def agent_team_configuration_error_message(error: Exception) -> str:
    """将 Team 配置领域异常或 Pydantic 字段错误映射为中文提示。"""

    if isinstance(error, ValidationError):
        messages = [
            _validation_issue_message(item)
            for item in error.errors(include_context=True)
        ]
        return "\n".join(dict.fromkeys(messages))

    message = str(error)
    graph_message = _graph_issue_message(message)
    if graph_message:
        return graph_message
    if message.startswith("Team 节点引用的 child Agent 不可用:"):
        agent_id = message.partition(":")[2].strip()
        return f"节点引用的子 Agent“{agent_id}”当前不可用，请选择当前作用域中的子 Agent。"
    if message == "Team ID 不可变":
        return "已保存的 Team ID 不能修改。"
    return "Agent Team 配置未能保存，请检查 Team 信息、节点状态和转移设置。"


def _validation_issue_message(issue: dict[str, object]) -> str:
    location = issue.get("loc", ())
    field = str(location[-1]) if isinstance(location, tuple) and location else ""
    error_type = str(issue.get("type", ""))
    field_name = {
        "team_id": "Team ID",
        "name": "Team 名称",
        "description": "用途说明",
        "max_runs": "最大轮数",
        "start_node_id": "入口节点",
        "nodes": "Team 节点",
        "node_id": "节点 ID",
        "agent_id": "执行子 Agent",
        "statuses": "业务状态",
        "transitions": "状态转移",
        "from_node_id": "转移来源节点",
        "status": "转移状态",
        "target_node_id": "转移目标节点",
    }.get(field, "Team 配置")

    if error_type == "value_error":
        context = issue.get("ctx")
        detail = str(context.get("error", "")) if isinstance(context, dict) else ""
        graph_message = _graph_issue_message(detail)
        if graph_message:
            return graph_message
        if field == "statuses" and "duplicates" in detail:
            return "同一节点内的业务状态不能重复。"
        if field == "statuses" and "blank" in detail:
            return "业务状态不能为空。"
        return f"{field_name}不符合要求，请检查后重试。"
    if error_type in {"missing", "string_too_short"}:
        return f"{field_name}不能为空。"
    if error_type == "string_too_long":
        return f"{field_name}内容过长，请缩短后重试。"
    if error_type == "string_pattern_mismatch":
        return f"{field_name}格式不符合要求，请检查字母、数字和下划线等字符。"
    if error_type == "int_parsing":
        return "最大轮数必须填写整数。"
    if error_type == "greater_than_equal":
        return "最大轮数至少为 10。"
    if error_type == "too_short":
        return "Team 至少需要一个节点。" if field == "nodes" else f"{field_name}至少需要一项。"
    if error_type == "too_long":
        return f"{field_name}数量超过允许范围。"
    return f"{field_name}不符合要求，请检查后重试。"


def _graph_issue_message(message: str) -> str | None:
    if "Team must contain at least one node" in message:
        return "请至少添加一个 Team 节点。"
    if "node_id must be unique" in message:
        return "存在重复的节点 ID，请为每个节点设置不同的 ID。"
    if "start_node_id" in message and "not a declared node" in message:
        return "入口节点不存在，请从当前节点中选择一个入口。"
    if "reserved as the terminal target" in message:
        return "节点 ID“END”是保留名称，请为该节点更换 ID。"
    if "unknown source node" in message:
        return "有状态转移引用了不存在的来源节点，请选择有效节点。"
    if "unknown target node" in message:
        return "有状态转移引用了不存在的目标节点，请选择有效节点或 END。"
    if "multiple ambiguous transitions" in message:
        return "同一节点的同一业务状态只能设置一条转移，请合并或删除重复转移。"
    if "is not allowed by node" in message:
        return "有状态转移使用了来源节点未定义的业务状态，请调整状态或转移。"
    if "statuses without transitions" in message:
        return "有业务状态尚未设置转移目标，请为每个状态连到节点或 END。"
    if "unreachable nodes" in message:
        return "有节点无法从入口到达，请调整状态转移连接。"
    if "cannot reach the terminal END target" in message:
        return "有节点无法通过状态转移到达 END，请补充结束路径。"
    if "only allowed as a transition target" in message:
        return "END 只能作为转移目标，不能作为来源节点。"
    return None
