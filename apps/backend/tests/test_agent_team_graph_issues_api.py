from fastapi.testclient import TestClient

import app.api.agent_teams_api  # 注册路由
from app.app import app


def test_graph_issues_endpoint_locates_every_problem_in_an_unfinished_draft() -> None:
    # 草稿允许空字段与未填完的转移；接口只回答图结构问题，不做字段格式校验，也不保存。
    response = TestClient(app).post("/configuration/agent-teams/graph-issues", json={
        "start_node_id": "plan",
        "nodes": [
            {
                "node_id": "plan", "name": "", "agent_id": "",
                "statuses": [{"name": "done", "color": "#3B82F6"}],
            },
            {"node_id": "code", "statuses": [{"name": "done"}]},
        ],
        "transitions": [
            {"from_node_id": "plan", "status": "done", "target_node_id": "code"},
            {"from_node_id": "plan", "status": "oops", "target_node_id": "END"},
        ],
    })

    assert response.status_code == 200
    assert [
        (issue["code"], issue["node_id"], issue["status"], issue["transition_index"])
        for issue in response.json()["issues"]
    ] == [
        ("undeclared_status", "plan", "oops", 1),
        ("status_without_transition", "code", "done", None),
        ("dead_end_node", "plan", None, None),
        ("dead_end_node", "code", None, None),
    ]
    assert response.json()["issues"][0]["message"] == (
        "节点“plan”没有声明状态“oops”，请添加该状态或修改转移。"
    )


def test_graph_issues_endpoint_returns_empty_list_for_a_complete_graph() -> None:
    response = TestClient(app).post("/configuration/agent-teams/graph-issues", json={
        "start_node_id": "plan",
        "nodes": [{"node_id": "plan", "statuses": [{"name": "done"}]}],
        "transitions": [{"from_node_id": "plan", "status": "done", "target_node_id": "END"}],
    })

    assert response.json() == {"issues": []}
