"""工具测试的共享 fixture。

只提供两类基础设施：临时 workspace 根目录，以及以其为边界的
``ToolExecutionContext``。不承载任何断言逻辑或工具构造，避免测试间隐式耦合。
当前被 write_file / patch / execute_terminal 等工具测试复用。
"""

from pathlib import Path

import pytest

from app.tools.schemas import ToolExecutionContext


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """返回本次测试的隔离 workspace 根目录（pytest tmp_path）。"""

    return tmp_path


@pytest.fixture
def context(workspace: Path) -> ToolExecutionContext:
    """构造以临时 workspace 为边界的工具执行上下文。"""

    return ToolExecutionContext(
        task_id="t-task",
        workspace_id="t-ws",
        workspace_root=workspace,
        run_id="t-turn",
    )
