"""针对任务创建与 SSE 流式传输的 FastAPI API 测试。"""

import importlib.util
import logging
from pathlib import Path
import tempfile
import unittest

from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.models.echo import EchoStreamingModelAdapter
from app.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.tools.registry import ToolRegistry
from app.tools.safe_read import SafeReadTools
from app.tools.scheduler import ToolScheduler


FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class BackendApiTests(unittest.TestCase):
    """在 FastAPI 依赖可用时校验 API 路由。"""

    def setUp(self) -> None:
        """初始化 API 测试使用的临时目录。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建一个用于持有临时目录句柄的列表，使其保持存活。
        """

        self._temp_dirs = []

    def tearDown(self) -> None:
        """清理 API 测试使用的临时目录。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            删除测试创建的临时目录。
        """

        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def test_task_creation_rejects_null_text(self) -> None:
        """校验 API 校验会拒绝空任务文本。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果接受了空文本。

        副作用:
            创建一个进程内的 FastAPI 测试客户端。
        """

        client = self._build_client()
        response = client.post("/tasks", json={"text": None})

        self.assertEqual(response.status_code, 422)

    def test_task_stream_emits_sse_and_does_not_rerun_completed_task(self) -> None:
        """校验任务流会发出 SSE，且重复读取流会重放事件。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果流式传输失败，或重复读取追加了事件。

        副作用:
            在应用的测试数据库中创建任务状态。
        """

        client = self._build_client()
        create_response = client.post("/tasks", json={"text": "hello api"})
        self.assertEqual(create_response.status_code, 200)
        task_id = create_response.json()["task_id"]
        self.assertEqual(create_response.json()["agent_id"], "developer")

        first_stream = client.get(f"/tasks/{task_id}/stream")
        second_stream = client.get(f"/tasks/{task_id}/stream")
        events_response = client.get(f"/tasks/{task_id}/events")
        checkpoints_response = client.get(f"/tasks/{task_id}/checkpoints")

        self.assertEqual(first_stream.status_code, 200)
        self.assertIn("event: run_started", first_stream.text)
        self.assertIn("event: run_finished", first_stream.text)
        self.assertEqual(second_stream.status_code, 200)
        self.assertEqual(events_response.status_code, 200)
        self.assertEqual(checkpoints_response.status_code, 200)
        self.assertEqual(len(events_response.json()), first_stream.text.count("event: "))
        self.assertGreaterEqual(len(checkpoints_response.json()), 2)

    def _build_client(self):
        """为后端应用构建 FastAPI TestClient。

        参数:
            无。

        返回:
            后端 API 的 TestClient 实例。

        异常:
            RuntimeError: 如果 FastAPI 依赖不可用。

        副作用:
            初始化应用运行时依赖项。
        """

        from fastapi.testclient import TestClient

        from app.api.app import create_app

        return TestClient(create_app(runtime=self._build_runtime()))

    def _build_runtime(self) -> AgentRuntime:
        """为 API 测试构建一个隔离的运行时。

        参数:
            无。

        返回:
            带有临时项目根目录、数据库与日志路径的 AgentRuntime。

        异常:
            无。

        副作用:
            创建一个临时目录与内存中的日志记录器引用。
        """

        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        project_root = Path(temp_dir.name)
        logger = logging.getLogger(f"test-api-{id(temp_dir)}")
        logger.handlers = []
        logger.addHandler(logging.NullHandler())
        safe_tools = SafeReadTools(project_root)
        registry = ToolRegistry(safe_tools.definitions())
        return AgentRuntime(
            settings=BackendSettings(
                project_root=project_root,
                log_file=project_root / "app.log",
                database_file=project_root / "app.sqlite3",
            ),
            task_store=SQLiteTaskStore(project_root / "app.sqlite3"),
            context_builder=TextContextBuilder(),
            model_adapter=EchoStreamingModelAdapter(),
            tool_scheduler=ToolScheduler(
                registry=registry,
                allowed_permissions=("safe_read",),
                logger=logger,
            ),
            logger=logger,
        )


if __name__ == "__main__":
    unittest.main()
