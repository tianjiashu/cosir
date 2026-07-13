"""日志可排查性探针测试（仅测试用，不修改业务代码）。

校验：使用 configure_logging 配置的真实 logger 时，运行时错误日志
会落盘、含 task_id 定位上下文、含堆栈、且格式含 ERROR 级别标记；
业务代码不主动把真实 API Key 明文写入日志。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.logging.configuration import configure_logging
from app.models.echo import EchoStreamingModelAdapter
from app.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.tools.registry import ToolRegistry
from app.tools.safe_read import SafeReadTools
from app.tools.scheduler import ToolScheduler


class FailingCheckpointStore(SQLiteTaskStore):
    def create_checkpoint(self, task_id, stage, summary, snapshot):
        raise RuntimeError("checkpoint storage unavailable")


class TestLoggingProbe(unittest.TestCase):
    def test_error_log_has_context_and_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            log_file = temp_dir / "app.log"
            logger = configure_logging(log_file)
            logger.setLevel(logging.DEBUG)

            scheduler = ToolScheduler(
                ToolRegistry(SafeReadTools(temp_dir).definitions()),
                allowed_permissions=("safe_read",),
                logger=logger,
            )
            runtime = AgentRuntime(
                settings=BackendSettings(
                    project_root=temp_dir,
                    log_file=log_file,
                    database_file=temp_dir / "app.sqlite3",
                ),
                task_store=FailingCheckpointStore(temp_dir / "app.sqlite3"),
                context_builder=TextContextBuilder(),
                model_adapter=EchoStreamingModelAdapter(),
                tool_scheduler=scheduler,
                logger=logger,
            )
            task = runtime.create_task("hello agent")

            async def collect():
                async for _ in runtime.run_task(task.task_id):
                    pass

            with self.assertLogs(logger="coding_agent.backend", level="ERROR"):
                asyncio.run(collect())

            for handler in logger.handlers:
                handler.flush()

            content = log_file.read_text(encoding="utf-8")
            # 错误日志应包含 task_id 定位上下文
            self.assertIn(task.task_id, content)
            # 错误日志应含级别标记与堆栈
            self.assertIn("ERROR", content)
            self.assertIn("Traceback", content)
            self.assertIn("RuntimeError", content)
            # 业务代码不会主动把真实 API Key 明文写入日志
            self.assertNotIn("sk-REALKEY", content)


if __name__ == "__main__":
    unittest.main()
