"""依赖方向守卫：``app.core`` 不得导入 ``app.tools``。

``core/tool`` 承载工具契约、执行管线与内部工具，``app/tools`` 只放外部工具实现；外部工具可以
依赖 core 的契约，反向导入会把外部工具重新绑回 Agent 内核，破坏后续把工具宿主独立部署的前提。
扫描包含函数内的延迟导入。
"""

import ast
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[1] / "app" / "core"


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def test_core_does_not_import_app_tools() -> None:
    violations = [
        f"{path.relative_to(_CORE_DIR.parent)}:{lineno} imports {module}"
        for path in sorted(_CORE_DIR.rglob("*.py"))
        for lineno, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if module == "app.tools" or module.startswith("app.tools.")
    ]
    assert violations == []
