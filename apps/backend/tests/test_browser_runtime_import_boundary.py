"""依赖方向守卫：浏览器运行时不依赖工具 handler、传输层与 API。

浏览器工具与应用层（Session 删除、lifespan、HTTP）调运行时，反过来不行；运行时一旦导入工具包，调宿主的
方式就会再次被拆到两层、两边互相导入。运行时经 ``app.service.depends`` 取进程单例是本仓惯例，不在禁止之列。
扫描包含函数内的延迟导入。
"""

import ast
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "tools" / "browser_runtime"
_FORBIDDEN = ("app.tools.tool_handler", "app.assistant_transport", "app.api")


def test_browser_runtime_does_not_import_tools_or_transport() -> None:
    violations = [
        f"{path.name}:{node.lineno} imports {module}"
        for path in sorted(_RUNTIME_DIR.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for module in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
            else []
        )
        if any(module == prefix or module.startswith(prefix + ".") for prefix in _FORBIDDEN)
    ]
    assert violations == []
