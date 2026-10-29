"""把 Agent Team 配置领域校验错误转换为可供本机用户修改的提示。

图结构问题按 :class:`TeamGraphIssueCode` 生成文案，不解析英文描述；保存失败与编辑器
实时提示共用 :func:`team_graph_issue_message`，两处措辞一致。
"""

from pydantic import ValidationError

from app.models.agent_team.configuration.graph_validation import (
    TeamGraphIssue,
    TeamGraphIssueCode,
    TeamGraphValidationError,
)


def agent_team_configuration_error_message(error: Exception) -> str:
    """将 Team 配置领域异常或 Pydantic 字段错误映射为中文提示。"""

    if isinstance(error, ValidationError):
        messages = [
            message
            for item in error.errors(include_context=True)
            for message in _validation_issue_messages(item)
        ]
        return "\n".join(dict.fromkeys(messages))

    message = str(error)
    if message.startswith("Team 节点引用的 child Agent 不可用:"):
        agent_id = message.partition(":")[2].strip()
        return f"节点引用的子 Agent“{agent_id}”当前不可用，请选择当前作用域中的子 Agent。"
    if message == "Team ID 不可变":
        return "已保存的 Team ID 不能修改。"
    return "Agent Team 配置未能保存，请检查 Team 信息、节点状态和转移设置。"


def _validation_issue_messages(issue: dict[str, object]) -> list[str]:
    """图校验错误展开为逐条问题；其余字段错误各对应一条提示。"""

    context = issue.get("ctx")
    cause = context.get("error") if isinstance(context, dict) else None
    if isinstance(cause, TeamGraphValidationError):
        return [team_graph_issue_message(item) for item in cause.issues]
    return [_field_issue_message(issue)]


def _field_issue_message(issue: dict[str, object]) -> str:
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


def team_graph_issue_message(issue: TeamGraphIssue) -> str:
    """把一条图结构问题转换为指向具体节点 / 状态的中文提示。"""

    node = f"节点“{issue.node_id}”"
    status = f"状态“{issue.status}”"
    match issue.code:
        case TeamGraphIssueCode.NO_NODES:
            return "请至少添加一个 Team 节点。"
        case TeamGraphIssueCode.DUPLICATE_NODE_ID:
            return f"节点 ID“{issue.node_id}”重复，请为每个节点设置不同的 ID。"
        case TeamGraphIssueCode.INVALID_START:
            return "入口节点无效，请把一个节点设为入口。"
        case TeamGraphIssueCode.RESERVED_END_ID:
            return "节点 ID“END”是保留名称，请为该节点更换 ID。"
        case TeamGraphIssueCode.END_AS_SOURCE:
            return "END 只能作为转移目标，不能作为来源节点。"
        case TeamGraphIssueCode.UNKNOWN_SOURCE:
            return "有状态转移的来源节点不存在，请删除或改为有效节点。"
        case TeamGraphIssueCode.UNDECLARED_STATUS:
            return f"{node}没有声明{status}，请添加该状态或修改转移。"
        case TeamGraphIssueCode.AMBIGUOUS_TRANSITION:
            return f"{node}的{status}设置了多条转移，同一状态只能交给一个目标。"
        case TeamGraphIssueCode.UNKNOWN_TARGET:
            return f"{node}的{status}指向了不存在的节点，请改为有效节点或 END。"
        case TeamGraphIssueCode.STATUS_WITHOUT_TRANSITION:
            return f"{node}的{status}还没有设置下一步。"
        case TeamGraphIssueCode.UNREACHABLE_NODE:
            return f"{node}从入口出发走不到。"
        case TeamGraphIssueCode.DEAD_END_NODE:
            return f"{node}之后无法走到 END。"
