"""依赖方向守卫：终端运行时不依赖工具与应用层，子包之间只向下依赖。

方向：``tool_handler/terminal``（工具）→ ``session`` / ``command`` → ``observation`` → 根上的
``worker_client`` / ``errors``。会话与命令互不依赖；运行时一旦导入工具包，一条命令的事实就会再次被
拆到两层、两个包互相导入。扫描包含函数内的延迟导入。
"""

import ast
from collections.abc import Callable
from pathlib import Path

_RUNTIME = "app.tools.terminal_runtime"
_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "tools" / "terminal_runtime"

# 子包（或根模块）→ 不得导入的模块前缀
_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "session": (f"{_RUNTIME}.command",),
    "command": (f"{_RUNTIME}.session",),
    "observation": (f"{_RUNTIME}.session", f"{_RUNTIME}.command"),
    "worker_client.py": (f"{_RUNTIME}.session", f"{_RUNTIME}.command", f"{_RUNTIME}.observation"),
    "errors.py": (_RUNTIME,),
}


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def _violations(forbidden_for: Callable[[Path], tuple[str, ...]]) -> list[str]:
    return [
        f"{path.relative_to(_RUNTIME_DIR)}:{lineno} imports {module}"
        for path in sorted(_RUNTIME_DIR.rglob("*.py"))
        for lineno, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if any(
            module == prefix or module.startswith(prefix + ".") for prefix in forbidden_for(path)
        )
    ]


def test_runtime_does_not_import_tools_or_application_layers() -> None:
    """运行时只被工具与应用层调用：应用层的回调（如会话变化重投影）在装配时注入。"""
    forbidden = ("app.tools.tool_handler", "app.service", "app.assistant_transport", "app.api")
    assert _violations(lambda _path: forbidden) == []


def test_runtime_subpackages_only_depend_downward() -> None:
    def forbidden_for(path: Path) -> tuple[str, ...]:
        return _FORBIDDEN.get(path.relative_to(_RUNTIME_DIR).parts[0], ())

    assert _violations(forbidden_for) == []
