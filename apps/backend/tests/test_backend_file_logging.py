"""后端固定 JSONL 文件日志的最小行为契约。"""

import json
import logging
from datetime import date
from pathlib import Path

from app.config.logging.configuration import configure_logging, shutdown_logging
from app.config.logging.context.log_context_store import merge_log_context, reset_log_context
from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
from app.config.logging.logger import log


def test_configure_logging_writes_fixed_jsonl_record(tmp_path: Path) -> None:
    """日志只落本地文件，并输出固定顶层字段。"""

    configure_logging(tmp_path)
    token = merge_log_context(trace_id="a" * 32)
    try:
        log.info(
            "backend_file_log_test",
            extra={"msg": "固定格式日志测试", "data": {"task_id": "task-1"}},
        )
    finally:
        reset_log_context(token)
        shutdown_logging()

    log_file = tmp_path / f"backend-{date.today().isoformat()}.log"
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    record = next(item for item in records if item["event"] == "backend_file_log_test")
    assert set(record) == {
        "ts",
        "level",
        "logger",
        "trace_id",
        "caller",
        "event",
        "msg",
        "data",
        "error",
        "truncated",
    }
    assert record["level"] == "INFO"
    assert record["trace_id"] == "a" * 32
    assert record["msg"] == "固定格式日志测试"
    assert record["data"] == {"task_id": "task-1"}


def test_shutdown_logging_does_not_close_unmanaged_handlers(tmp_path: Path) -> None:
    """测试或调用方临时 handler 不应被日志配置生命周期误关闭。"""

    logger = logging.getLogger("coding_agent.backend")
    unmanaged = logging.NullHandler()
    logger.addHandler(unmanaged)
    configure_logging(tmp_path)
    try:
        assert unmanaged in logger.handlers
    finally:
        logger.removeHandler(unmanaged)
        shutdown_logging()


def test_process_tool_context_preserves_trace_id() -> None:
    context = ToolExecutionContext(
        task_id=1,
        workspace_id=2,
        workspace_root=Path("."),
        trace_id="b" * 32,
    )

    assert context.for_process_execution().trace_id == "b" * 32
