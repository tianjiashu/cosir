"""真实 FastAPI 响应序列化回归；直接调用路由无法发现运行时类型引用缺失。"""

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.service import depends


def test_snapshot_and_history_use_real_storage_over_http(backend_env, tmp_path):
    workspace = depends.get_workspace_crud().create("http-contract", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "snapshot-contract")
    client = TestClient(app)
    response = client.get(f"/tasks/{task.id}/assistant/state")
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "snapshot-contract"
    assert response.json()["messages"] == []
    page = client.get(f"/tasks/{task.id}/assistant/messages?end=0")
    assert page.status_code == 200, page.text
    assert page.json() == {"messages": [], "messageOffset": 0}


@pytest.mark.parametrize("presentation", ["activity", "standalone", "quiet"])
def test_nested_tool_parts_serialize_through_actual_routes(presentation):
    """服务替身控制载荷；路由、依赖解析和 Pydantic 响应序列化保持真实。"""
    from app.assistant_transport.assistant_transport_api import get_assistant_transport_service

    state = empty_snapshot()
    state["messages"] = [
        {
            "id": "assistant-1",
            "role": "assistant",
            "turn": 1, "inherited": False,
            "outcome": None,
            "source": None,
            "parts": [
                {
                    "type": "tool-call",
                    "toolCallId": "denied",
                    "toolName": "write_file",
                    "presentation": presentation,
                    "status": "failed",
                    "error": "本轮禁用",
                }
            ],
        }
    ]

    class Source:
        def get_state(self, task_id):
            return state

        def get_history_page(self, task_id, end):
            return {"messages": state["messages"], "messageOffset": 0}

    app.dependency_overrides[get_assistant_transport_service] = Source
    try:
        client = TestClient(app)
        for path in ["state", "messages?end=1"]:
            response = client.get(f"/tasks/1/assistant/{path}")
            assert response.status_code == 200, response.text
            part = response.json()["messages"][0]["parts"][0]
            assert part["presentation"] == presentation
            assert part["status"] == "failed"
    finally:
        del app.dependency_overrides[get_assistant_transport_service]


def test_snapshot_json_schema_resolves_runtime_types():
    from pydantic import TypeAdapter

    from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot

    schema = TypeAdapter(ConversationStateSnapshot).json_schema()
    assert schema["$defs"]["ConversationStateToolCallPart"]["properties"]["presentation"][
        "enum"
    ] == [
        "activity",
        "standalone",
        "quiet",
    ]
