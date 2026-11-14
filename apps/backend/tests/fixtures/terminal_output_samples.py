"""为 Chromium 测试采集真实 Terminal Worker 命令结果；不启动模型或访问用户数据库。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from app.app import app  # noqa: F401 — 与真实入口一致装配模块依赖，不启动 lifespan
from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool


def collect() -> dict[str, object]:
    samples = {}
    with TemporaryDirectory(prefix="cosir-terminal-ui-") as directory:
        for name, command, timeout in [
            ("ansi",
             "printf '\\033[31m中文输出\\033[0m\\n'; "
             "printf 'progress 10%%\\rprogress 100%%\\n'; seq 1 120", 10),
            ("long", "seq 1 6000", 10),
            ("nonzero", "printf 'before failure\\n'; exit 7", 10),
            ("empty", "true", 10),
            ("timeout", "printf 'before timeout\\n'; sleep 5", 0.2),
        ]:
            result = ExecuteTerminalTool().execute(
                command=command, timeout=timeout,
                execution_context=ToolExecutionContext(
        call_intent="test",
                    session_id=9042, run_id=1, workspace_id=1, workspace_root=Path(directory),
                    step_id="fixture", tool_call_id=f"fixture-{name}",
                ),
            )
            samples[name] = {"status": result.status, "display": result.display_data}
    return samples


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(collect(), ensure_ascii=False))
