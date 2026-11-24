"""为 Chromium 测试采集真实 Terminal Worker 命令结果；不启动模型或访问用户数据库。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from app.app import app  # noqa: F401 — 与真实入口一致装配模块依赖，不启动 lifespan
from app.core.tool.schemas import ToolExecutionContext
from app.service.session.session_scratch import ensure_session_scratch
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool
from app.utils.path.system_cosir import system_cosir_dir
from app.utils.path.workspace_cosir import workspace_cosir_dir

SESSION_ID = 9042


def collect() -> dict[str, object]:
    """按默认的「请求批准」权限执行命令；生产里先于命令存在的目录在这里补齐，否则 Worker 校验沙箱路径时拒绝启动。"""
    samples = {}
    with TemporaryDirectory(prefix="cosir-terminal-ui-") as directory:
        # 后端启动时建系统 .cosir、建工作区时建工作区 .cosir、驱动开始前建会话临时目录。
        system_cosir_dir().mkdir(parents=True, exist_ok=True)
        workspace_cosir_dir(directory).mkdir(parents=True, exist_ok=True)
        ensure_session_scratch(SESSION_ID)
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
                    session_id=SESSION_ID, run_id=1, workspace_id=1, workspace_root=Path(directory),
                    step_id="fixture", tool_call_id=f"fixture-{name}",
                ),
            )
            # 样本必须是真的跑过的命令：启动失败的样本会让界面测试拿「无输出」去比真实输出，失败原因也看不出。
            if result.display_data.get("end") == "start_failed":
                raise RuntimeError(f"样本 {name} 启动失败：{result.content}")
            samples[name] = {"status": result.status, "display": result.display_data}
    return samples


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(collect(), ensure_ascii=False))
