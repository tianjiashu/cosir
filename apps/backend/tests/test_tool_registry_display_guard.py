"""注册表守卫：每个工具都有展示声明与调用标题投影，产出的展示 kind 都登记在契约文档里。

目的：新增工具时不靠人记得补 ``display`` / ``heading`` 或更新契约表格——漏了就在这里失败。
样例参数按各工具 ``args_model`` 的最小合法形状给出，经与准入同一条校验路径（归一 + strict）。
"""

import re
from pathlib import Path

import pytest

from app.config.configuration import get_tool_registry
from app.core.tool.schemas import ToolHeading
from app.core.tool.schemas.tool_names import ALL_TOOL_NAMES
from app.core.tool.tool_heading import project_tool_heading

_CONTRACT = Path("app/core/tool/tool_ui_display_contract.md")
_DISPLAY_MODULES = [
    *Path("app/tools/display").glob("*.py"),
    *Path("app/core/tool/handler").glob("*/*display*.py"),
]

# 每个工具一份最小合法参数；没有参数的工具给空字典。
SAMPLE_ARGUMENTS: dict[str, dict[str, object]] = {
    "read_file": {"path": "app/main.py"},
    "read_image": {"paths": ["a.png", "b.png"]},
    "write_file": {"path": "notes.md", "content": "hi"},
    "patch_write": {"path": "app/main.py", "old_string": "a", "new_string": "b"},
    "apply_patch": {
        "patch": "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    },
    "delete_file": {"path": "old.txt"},
    "move_file": {"source_path": "a.txt", "destination_path": "b.txt"},
    "search_content": {"pattern": "ToolHeading", "path": "app"},
    "find_files": {"pattern": "*.py", "path": "app"},
    "list_directory": {"path": "app"},
    "execute_terminal": {"description": "Run backend unit tests", "command": "pytest -q\necho"},
    "terminal_list": {},
    "terminal_start": {"label": "dev server"},
    "terminal_read": {"session_id": "term_1"},
    "terminal_write": {
        "session_id": "term_1",
        "operation_id": "op-1",
        "input": {"type": "line", "text": "npm test"},
    },
    "terminal_handoff": {"session_id": "term_1", "reason": "enter the sudo password"},
    "terminal_close": {"session_id": "term_1"},
    "web_search": {"query": "local-first desktop agent"},
    "web_extract": {"urls": ["https://example.com/a", "https://example.com/b"]},
    "browser_navigate": {"url": "https://example.com"},
    "browser_snapshot": {},
    "browser_act": {
        "actions": [{"type": "click", "ref": "e1"}, {"type": "scroll", "direction": "down"}]
    },
    "browser_screenshot": {"full_page": True},
    "browser_tabs": {"action": "close", "tab": "t1"},
    "browser_handoff": {"reason": "finish the login"},
    "delegate_task": {"child_agent_id": "reviewer", "agent_name": "Review", "message": "go"},
    "child_agent_send": {"child_task_id": 12, "message": "continue"},
    "child_agent_status": {"child_task_id": 12},
    "wait_messages": {},
    "propose_agent_configuration": {
        "agent_id": "reviewer",
        "role": "Reviewer",
        "description": "Reviews code",
        "system_prompt": "You review code.",
    },
    "propose_agent_team_configuration": {
        "team_id": "dev-team",
        "name": "Dev Team",
        "description": "Builds things",
        "max_runs": 10,
        "start_node_id": "develop",
        "nodes": [
            {
                "node_id": "develop",
                "name": "Development",
                "agent_id": "developer_agent",
                "statuses": [{"name": "done", "color": "#16A34A"}],
            }
        ],
        "transitions": [
            {"from_node_id": "develop", "status": "done", "target_node_id": "END"}
        ],
    },
    "agent_team": {
        "team_id": "dev-team",
        "goal": "ship",
        "node_goals": [{"node_id": "develop", "goal": "write code", "summary": "code"}],
    },
    "mcp": {"action": "call", "server_name": "fs", "tool_name": "read", "arguments": "{}"},
    "skill": {"action": "load", "skill_name": "pdf"},
}

EXPECTED_HEADINGS: dict[str, ToolHeading] = {
    "execute_terminal": ToolHeading(title="Run backend unit tests", target="pytest -q"),
    "search_content": ToolHeading(title="搜索内容", target='"ToolHeading" in app'),
    "move_file": ToolHeading(title="移动文件", target="a.txt → b.txt"),
    "apply_patch": ToolHeading(title="应用补丁", target="x.py"),
    "read_image": ToolHeading(title="查看图片", target="a.png +1"),
    "web_extract": ToolHeading(title="网页正文提取", target="https://example.com/a +1"),
    "terminal_write": ToolHeading(title="输入终端", target="term_1 · npm test"),
    "delegate_task": ToolHeading(title="Review", target="reviewer"),
    "mcp": ToolHeading(title="调用 MCP", target="fs/read"),
    "wait_messages": ToolHeading(title="等待结果", target=None),
}


def _contract_table() -> dict[str, set[str]]:
    """解析契约文档第 3 节表格：``| `tool` | `kind` | ...``，``kind`` 列可写多个或「无」。"""

    rows: dict[str, set[str]] = {}
    for line in _CONTRACT.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        tool = cells[0].strip("`")
        if tool not in ALL_TOOL_NAMES:
            continue
        rows[tool] = set(re.findall(r"`([a-z-]+)`", cells[1]))
    return rows


def _kinds_in_source() -> set[str]:
    kinds: set[str] = set()
    for module in _DISPLAY_MODULES:
        kinds.update(re.findall(r'"kind": "([a-z-]+)"', module.read_text(encoding="utf-8")))
    return kinds


@pytest.mark.usefixtures("backend_env")
def test_every_registered_tool_declares_display_and_projects_a_heading() -> None:
    definitions = get_tool_registry().get_all_definitions()
    assert definitions
    for definition in definitions:
        assert definition.display is not None, definition.name
        assert definition.display.verb and definition.display.icon, definition.name
        assert definition.heading is not None, definition.name
        assert definition.name in SAMPLE_ARGUMENTS, f"缺少 {definition.name} 的样例参数"
        heading = project_tool_heading(definition, SAMPLE_ARGUMENTS[definition.name])
        assert heading is not None, f"{definition.name} 的样例参数通不过 args_model"
        assert heading.title, definition.name
        expected = EXPECTED_HEADINGS.get(definition.name)
        if expected is not None:
            assert heading == expected, definition.name


def test_sample_arguments_cover_every_tool_name() -> None:
    assert set(SAMPLE_ARGUMENTS) == set(ALL_TOOL_NAMES)


def test_contract_document_lists_every_tool_and_every_display_kind() -> None:
    table = _contract_table()
    assert set(table) == set(ALL_TOOL_NAMES), "契约文档第 3 节表格须列全全部工具"
    documented = set().union(*table.values())
    for kind in _kinds_in_source():
        assert kind in documented, f"展示 kind `{kind}` 未在契约文档登记"
