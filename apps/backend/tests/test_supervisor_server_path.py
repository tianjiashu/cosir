"""CodeGraph Kernel supervisor 启动脚本路径回归测试。

覆盖：修复前的 `_server_script_path()` 错误地指向不存在的
``third_party/workspace_event/dist/agent-kernel/server.js``（导致 Kernel 启动报
``agent-kernel server not built``）。修正后应指向 ``third_party/codegraph`` vendor 内
实际构建产出的 ``server.js``。

本测试同时断言：
- 路径落在 ``third_party/codegraph/dist/agent-kernel/server.js``；
- 该文件确实存在（即 Kernel 已构建，否则 CI/本地会出现「索引中」卡死）。
"""

from __future__ import annotations

from app.codegraph.supervisor import _server_script_path


def test_server_script_path_points_to_codegraph_vendor():
    """路径应指向 codegraph vendor，而非已废弃的 workspace_event 目录。"""
    path = _server_script_path()
    assert path.name == "server.js"
    assert path.parent.name == "agent-kernel"
    assert path.parent.parent.name == "dist"
    # 关键断言：修正 bug——根目录必须是 third_party/codegraph，不是 third_party/workspace_event。
    assert path.parts[-5:-1] == ("third_party", "codegraph", "dist", "agent-kernel")


def test_server_script_exists():
    """server.js 必须已构建产出，否则 Kernel 无法启动，前端会永久「索引中」。"""
    path = _server_script_path()
    assert path.exists(), (
        f"agent-kernel server 未构建：{path} 不存在。"
        "请先在 third_party/codegraph 执行 npm install && npm run build。"
    )
    assert path.is_file()
