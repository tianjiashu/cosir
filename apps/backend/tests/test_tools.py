"""针对安全只读工具与工具调度器边界的测试。"""

import logging
import signal
import tempfile
import time
import unittest
from pathlib import Path
from typing import Iterable

from app.tools.registry.memory import ToolRegistry
from app.tools.tool_handler.safe_read import SafeReadTools
from app.tools.runtime.compatibility import ToolScheduler
from app.tools.schemas import ToolCall, ToolDefinition


def _empty_test_handler() -> str:
    """为不应被执行它的测试返回一个标记字符串。

    参数:
        无。

    返回:
        标记内容。

    异常:
        无。

    副作用:
        无。
    """

    return "executed"


def _write_marker_test_handler(path: str) -> str:
    """如果测试工具处理函数被执行，则写入一个标记文件。

    参数:
        path: 待写入的文件系统路径。

    返回:
        标记内容。

    异常:
        OSError: 如果标记文件无法被写入。

    副作用:
        写入标记文件。
    """

    Path(path).write_text("executed", encoding="utf-8")
    return "executed"


def _ignore_term_write_test_handler(path: str) -> str:
    """忽略 SIGTERM、休眠，然后写入一个标记文件。

    参数:
        path: 休眠之后待写入的文件系统路径。

    返回:
        标记内容。

    异常:
        OSError: 如果标记文件无法被写入。

    副作用:
        安装一个 SIGTERM 处理器、休眠并写入文件，除非被终止。
    """

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(0.5)
    Path(path).write_text("late", encoding="utf-8")
    return "late"


def _large_output_test_handler() -> str:
    """返回一个大的文本负载，用于进程结果传输测试。

    参数:
        无。

    返回:
        大的文本负载。

    异常:
        无。

    副作用:
        无。
    """

    return "x" * (1024 * 1024 * 2)


