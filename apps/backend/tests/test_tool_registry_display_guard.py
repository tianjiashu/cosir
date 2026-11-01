"""注册表守卫：每个工具在前端工具目录表里有条目，产出的展示 kind 都登记在契约文档里。

目的：新增工具时不靠人记得补前端目录（图标、动词、标题规则）或更新契约表格——漏了就在这里失败。
前端目录表是 ``apps/desktop/lib/assistant/tool-catalog.ts``：工具名是封闭集合（``ALL_TOOL_NAMES``），
后端不再下发任何展示外壳，所以这张表就是「工具叫什么、用什么图标」的唯一事实源，由本测试按注册表核对。
"""

import re
from pathlib import Path

import pytest

from app.config.configuration import get_tool_registry
from app.core.tool.schemas.tool_names import ALL_TOOL_NAMES

_CONTRACT = Path("app/core/tool/tool_ui_display_contract.md")
_FRONTEND_CATALOG = Path("../desktop/lib/assistant/tool-catalog.ts")
_DISPLAY_MODULES = [
    *Path("app/tools/display").glob("*.py"),
    *Path("app/core/tool/handler").glob("*/*display*.py"),
]


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


def _frontend_catalog_tools() -> set[str]:
    """目录表里每个条目以 ``  tool_name: {`` 开头（对象字面量的键），按此提取工具名。"""

    source = _FRONTEND_CATALOG.read_text(encoding="utf-8")
    return set(re.findall(r"^  ([a-z][a-z0-9_]*): \{", source, flags=re.M))


@pytest.mark.usefixtures("backend_env")
def test_frontend_catalog_covers_every_registered_tool() -> None:
    registered = {definition.name for definition in get_tool_registry().get_all_definitions()}
    assert registered
    catalog = _frontend_catalog_tools()
    assert registered <= catalog, f"前端工具目录缺少：{sorted(registered - catalog)}"


def test_frontend_catalog_matches_tool_names() -> None:
    assert _frontend_catalog_tools() == set(ALL_TOOL_NAMES)


def test_contract_document_lists_every_tool_and_every_display_kind() -> None:
    table = _contract_table()
    assert set(table) == set(ALL_TOOL_NAMES), "契约文档第 3 节表格须列全全部工具"
    documented = set().union(*table.values())
    for kind in _kinds_in_source():
        assert kind in documented, f"展示 kind `{kind}` 未在契约文档登记"
