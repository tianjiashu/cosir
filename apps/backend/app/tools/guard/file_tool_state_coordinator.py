"""协调文件 revision、重复只读调用和写路径锁。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.guard.file_resource_paths import (
    FileResourcePaths,
    resolve_file_resource_paths,
)
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_handler.file_state import (
    FileFingerprint,
    FilePathLockRegistry,
    FileRevisionRegistry,
    RepeatedCallRegistry,
)
from app.tools.tool_handler.search.file_walker import iter_files

_REPEATED_TOOLS = frozenset({"read_file", "search_files"})


@dataclass(frozen=True)
class FileToolExecutionPlan:
    """文件工具执行前计算出的状态检查与锁资源。"""

    resources: FileResourcePaths
    observed_paths: tuple[Path, ...]
    observed_snapshot: tuple[tuple[str, FileFingerprint], ...] = ()
    snapshot_complete: bool = True
    repeated_signature: str = ""
    early_observation: ToolObservation | None = None


class FileToolStateCoordinator:
    """把文件协作状态机制收口为 ToolScheduler 的窄协作者。"""

    def __init__(
        self,
        *,
        revisions: FileRevisionRegistry | None = None,
        path_locks: FilePathLockRegistry | None = None,
        repeated_calls: RepeatedCallRegistry | None = None,
        max_scope_paths: int = 2_048,
    ) -> None:
        """初始化文件状态协调器。

        参数:
            revisions: 可注入的 revision registry。
            path_locks: 可注入的路径锁 registry。
            repeated_calls: 可注入的重复调用 registry。
            max_scope_paths: list/search 最多纳入 fingerprint 的路径数。

        返回:
            无。

        异常:
            ValueError: ``max_scope_paths`` 小于 1 时抛出。

        副作用:
            缺省时创建三个进程内有界 registry。
        """

        if max_scope_paths < 1:
            raise ValueError("max_scope_paths must be greater than zero")
        self._revisions = revisions or FileRevisionRegistry()
        self._path_locks = path_locks or FilePathLockRegistry()
        self._repeated_calls = repeated_calls or RepeatedCallRegistry()
        self._max_scope_paths = max_scope_paths

    def prepare(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext | None,
        *,
        tool_call_id: str,
    ) -> FileToolExecutionPlan:
        """执行前解析文件资源并完成重复调用检测。

        参数:
            tool: 当前工具定义。
            arguments: 已校验工具参数。
            execution_context: 当前 task/workspace 上下文。
            tool_call_id: 当前模型工具调用 id。

        返回:
            执行计划；``early_observation`` 非空时调度器应直接返回。

        异常:
            无。

        副作用:
            读取文件元数据，并可能更新重复调用计数。
        """

        resources = resolve_file_resource_paths(tool.name, arguments, execution_context)
        if execution_context is None:
            return FileToolExecutionPlan(resources=resources, observed_paths=())

        observed_paths, snapshot_complete = self._observed_paths(resources)
        observed_snapshot = self._revisions.snapshot_token(observed_paths)
        if tool.name not in _REPEATED_TOOLS or not snapshot_complete:
            return FileToolExecutionPlan(
                resources=resources,
                observed_paths=observed_paths,
                observed_snapshot=observed_snapshot,
                snapshot_complete=snapshot_complete,
            )

        signature = self._signature(tool.name, arguments)
        action = self._repeated_calls.check(
            execution_context.task_id,
            signature,
            observed_snapshot,
            tool_name=tool.name,
        )
        early = self._repeated_observation(
            tool,
            action.action,
            tool_call_id=tool_call_id,
        )
        return FileToolExecutionPlan(
            resources=resources,
            observed_paths=observed_paths,
            observed_snapshot=observed_snapshot,
            snapshot_complete=True,
            repeated_signature=signature,
            early_observation=early,
        )

    def check_stale(
        self,
        plan: FileToolExecutionPlan,
        tool: ToolDefinition,
        execution_context: ToolExecutionContext | None,
        *,
        tool_call_id: str,
    ) -> ToolObservation | None:
        """在持有写路径锁后检查 stale revision。

        参数:
            plan: 本次文件工具执行计划。
            tool: 当前工具定义。
            execution_context: 当前 task/workspace 上下文。
            tool_call_id: 当前模型工具调用 id。

        返回:
            未 stale 时返回 None；否则返回 ``stale_file`` / ``stale_patch`` 错误。

        异常:
            无。

        副作用:
            仅读取 revision registry 和当前文件元数据。
        """

        if execution_context is None or not plan.resources.write_paths:
            return None
        stale_paths = self._revisions.stale_paths(
            execution_context.task_id,
            plan.resources.write_paths,
        )
        if not stale_paths:
            return None
        reason = "stale_patch" if tool.name == "patch" else "stale_file"
        path_text = ", ".join(str(path) for path in stale_paths)
        return tool_error(
            tool.name,
            f"file changed since it was last observed: {path_text}",
            reason=reason,
            permission=tool.permission,
            tool_call_id=tool_call_id,
        )

    @contextmanager
    def lock(
        self,
        plan: FileToolExecutionPlan,
        execution_context: ToolExecutionContext | None,
    ) -> Iterator[None]:
        """为执行计划中的写路径持锁。

        参数:
            plan: :meth:`prepare` 产出的执行计划。
            execution_context: 当前 task/workspace 上下文。

        返回:
            上下文管理器。

        异常:
            RuntimeError: 路径锁 registry 容量耗尽时抛出。

        副作用:
            获取并释放 task/path 锁。
        """

        if execution_context is None or not plan.resources.lock_paths:
            yield
            return
        with self._path_locks.acquire(
            execution_context.task_id,
            plan.resources.lock_paths,
        ):
            yield

    def complete(
        self,
        plan: FileToolExecutionPlan,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> None:
        """成功后刷新 read/write revision。

        参数:
            plan: 本次执行计划。
            observation: handler 返回的最终观察。
            execution_context: 当前 task/workspace 上下文。

        返回:
            无。

        异常:
            无。

        副作用:
            成功时更新 task revision registry。
        """

        if execution_context is None:
            return
        if observation.status != "success":
            return
        if plan.observed_snapshot:
            self._revisions.record_snapshots(
                execution_context.task_id,
                plan.observed_snapshot,
            )
        if plan.resources.write_paths:
            self._revisions.record(
                execution_context.task_id,
                plan.resources.write_paths,
            )
        if plan.repeated_signature:
            self._repeated_calls.record_success(
                execution_context.task_id,
                plan.repeated_signature,
                plan.observed_snapshot,
            )

    @property
    def revisions(self) -> FileRevisionRegistry:
        """返回协调器使用的 revision registry。

        参数:
            无。

        返回:
            ``FileRevisionRegistry`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return self._revisions

    def _observed_paths(
        self,
        resources: FileResourcePaths,
    ) -> tuple[tuple[Path, ...], bool]:
        """展开一次只读调用实际观察的有界路径集合。

        参数:
            resources: 工具资源描述。

        返回:
            ``(路径集合, 是否完整)``；list/search 超过容量时完整标记为 False。

        异常:
            无。目录遍历失败时仅保留 scope 根。

        副作用:
            读取目录结构和文件元数据。
        """

        paths = list(resources.read_paths)
        scope_root = resources.scope_root
        if scope_root is None:
            return tuple(paths), True
        paths.append(scope_root)
        snapshot_complete = True
        try:
            if resources.scope_recursive:
                sampled = list(islice(iter_files(scope_root), self._max_scope_paths))
            elif scope_root.is_dir():
                sampled = list(islice(scope_root.iterdir(), self._max_scope_paths))
            else:
                sampled = []
            available = self._max_scope_paths - 1
            paths.extend(sampled[:available])
            snapshot_complete = len(sampled) <= available
        except OSError:
            snapshot_complete = False
        return tuple(paths[: self._max_scope_paths]), snapshot_complete

    @staticmethod
    def _signature(tool_name: str, arguments: Mapping[str, Any]) -> str:
        """构造稳定重复调用签名。

        参数:
            tool_name: 当前工具名。
            arguments: 已校验工具参数。

        返回:
            工具名与规范 JSON 参数组成的字符串。

        异常:
            无。

        副作用:
            无。
        """

        payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        return f"{tool_name}:{payload}"

    @staticmethod
    def _repeated_observation(
        tool: ToolDefinition,
        action: str,
        *,
        tool_call_id: str,
    ) -> ToolObservation | None:
        """把重复调用动作投影为提前返回的观察。

        参数:
            tool: 当前工具定义。
            action: registry 返回的动作。
            tool_call_id: 当前模型工具调用 id。

        返回:
            ``execute`` 返回 None；其余动作返回 success warning 或 error observation。

        异常:
            无。

        副作用:
            无。
        """

        if action == "execute":
            return None
        if action == "block":
            return tool_error(
                tool.name,
                "repeated search request was blocked because the search scope is unchanged",
                reason="repeated_call",
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )
        if action == "warning":
            content = (
                "Repeated search skipped: the search scope is unchanged. "
                "Use the previous result or refine the query."
            )
            data = {"repeated": True, "warning": True}
        else:
            content = (
                "Unchanged since the previous read. Use the previous result "
                "instead of reading the same file again."
            )
            data = {"unchanged": True}
        return ToolObservation(
            tool_name=tool.name,
            status="success",
            content=content,
            permission=tool.permission,
            tool_call_id=tool_call_id,
            display_data=data,
        )
