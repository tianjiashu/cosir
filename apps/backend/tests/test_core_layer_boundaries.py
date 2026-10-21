"""执行内核的依赖方向：整个 ``app.core`` 不依赖 Assistant Transport。

执行事件只定义事实，经 ``EventSink`` 回调交给消费方；workflow 的其余协作者直接使用对话场景的
具体类型（每个只有一个实现，不预留端口）。
"""

import ast
from pathlib import Path

import pytest

_APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("core", ("app.assistant_transport",)),
    ],
)
def test_core_dependency_direction(package: str, forbidden: tuple[str, ...]) -> None:
    violations = sorted(
        f"{path.relative_to(_APP_ROOT)} -> {module}"
        for path in (_APP_ROOT / package).rglob("*.py")
        for module in _imported_modules(path)
        if module.startswith(forbidden)
    )
    assert violations == []