class ToolSchedulerTests(unittest.TestCase):
    """校验工具执行会经过注册表与权限检查。"""

    def test_read_file_tool_returns_file_content(self) -> None:
        """校验 read_file 通过工具调度器成功。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 read_file 未返回期望的内容。

        副作用:
            创建一个临时的项目文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "note.txt").write_text("hello", encoding="utf-8")
            scheduler = self._build_scheduler(project_root, allowed_permissions=("safe_read",))

            observation = scheduler.execute(
                ToolCall(tool_name="read_file", arguments={"path": "note.txt"})
            )

            self.assertEqual(observation.status, "success")
            self.assertEqual(observation.content, "hello")

    def test_permission_denial_returns_error_observation(self) -> None:
        """校验当权限不被允许时工具会被拒绝。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果被拒绝的工具未返回错误观测结果。

        副作用:
            创建一个临时的项目文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "note.txt").write_text("hello", encoding="utf-8")
            scheduler = self._build_scheduler(project_root, allowed_permissions=())

            observation = scheduler.execute(
                ToolCall(tool_name="read_file", arguments={"path": "note.txt"})
            )

            self.assertEqual(observation.status, "error")
            self.assertIn("permission denied", observation.error)

    def test_list_model_visible_tools_filters_by_policy(self) -> None:
        """校验对模型可见的工具只包含可见的权限。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果被拒绝的工具被暴露为对模型可见的工具。

        副作用:
            创建一个内存中的调度器。
        """

        safe_tool = ToolDefinition(
            name="safe_tool",
            description="Safe test tool.",
            permission="safe_read",
            required_params=(),
            handler=_empty_test_handler,
            parameters_schema={"type": "object", "properties": {}},
        )
        write_tool = ToolDefinition(
            name="write_tool",
            description="Write test tool.",
            permission="write_file",
            required_params=(),
            handler=_empty_test_handler,
            parameters_schema={"type": "object", "properties": {}},
        )
        scheduler = self._build_scheduler_with_tools(
            (write_tool, safe_tool),
            allowed_permissions=("safe_read",),
        )

        visible_tools = scheduler.list_model_visible_tools()

        self.assertEqual([tool.name for tool in visible_tools], ["safe_tool"])

    def test_approval_required_tool_is_visible_but_not_executed(self) -> None:
        """校验需要审批的工具会被暴露但不会被执。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果需审批的工具在未审批的情况下被执行。

        副作用:
            创建一个内存中的调度器。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "marker.txt"
            tool = ToolDefinition(
                name="write_tool",
                description="Write test tool.",
                permission="write_file",
                required_params=("path",),
                handler=_write_marker_test_handler,
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
            scheduler = self._build_scheduler_with_tools(
                (tool,),
                allowed_permissions=(),
                approval_required_permissions=("write_file",),
            )

            visible_tools = scheduler.list_model_visible_tools()
            observation = scheduler.execute(
                ToolCall(tool_name="write_tool", arguments={"path": str(marker_path)})
            )

            self.assertEqual([visible_tool.name for visible_tool in visible_tools], ["write_tool"])
            self.assertEqual(observation.status, "approval_required")
            self.assertEqual(observation.permission, "write_file")
            self.assertEqual(observation.approval_status, "approval_required")
            self.assertIn("approval", observation.error)
            self.assertFalse(marker_path.exists())

    def test_path_escape_returns_error_observation(self) -> None:
        """校验安全只读工具无法逃逸出项目根目录。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果路径穿越未返回错误观测结果。

        副作用:
            创建一个临时的项目根目录。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = self._build_scheduler(
                Path(temp_dir),
                allowed_permissions=("safe_read",),
            )

            observation = scheduler.execute(
                ToolCall(tool_name="read_file", arguments={"path": "../outside.txt"})
            )

            self.assertEqual(observation.status, "error")
            self.assertIn("path escapes project root", observation.error)

    def test_schema_rejects_wrong_argument_type(self) -> None:
        """校验参数模式校验会拒绝非法的模型参数。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非字符串的 path 到达了工具处理函数。

        副作用:
            创建一个临时的调度器。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = self._build_scheduler(
                Path(temp_dir),
                allowed_permissions=("safe_read",),
            )

            observation = scheduler.execute(
                ToolCall(tool_name="read_file", arguments={"path": 123})
            )

            self.assertEqual(observation.status, "error")
            self.assertIn("invalid tool arguments", observation.error)

    def test_schema_rejects_unexpected_argument(self) -> None:
        """校验参数模式校验会拒绝额外的模型参数。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果额外的参数被接受。

        副作用:
            创建一个临时的调度器。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "note.txt").write_text("hello", encoding="utf-8")
            scheduler = self._build_scheduler(project_root, allowed_permissions=("safe_read",))

            observation = scheduler.execute(
                ToolCall(
                    tool_name="read_file",
                    arguments={"path": "note.txt", "extra": "ignored"},
                )
            )

            self.assertEqual(observation.status, "error")
            self.assertIn("invalid tool arguments", observation.error)

    def test_timeout_returns_error_observation(self) -> None:
        """校验超时的工具会在后续副作用之前被终止。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果慢处理函数被允许正常完成。

        副作用:
            启动一个子进程并校验延迟的文件写入不会发生。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "late.txt"
            tool = ToolDefinition(
                name="slow_tool",
                description="Slow test tool.",
                permission="safe_read",
                required_params=("path",),
                handler=_ignore_term_write_test_handler,
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                timeout_seconds=0.01,
            )
            scheduler = self._build_scheduler_with_tools(
                (tool,),
                allowed_permissions=("safe_read",),
            )

            observation = scheduler.execute(
                ToolCall(tool_name="slow_tool", arguments={"path": str(marker_path)})
            )
            time.sleep(0.7)

            self.assertEqual(observation.status, "error")
            self.assertIn("timed out", observation.error)
            self.assertFalse(marker_path.exists())

    def test_non_object_arguments_return_error_observation(self) -> None:
        """校验非对象的模型参数会在处理函数执行前被拒绝。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果列表参数被接受或执行。

        副作用:
            创建一个内存中的调度器。
        """

        tool = ToolDefinition(
            name="empty_tool",
            description="No-argument test tool.",
            permission="safe_read",
            required_params=(),
            handler=_empty_test_handler,
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
        scheduler = self._build_scheduler_with_tools((tool,), allowed_permissions=("safe_read",))

        observation = scheduler.execute(ToolCall(tool_name="empty_tool", arguments=[]))

        self.assertEqual(observation.status, "error")
        self.assertIn("expected object", observation.error)

    def test_large_tool_output_is_not_misclassified_as_timeout(self) -> None:
        """校验大的进程结果可以在超时之前被返回。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果大输出被当作超时或错误。

        副作用:
            启动一个返回大字符串的子进程。
        """

        tool = ToolDefinition(
            name="large_tool",
            description="Large output test tool.",
            permission="safe_read",
            required_params=(),
            handler=_large_output_test_handler,
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            timeout_seconds=2.0,
        )
        scheduler = self._build_scheduler_with_tools((tool,), allowed_permissions=("safe_read",))

        observation = scheduler.execute(ToolCall(tool_name="large_tool", arguments={}))

        self.assertEqual(observation.status, "success")
        self.assertEqual(len(observation.content), 1024 * 1024 * 2)

    def _build_scheduler(
        self,
        project_root: Path,
        allowed_permissions: tuple,
    ) -> ToolScheduler:
        """为测试构建一个带有安全只读工具的调度器。

        参数:
            project_root: 安全只读工具使用的临时项目根目录。
            allowed_permissions: 调度器允许的权限级别。

        返回:
            配置了安全只读工具定义的 ToolScheduler。

        异常:
            无。

        副作用:
            创建内存中的注册表与日志记录器对象。
        """

        safe_tools = SafeReadTools(project_root)
        return self._build_scheduler_with_tools(
            safe_tools.definitions(),
            allowed_permissions,
        )

    def _build_scheduler_with_tools(
        self,
        tools: Iterable[ToolDefinition],
        allowed_permissions: tuple,
        approval_required_permissions: tuple = (),
    ) -> ToolScheduler:
        """为测试构建一个带有显式工具定义的调度器。

        参数:
            tools: 待注册的工具定义。
            allowed_permissions: 调度器允许的权限级别。
            approval_required_permissions: 需要审批的权限级别。

        返回:
            配置了所提供工具定义的 ToolScheduler。

        异常:
            无。

        副作用:
            创建内存中的注册表与日志记录器对象。
        """

        registry = ToolRegistry(tools)
        logger = logging.getLogger(f"test-tools-{id(tools)}")
        logger.handlers = []
        logger.addHandler(logging.NullHandler())
        return ToolScheduler(
            registry,
            allowed_permissions,
            logger,
            approval_required_permissions=approval_required_permissions,
        )


if __name__ == "__main__":
    unittest.main()
