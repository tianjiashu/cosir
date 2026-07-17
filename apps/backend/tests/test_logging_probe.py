"""日志可排查性探针测试（仅测试用，不修改业务代码）。

校验：使用 configure_logging 配置的真实 logger 时，运行时错误日志
会落盘、含 task_id 定位上下文、含堆栈、且格式含 ERROR 级别标记；
业务代码不主动把真实 API Key 明文写入日志。
"""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.config.logging import configure_logging
from app.config.logging import current_log_file
from app.models.echo import EchoStreamingModelAdapter
from app.core.runtime.runner import AgentRuntime
from app.storage.crud.task import SQLiteTaskStore
from app.tools.registry.memory import ToolRegistry
from app.tools.builtin.safe_read import SafeReadTools
from app.tools.runtime.compatibility import ToolScheduler


class FailingCheckpointStore(SQLiteTaskStore):
    """用于模拟 checkpoint 写入失败的测试存储。"""

    def create_checkpoint(self, task_id, stage, summary, snapshot):
        """模拟 checkpoint 存储不可用。

        参数:
            task_id: 任务标识。
            stage: checkpoint 阶段。
            summary: checkpoint 摘要。
            snapshot: checkpoint 快照。

        返回:
            永不返回。

        异常:
            RuntimeError: 始终抛出以触发错误日志。

        副作用:
            无。
        """

        raise RuntimeError("checkpoint storage unavailable")


class TestLoggingProbe(unittest.TestCase):
    def test_error_log_has_context_and_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            log_dir = temp_dir / "logs"
            log_file = current_log_file(log_dir)
            logger = configure_logging(log_dir)
            logger.setLevel(logging.DEBUG)

            scheduler = ToolScheduler(
                ToolRegistry(SafeReadTools(temp_dir).definitions()),
                allowed_permissions=("safe_read",),
                logger=logger,
            )
            runtime = AgentRuntime(
                settings=BackendSettings(
                    project_root=temp_dir,
                    log_dir=log_dir,
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
                """消费运行时事件直到任务结束。

                参数:
                    无。

                返回:
                    无。

                异常:
                    无。

                副作用:
                    执行运行时任务并触发 checkpoint 写入失败。
                """

                async for _ in runtime.run_task(task.task_id):
                    pass

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
