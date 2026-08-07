"""write_file 工具的严苛单元测试。

覆盖 ``WriteFileTool.execute`` 的全部分支与其依赖（PathResolver / atomic_write /
looks_like_line_numbered / syntax_check / file_change_display / tool_success /
tool_error），并针对潜在 bug 设计对抗性用例：
- 设备名 / 越界路径拦截；
- 行号污染门禁（阈值边界）；
- BOM / CRLF 保留；
- 语法错误后「文件已写但返回 error」的副作用与状态一致性；
- 读旧文件失败 / 写失败的 OSError 归一化（retryable=True）；
- to_definition / build_write_file_definition 契约。
"""

from pathlib import Path

import pytest

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.write_file import (
    WriteFileTool,
    build_write_file_definition,
)


@pytest.fixture
def tool() -> WriteFileTool:
    return WriteFileTool()


# --------------------------------------------------------------------------- #
# 正常写入
# --------------------------------------------------------------------------- #


def test_write_new_file_success(tool, context, workspace):
    obs = tool.execute("sub/new.txt", "hello\nworld\n", execution_context=context)

    assert obs.status == "success"
    target = workspace / "sub" / "new.txt"
    assert target.read_text(encoding="utf-8") == "hello\nworld\n"
    # content 原样回传；data 含 diff 展示，status=added。
    assert obs.content == "hello\nworld\n"
    assert obs.data is not None
    changes = obs.data["changes"]
    assert changes[0]["status"] == "added"
    assert changes[0]["before"] == ""
    assert obs.data["diff_stats"]["total_files"] == 1


def test_overwrite_existing_file_reports_modified(tool, context, workspace):
    target = workspace / "a.txt"
    target.write_text("old\n", encoding="utf-8")

    obs = tool.execute("a.txt", "new\n", execution_context=context)

    assert obs.status == "success"
    assert target.read_text(encoding="utf-8") == "new\n"
    changes = obs.data["changes"]
    assert changes[0]["status"] == "modified"
    assert changes[0]["before"] == "old\n"
    assert changes[0]["after"] == "new\n"


def test_write_empty_content(tool, context, workspace):
    obs = tool.execute("empty.txt", "", execution_context=context)

    assert obs.status == "success"
    assert (workspace / "empty.txt").read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------- #
# 路径 / 设备拦截
# --------------------------------------------------------------------------- #


def test_blocked_device_name_rejected(tool, context):
    obs = tool.execute("NUL", "x", execution_context=context)

    assert obs.status == "error"
    assert "device" in obs.error.lower()
    assert obs.permission == "file_write"
    # 确定性失败：不建议原样重试。
    assert obs.retryable is False


def test_path_escaping_workspace_rejected(tool, context):
    obs = tool.execute("../outside.txt", "x", execution_context=context)

    assert obs.status == "error"
    assert "escapes the project workspace" in obs.reason


def test_absolute_path_outside_workspace_rejected(tool, context, tmp_path):
    outside = tmp_path.parent / "totally_outside.txt"
    obs = tool.execute(str(outside), "x", execution_context=context)

    assert obs.status == "error"
    assert "could not write the file" in obs.error


def test_empty_path_rejected(tool, context):
    obs = tool.execute("   ", "x", execution_context=context)

    assert obs.status == "error"


# --------------------------------------------------------------------------- #
# 行号污染门禁
# --------------------------------------------------------------------------- #


def test_line_numbered_content_rejected(tool, context, workspace):
    polluted = "1| import os\n2| import sys\n3| print(os)\n"
    obs = tool.execute("x.py", polluted, execution_context=context)

    assert obs.status == "error"
    assert "line-numbered" in obs.error
    # 严苛：被拒后不得写盘。
    assert not (workspace / "x.py").exists()


def test_line_numbered_threshold_boundary_below_not_rejected(tool, context, workspace):
    """恰好未达 0.5 阈值（1/3 行含前缀）应放行，验证边界不误伤。"""

    content = "1| polluted\nplain line two\nplain line three\n"
    obs = tool.execute("mix.txt", content, execution_context=context)

    assert obs.status == "success"
    assert (workspace / "mix.txt").exists()


def test_line_numbered_threshold_boundary_at_half_rejected(tool, context, workspace):
    """恰好达到 0.5 阈值应拒绝（>= 判定）。"""

    # 4 行：2 行带前缀 + 1 行普通 + 末尾空行 => split 得 4 段，2/4 == 0.5。
    content = "1| a\n2| b\nplain\n"
    obs = tool.execute("half.txt", content, execution_context=context)

    assert obs.status == "error"


