"""待确认占位结果行 → 冷重建恢复审批卡片的契约测试。

背景：``wait_user`` 在挂起前为每条待决调用写一条占位 ``ToolMessage`` 行，``transport_metadata``
携带卡片载荷与请求（``RuntimeContextManager.add_message``，同 ``tool_call_id`` 命中既有行时原地
覆盖）。冷重建（``ConversationTaskStateRebuilder.build_pair_tool_part``）的 part 动态数据**只**
从该类行的 metadata 读取，因此这条行决定了「进程重启后还能不能看到审批卡片、还能不能作答」。

本模块钉住重启这条路径：有占位行 ⇒ 卡片与表单都在；没有（旧行为）⇒ 只剩一个 ``cancelled`` 空壳。
前端侧对应行为见 ``apps/desktop/tests/unit/agent-team-hitl.test.ts``（读取器从
``display_data.user_input_request`` 取表单）。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
    TransportMetadata,
)

_TOOL_CALL = {
    "name": "agent_team",
    "args": {"team_id": "code-quality"},
    "id": "call-a",
}

_CARD_PAYLOAD: dict[str, Any] = {
    "kind": "agent-team-preview",
    "status": "pending",
    "team_run_id": 17,
    "team_id": "code-quality",
    "name": "交付报告",
    "goal": "交付报告",
    "node_goals": {"develop": "实现"},
    "start_node": "develop",
    "parent_task_id": 3,
    "parent_run_id": 2,
    "workspace_id": 1,
    "configuration": {"team_id": "code-quality"},
    "nodes": [],
    "edges": [],
}

_REQUEST_PAYLOAD: dict[str, Any] = {
    "request_id": "17",
    "request_kind": "agent_team_review",
    "prompt": "确认执行方案",
    "decisions": ["approve", "reject"],
    "draft_schema": "agent-team-review-v1",
    "draft": {"goal": "交付报告"},
}


@pytest.fixture(autouse=True)
def _backend_env(backend_env: None) -> None:
    """冷重建要按工具名取静态展示声明，而工具系统与运行时单例的装配依赖已初始化的主库。"""

    return None


def _rows(metadata: TransportMetadata | None) -> list[ConversationTaskContextRecord]:
    """构造「AI 消息带 tool_calls + 一条结果行」的最小 context。"""

    rows = [
        ConversationTaskContextRecord(
            task_id=3,
            run_id=2,
            message=AIMessage(content="", tool_calls=[dict(_TOOL_CALL)]),
            include_in_context=True,
            sequence=1,
        )
    ]
    if metadata is not None:
        rows.append(
            ConversationTaskContextRecord(
                task_id=3,
                run_id=2,
                message=ToolMessage(
                    content="waiting for the user's decision", tool_call_id="call-a"
                ),
                include_in_context=True,
                sequence=2,
                transport_metadata=metadata,
            )
        )
    return rows


def test_waiting_row_rebuilds_approval_card_with_form() -> None:
    """有占位行：冷重建出「未结算 + 卡片载荷 + 请求」的 part，用户能看到方案也能作答。"""

    parts = ConversationTaskStateRebuilder.build_pair_tool_part(
        _rows(
            TransportMetadata(
                status="running",
                display_data={**_CARD_PAYLOAD, "user_input_request": _REQUEST_PAYLOAD},
                error=None,
            )
        )
    )

    part = parts["call-a"]
    assert part["status"] == "running"
    assert part["display_data"]["kind"] == "agent-team-preview"
    assert part["display_data"]["user_input_request"] == _REQUEST_PAYLOAD


def test_missing_row_leaves_cancelled_shell_without_form() -> None:
    """没有结果行（旧行为）：冷重建按 ``cancelled`` 兜底，卡片内容与表单都拿不到。"""

    parts = ConversationTaskStateRebuilder.build_pair_tool_part(_rows(None))

    part = parts["call-a"]
    assert part["status"] == "cancelled"
    assert part.get("display_data") is None
