"""execute_terminal 工具的严苛单元测试。

覆盖 ``ExecuteTerminalTool.execute`` 的核心分支与其依赖（detect_dangerous_command /
_resolve_workdir / _render_content / tool_success / tool_error），并针对已修复的
真实 bug 设计对抗性用例：

- P0-2 修复：退出码 / 超时 / 截断此前对模型不可见（content 仅含原始输出）。
  修复后这些结构性事实必须并入 ``content`` 的机器可读标记，使模型得以区分
  「成功但无输出」与「失败但无 stderr」。
- 超时强杀改为 ``status="error"`` 且 ``retryable=True``，明确告知命令未正常结束。
- 危险命令 / 工作目录边界的既有行为不回归。
- to_definition / build_execute_terminal_definition 契约。

测试用 ``FakeBackend`` 替换 ``create_backend``，不派生真实子进程。
"""

import pytest

from app.tools.tool_handler.execute_terminal import (
    ExecuteTerminalTool,
    build_execute_terminal_definition,
)
from app.tools.tool_handler.terminal.execution_result import ExecutionResult


class FakeBackend:
    """可预设结果的虚拟执行后端，避免测试中派生真实子进程。"""

    def __init__(self, result: ExecutionResult) -> None:
        self._result = result

    def execute(
        self, command: str, cwd: str, timeout: float, output_sink=None
    ) -> ExecutionResult:
        return self._result


def _patch_backend(monkeypatch, result: ExecutionResult) -> None:
    """把 execute_terminal.create_backend 替换为返回预设结果的虚拟后端。"""

    backend = FakeBackend(result)
    monkeypatch.setattr(
        "app.tools.tool_handler.execute_terminal.create_backend",
        lambda _name: backend,
    )


@pytest.fixture
def tool() -> ExecuteTerminalTool:
    return ExecuteTerminalTool()


# --------------------------------------------------------------------------- #
# P0-2 修复：退出码 / 截断对模型可见
# --------------------------------------------------------------------------- #


def test_exit_code_zero_with_output_visible(tool, context, monkeypatch):
    """退出码 0 且命令有输出时，content 必须前缀 [exit_code=0]。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="hello world", exit_code=0, truncated=False, timed_out=False),
    )
    obs = tool.execute("echo hello world", execution_context=context)

    assert obs.status == "success"
    assert obs.content.startswith("[exit_code=0]\n")
    assert obs.content.endswith("hello world")


def test_nonzero_exit_code_visible_to_model(tool, context, monkeypatch):
    """核心回归：exit 3 无 stderr 时，修复前 content 为空无法区分成败；
    修复后 content 前缀 [exit_code=3]，模型能识别失败。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="", exit_code=3, truncated=False, timed_out=False),
    )
    obs = tool.execute("exit 3", execution_context=context)

    assert obs.status == "success"
    # 关键：非零退出码必须显式出现在模型可见文本中（修复前 content 为空串）。
    assert obs.content == "[exit_code=3]"


def test_success_with_no_output_marks_zero_exit(tool, context, monkeypatch):
    """退出码 0 且无输出时，content 仅含 [exit_code=0] 标记。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="", exit_code=0, truncated=False, timed_out=False),
    )
    obs = tool.execute("true", execution_context=context)

    assert obs.status == "success"
    assert obs.content == "[exit_code=0]"


def test_truncated_output_marks_truncation(tool, context, monkeypatch):
    """输出被截断时必须出现 [output truncated] 标记。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="partial", exit_code=0, truncated=True, timed_out=False),
    )
    obs = tool.execute("slow --stream", execution_context=context)

    assert obs.status == "success"
    assert "[output truncated]" in obs.content
    assert "[exit_code=0]" in obs.content


# --------------------------------------------------------------------------- #
# P0-2 修复：超时强杀改为 error + retryable
# --------------------------------------------------------------------------- #


def test_timeout_kill_returns_error_retryable(tool, context, monkeypatch):
    """超时强杀（exit_code=-1, timed_out=True）此前被误判为 success；
    修复后应返回 error 且 retryable=True，明确告知命令未正常结束。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="half done", exit_code=-1, truncated=True, timed_out=True),
    )
    obs = tool.execute("sleep 999", execution_context=context, timeout=1)

    assert obs.status == "error"
    assert obs.retryable is True
    # 被截断的半截输出仍随 error 回传，但明确标注超时。
    assert "timeout" in obs.reason.lower()
    assert "[exit_code=-1]" in obs.content
    assert "[output truncated]" in obs.content


# --------------------------------------------------------------------------- #
# _render_content 纯函数单元
# --------------------------------------------------------------------------- #


def test_render_content_with_output():
    out = ExecuteTerminalTool._render_content(
        "abc", ExecutionResult(output="abc", exit_code=0, truncated=False, timed_out=False)
    )
    assert out == "[exit_code=0]\nabc"


def test_render_content_empty_output():
    out = ExecuteTerminalTool._render_content(
        "", ExecutionResult(output="", exit_code=2, truncated=False, timed_out=False)
    )
    assert out == "[exit_code=2]"


def test_render_content_truncated_only():
    out = ExecuteTerminalTool._render_content(
        "x", ExecutionResult(output="x", exit_code=0, truncated=True, timed_out=False)
    )
    assert out == "[exit_code=0][output truncated]\nx"


# --------------------------------------------------------------------------- #
# 危险命令 / 工作目录边界（既有行为不回归）
# --------------------------------------------------------------------------- #


def test_dangerous_command_blocked(tool, context):
    """灾难级命令（rm -rf /）必须被硬拒，且不派生后端。"""

    obs = tool.execute("rm -rf /", execution_context=context)

    assert obs.status == "error"
    assert "blocked" in obs.error.lower()


def test_workdir_escape_rejected(tool, context, tmp_path, monkeypatch):
    """工作目录逃逸 workspace 根应被拒（不触达后端）。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="", exit_code=0, truncated=False, timed_out=False),
    )
    obs = tool.execute("echo hi", workdir="..", execution_context=context)

    assert obs.status == "error"
    assert "workspace root" in obs.reason.lower()


def test_unsafe_workdir_chars_rejected(tool, context, monkeypatch):
    """含注入字符的 workdir 应被拒。"""

    _patch_backend(
        monkeypatch,
        ExecutionResult(output="", exit_code=0, truncated=False, timed_out=False),
    )
    obs = tool.execute("echo hi", workdir="a;b", execution_context=context)

    assert obs.status == "error"
    assert "shell-injection" in obs.reason.lower()


# --------------------------------------------------------------------------- #
# 定义契约
# --------------------------------------------------------------------------- #


def test_to_definition_contract(tool):
    definition = tool.to_definition()

    assert definition.name == "execute_terminal"
    assert definition.permission == "execute_terminal"
    assert definition.args_model.__name__ == "ExecuteTerminalArgs"
    assert definition.handler == tool.execute
    assert definition.resource_keys == ("shell",)
    assert definition.execution_mode == "process"
    assert definition.display.expand_layout == "terminal"
    # 展示契约：无 result_summary_template（exit_code 已并入 content）。
    assert definition.display.verb == "执行命令"
    assert definition.display.icon == "terminal"


def test_build_execute_terminal_definition_factory():
    definition = build_execute_terminal_definition()
    assert definition.name == "execute_terminal"
