"""Agent Team 终态原因。"""

from enum import Enum


class AgentTeamRunEndReason(str, Enum):
    """Agent Team 写入数据库的稳定终态原因码。

    原因码只描述业务分类，不承载异常正文或面向用户的展示文案；详细诊断由结构化日志
    记录，展示文案由 API/前端按原因码映射。
    """

    SUPERSEDED_BY_NEW_PREVIEW = "superseded_by_new_preview"
    TEAM_START_FAILED = "team_start_failed"
    TRANSITION_NOT_FOUND = "transition_not_found"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    NEXT_NODE_START_FAILED = "next_node_start_failed"
    NODE_RUN_FAILED = "node_run_failed"
    NODE_RUN_CANCELLED = "node_run_cancelled"
    NODE_OUTPUT_INVALID = "node_output_invalid"
    IMPLICIT_COMPLETION_MISSING = "implicit_completion_missing"
    CANCELLED = "cancelled"
    RUNTIME_RESTARTED = "runtime_restarted"

    def __str__(self) -> str:
        """返回可持久化的稳定原因码。"""

        return self.value