# --------------------------------------------------------------------------- #
# BOM / CRLF 保留（atomic_write 依赖）
# --------------------------------------------------------------------------- #


def test_preserve_existing_crlf(tool, context, workspace):
    target = workspace / "crlf.txt"
    target.write_bytes(b"line1\r\nline2\r\n")

    obs = tool.execute("crlf.txt", "new1\nnew2\n", execution_context=context)

    assert obs.status == "success"
    # 既有 CRLF 应被保留。
    assert target.read_bytes() == b"new1\r\nnew2\r\n"


def test_preserve_existing_bom(tool, context, workspace):
    target = workspace / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfold\n")

    obs = tool.execute("bom.txt", "brand new\n", execution_context=context)

    assert obs.status == "success"
    data = target.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert data == b"\xef\xbb\xbfbrand new\n"


def test_new_file_bom_from_content(tool, context, workspace):
    """新建文件按 content 自身 BOM 保留。"""

    obs = tool.execute("newbom.txt", "\ufeffhi\n", execution_context=context)

    assert obs.status == "success"
    assert (workspace / "newbom.txt").read_bytes() == b"\xef\xbb\xbfhi\n"


# --------------------------------------------------------------------------- #
# 语法检查（error 驱动）：文件已写但返回 error
# --------------------------------------------------------------------------- #


def test_syntax_error_returns_error_but_file_written(tool, context, workspace):
    """严苛验证：语法错误时文件已落盘，但返回 error 且带结构化诊断。"""

    bad_py = "def broken(:\n    pass\n"
    obs = tool.execute("bad.py", bad_py, execution_context=context)

    assert obs.status == "error"
    assert obs.error == "syntax error detected after write"
    # 副作用真实发生：文件确实写入了坏内容（引导 Agent 二次编辑修复）。
    assert (workspace / "bad.py").read_text(encoding="utf-8") == bad_py
    # display_data 携带结构化诊断，供前端展示。
    assert obs.data is not None
    assert "syntax_errors" in obs.data
    assert len(obs.data["syntax_errors"]) >= 1


def test_valid_python_passes_syntax_check(tool, context, workspace):
    good_py = "def ok():\n    return 1\n"
    obs = tool.execute("ok.py", good_py, execution_context=context)

    assert obs.status == "success"


def test_unknown_extension_skips_syntax_check(tool, context, workspace):
    """未知扩展名跳过语法检查，不因内容像坏代码而误报。"""

    obs = tool.execute("weird.xyz", "def broken(:\n", execution_context=context)

    assert obs.status == "success"


# --------------------------------------------------------------------------- #
# OSError 归一化
# --------------------------------------------------------------------------- #


def test_read_existing_file_oserror_is_retryable(tool, context, workspace, monkeypatch):
    target = workspace / "locked.txt"
    target.write_text("orig\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("locked by another process")

    monkeypatch.setattr(Path, "read_text", boom)

    obs = tool.execute("locked.txt", "new\n", execution_context=context)

    assert obs.status == "error"
    assert obs.retryable is True
    assert "could not read the file" in obs.error


def test_write_oserror_is_retryable(tool, context, workspace, monkeypatch):
    import app.tools.tool_handler.write_file as wf

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wf, "atomic_write_text", boom)

    obs = tool.execute("wfail.txt", "data\n", execution_context=context)

    assert obs.status == "error"
    assert obs.retryable is True
    assert "could not write the file" in obs.error


# --------------------------------------------------------------------------- #
# 定义契约
# --------------------------------------------------------------------------- #


def test_to_definition_contract(tool):
    definition = tool.to_definition()

    assert definition.name == "write_file"
    assert definition.permission == "file_write"
    assert definition.args_model.__name__ == "WriteFileArgs"
    assert definition.handler == tool.execute
    assert definition.resource_keys == ("filesystem",)
    assert definition.display.expand_layout == "diff"


def test_build_write_file_definition_factory():
    definition = build_write_file_definition()
    assert definition.name == "write_file"


def test_execution_context_is_containment_source(tool, workspace):
    """execute 以 execution_context.workspace_root 为 containment 唯一事实源。"""

    ctx = ToolExecutionContext(
        task_id="x", workspace_id="y", workspace_root=workspace
    )
    obs = tool.execute("ok.txt", "hi\n", execution_context=ctx)
    assert obs.status == "success"
