"""CodeGraphTool 单元测试。

覆盖：参数模型校验、build_codegraph_definition 契约、CLI 未安装、project_path
越界、设备/伪文件拦截、affected 逐项越界、affected 合法多文件命令构造、query 缺
target、CLI 非零（未初始化提示）、成功路径、超时、build_tool_system 注册，以及
json/depth 边界开关。

外部依赖（shutil.which / subprocess.run）一律用 unittest.mock 隔离；路径安全优先用
真实 ProjectPathResolver + 真实临时 workspace 构造，越界/设备用真实 resolver 自然触发。
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.codegraph import (
    CodeGraphTool,
    build_codegraph_definition,
)
from app.tools.tool_models.codegraph_args import CodeGraphArgs


def _make_context(root: Path) -> ToolExecutionContext:
    """构造一个指向临时目录的执行上下文（真实 workspace_root）。"""
    return ToolExecutionContext(task_id="t", workspace_id="w", workspace_root=root)


def _run(action, **kwargs):
    """用真实临时 workspace 构造上下文并调用 handler 的便捷封装。

    kwargs 可带 workspace_root 覆盖；其余透传给 execute。
    """
    ws = kwargs.pop("workspace_root", None)
    if ws is None:
        ws = Path(tempfile.mkdtemp())
    ctx = _make_context(ws)
    return CodeGraphTool().execute(action=action, execution_context=ctx, **kwargs)


# ---------------------------------------------------------------------------
# 1. 参数模型：合法参数通过
# ---------------------------------------------------------------------------
# 测试目的：验证合法 CodeGraphArgs 能正常构造且字段类型正确。
# 可能发现的缺陷类型：字段约束过严/类型注解错误导致合法输入被拒。
def test_args_valid():
    args = CodeGraphArgs(action="explore", target="foo.bar", json=True, depth=3)
    assert args.action == "explore"
    assert args.target == "foo.bar"
    assert args.json is True
    assert args.depth == 3
    assert args.project_path is None


# ---------------------------------------------------------------------------
# 2. 参数模型：未知 action 被拒（Literal 校验）
# ---------------------------------------------------------------------------
# 测试目的：验证不在枚举内的 action 触发 pydantic 校验错误。
# 可能发现的缺陷类型：Literal 约束缺失/拼写错误导致非法 action 被接受。
def test_args_rejects_unknown_action():
    try:
        CodeGraphArgs(action="frobnicate")
    except Exception as exc:  # pydantic 抛 ValidationError
        assert "action" in str(exc)
        return
    raise AssertionError("expected validation error for unknown action")


# ---------------------------------------------------------------------------
# 3. 参数模型：多余字段被拒（extra=forbid）
# ---------------------------------------------------------------------------
# 测试目的：验证 extra="forbid" 拒绝未声明字段。
# 可能发现的缺陷类型：extra 配置失效导致未知字段被静默接受。
def test_args_rejects_extra_field():
    try:
        CodeGraphArgs(action="explore", target="x", bogus="nope")
    except Exception:
        return
    raise AssertionError("expected validation error for extra field")


# ---------------------------------------------------------------------------
# 4. 参数模型：depth 越界被拒（ge=1, le=10）
# ---------------------------------------------------------------------------
# 测试目的：验证 depth 范围约束生效。
# 可能发现的缺陷类型：范围约束缺失导致非法 depth 被接受。
def test_args_rejects_depth_out_of_range():
    for bad in (0, 11):
        try:
            CodeGraphArgs(action="impact", target="x", depth=bad)
        except Exception:
            continue
        raise AssertionError(f"expected validation error for depth={bad}")


# ---------------------------------------------------------------------------
# 5. build_codegraph_definition 返回合法 ToolDefinition
# ---------------------------------------------------------------------------
# 测试目的：验证定义 name/permission/args_model/description 正确且 handler 可调用。
# 可能发现的缺陷类型：定义字段缺失或 handler 不可调用导致集成失败。
def test_build_definition_ok():
    definition = build_codegraph_definition()
    assert definition.name == "codegraph"
    assert definition.permission == "codegraph_query"
    assert definition.args_model is CodeGraphArgs
    assert "explore" in definition.description
    assert callable(definition.handler)
    # handler 可调用（quick smoke：用 mock 的 which 短路到未安装分支）
    with mock.patch.object(shutil, "which", return_value=None):
        obs = definition.handler(action="status", execution_context=_make_context(Path(tempfile.mkdtemp())))
    assert obs.status == "error"
    assert obs.retryable is False


# ---------------------------------------------------------------------------
# 6. CLI 未安装（which -> None）
# ---------------------------------------------------------------------------
# 测试目的：验证 codegraph 不在 PATH 时返回结构化错误且不可重试。
# 可能发现的缺陷类型：未处理 which=None 导致异常/错误 reason 缺失。
def test_cli_not_installed():
    with mock.patch.object(shutil, "which", return_value=None):
        obs = _run("status")
    assert obs.status == "error"
    assert obs.retryable is False
    assert "not installed" in obs.reason.lower()
    assert obs.permission == "codegraph_query"


# ---------------------------------------------------------------------------
# 7. project_path 越界（指向 workspace 外）
# ---------------------------------------------------------------------------
# 测试目的：验证越界 project_path 被 resolver 拒绝且不可重试。
# 可能发现的缺陷类型：containment 校验失效导致跨 workspace 越界被允许。
def test_project_path_outside_workspace():
    ws = Path(tempfile.mkdtemp())
    outside = Path(tempfile.mkdtemp())  # 独立的临时目录，必然在 ws 外
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        obs = _run("status", project_path=str(outside), workspace_root=ws)
    assert obs.status == "error"
    assert obs.retryable is False
    assert "workspace" in obs.reason.lower()


# ---------------------------------------------------------------------------
# 8. project_path 指向设备/伪文件路径 -> blocked_device_reason 命中
# ---------------------------------------------------------------------------
# 测试目的：验证 POSIX 设备路径被拦截且不可重试。
# 可能发现的缺陷类型：设备拦截缺失导致 /dev/null 等被当作项目根。
def test_project_path_blocked_device():
    ws = Path(tempfile.mkdtemp())
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        obs = _run("status", project_path="/dev/null", workspace_root=ws)
    assert obs.status == "error"
    assert obs.retryable is False
    assert "device" in obs.reason.lower()


# ---------------------------------------------------------------------------
# 9. affected 的 target 含 workspace 外文件路径 -> 逐项 resolve 失败
# ---------------------------------------------------------------------------
# 测试目的：验证 affected 多文件逐项越界被拒绝且不可重试。
# 可能发现的缺陷类型：affected 文件校验跳过导致越界文件被执行。
def test_affected_target_outside_workspace():
    ws = Path(tempfile.mkdtemp())
    outside = Path(tempfile.mkdtemp()) / "evil.py"
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        obs = _run("affected", target=str(outside), workspace_root=ws)
    assert obs.status == "error"
    assert obs.retryable is False
    assert "affected" in obs.reason.lower() or "workspace" in obs.reason.lower()


# ---------------------------------------------------------------------------
# 10. affected 合法多个文件 -> 命令含这些文件且 cwd 正确
# ---------------------------------------------------------------------------
# 测试目的：验证 affected 多个合法文件被加入 cmd 且 cwd 为 workspace 根。
# 可能发现的缺陷类型：affected 文件未加入命令 / cwd 传错导致查询失败。
def test_affected_multiple_files_command():
    ws = Path(tempfile.mkdtemp())
    f1 = ws / "a.py"
    f2 = ws / "b.py"
    f1.write_text("x")
    f2.write_text("y")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            obs = _run("affected", target=f"{f1} {f2}", workspace_root=ws)

    assert obs.status == "success"
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/codegraph"
    assert cmd[1] == "affected"
    # 两个合法文件都应出现在命令中（绝对解析路径）
    assert str(f1) in cmd
    assert str(f2) in cmd
    # cwd 应为 workspace 根
    assert Path(captured["cwd"]) == ws.resolve()


# ---------------------------------------------------------------------------
# 11. query 类 action（explore）缺 target -> 拒绝
# ---------------------------------------------------------------------------
# 测试目的：验证 query 类 action 缺少 target 时确定性拒绝。
# 可能发现的缺陷类型：target 必填校验缺失导致空 target 进入子进程调用。
def test_query_missing_target():
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        obs = _run("explore")
    assert obs.status == "error"
    assert obs.retryable is False
    assert "target is required" in obs.error.lower()


# ---------------------------------------------------------------------------
# 12. CLI 返回非零（含 "not initialized"）-> 提示 codegraph init
# ---------------------------------------------------------------------------
# 测试目的：验证非零退出且 stderr 含未初始化提示时给出 init 修正建议。
# 可能发现的缺陷类型：失败 reason 分类错误导致模型得不到正确修正指引。
def test_cli_nonzero_not_initialized():
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["codegraph"], returncode=1, stdout="", stderr="Error: .codegraph not initialized"
            ),
        ):
            obs = _run("explore", target="foo")
    assert obs.status == "error"
    assert obs.retryable is False
    assert "codegraph init" in obs.reason.lower()


# ---------------------------------------------------------------------------
# 13. CLI 返回非零（不含未初始化关键字）-> 通用失败 reason
# ---------------------------------------------------------------------------
# 测试目的：验证非零退出但无未初始化关键字时回落到通用失败说明。
# 可能发现的缺陷类型：reason 分支逻辑错误导致永远命中 init 分支。
def test_cli_nonzero_generic():
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["codegraph"], returncode=2, stdout="", stderr="symbol not found"
            ),
        ):
            obs = _run("node", target="missing.symbol")
    assert obs.status == "error"
    assert obs.retryable is False
    assert "codegraph init" not in obs.reason.lower()


# ---------------------------------------------------------------------------
# 14. 成功路径（mock 返回 stdout）
# ---------------------------------------------------------------------------
# 测试目的：验证 CLI 成功时 status=success、content=stdout、retryable=False。
# 可能发现的缺陷类型：成功分支字段填充错误（content 缺失 / status 错）。
def test_success_path():
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["codegraph"], returncode=0, stdout="  graph output  ", stderr=""
            ),
        ):
            obs = _run("status")
    assert obs.status == "success"
    assert obs.content == "graph output"  # 注意 .strip()
    assert obs.retryable is False
    assert obs.error == ""


# ---------------------------------------------------------------------------
# 15. 超时（TimeoutExpired） -> retryable=True
# ---------------------------------------------------------------------------
# 测试目的：验证子进程超时归一化为可重试错误。
# 可能发现的缺陷类型：超时未捕获 / retryable 误设为 False。
def test_timeout_retryable():
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["codegraph"], timeout=60.0),
        ):
            obs = _run("explore", target="foo")
    assert obs.status == "error"
    assert obs.retryable is True


# ---------------------------------------------------------------------------
# 16. json=True 时 cmd 含 "--json"
# ---------------------------------------------------------------------------
# 测试目的：验证 json 开关被追加为 --json 参数。
# 可能发现的缺陷类型：json 开关被忽略导致输出格式非预期。
def test_json_flag_in_command():
    captured = {}
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(subprocess, "run", side_effect=lambda cmd, **kw: captured.update(cmd=list(cmd)) or subprocess.CompletedProcess(cmd, 0, "ok", "")):
            obs = _run("search", target="foo", json=True)
    assert obs.status == "success"
    assert "--json" in captured["cmd"]


# ---------------------------------------------------------------------------
# 17. depth 在 impact 时含 "--depth N"
# ---------------------------------------------------------------------------
# 测试目的：验证 impact 的 depth 被追加为 --depth N。
# 可能发现的缺陷类型：depth 仅在部分 action 生效的边界遗漏。
def test_depth_flag_for_impact():
    captured = {}
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(subprocess, "run", side_effect=lambda cmd, **kw: captured.update(cmd=list(cmd)) or subprocess.CompletedProcess(cmd, 0, "ok", "")):
            obs = _run("impact", target="foo", depth=4)
    assert obs.status == "success"
    assert "--depth" in captured["cmd"]
    assert "4" in captured["cmd"]


# ---------------------------------------------------------------------------
# 18. depth 不在 impact/affected 时不应出现 --depth
# ---------------------------------------------------------------------------
# 测试目的：验证 depth 仅对 impact/affected 生效（如 explore 不应带 --depth）。
# 可能发现的缺陷类型：depth 被误加到所有 action。
def test_depth_not_for_explore():
    captured = {}
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(subprocess, "run", side_effect=lambda cmd, **kw: captured.update(cmd=list(cmd)) or subprocess.CompletedProcess(cmd, 0, "ok", "")):
            obs = _run("explore", target="foo", depth=2)
    assert obs.status == "success"
    assert "--depth" not in captured["cmd"]


# ---------------------------------------------------------------------------
# 19. 显式 project_path 在 workspace 内 -> cwd 为该路径
# ---------------------------------------------------------------------------
# 测试目的：验证合法的 project_path 解析后作为 cwd 传入子进程。
# 可能发现的缺陷类型：project_path 解析结果未被用作 cwd。
def test_project_path_inside_workspace_used_as_cwd():
    ws = Path(tempfile.mkdtemp())
    sub = ws / "subproj"
    sub.mkdir()
    captured = {}
    with mock.patch.object(shutil, "which", return_value="/usr/bin/codegraph"):
        with mock.patch.object(subprocess, "run", side_effect=lambda cmd, **kw: captured.update(cwd=kw.get("cwd")) or subprocess.CompletedProcess(cmd, 0, "ok", "")):
            obs = _run("status", project_path="subproj", workspace_root=ws)
    assert obs.status == "success"
    assert Path(captured["cwd"]) == sub.resolve()


# ---------------------------------------------------------------------------
# 20. build_tool_system 注册表中存在 "codegraph"
# ---------------------------------------------------------------------------
# 测试目的：验证 codegraph 已集成注册到工具系统。
# 可能发现的缺陷类型：注册遗漏导致工具在运行时不可用。
def test_registered_in_tool_system():
    from app.tools.tool_system import ToolSystem

    system = ToolSystem.build_tool_system()
    definition = system.registry.get("codegraph")
    assert definition is not None
    assert definition.name == "codegraph"
    assert definition.args_model is CodeGraphArgs
