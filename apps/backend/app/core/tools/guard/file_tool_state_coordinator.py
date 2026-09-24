"""协调文件 revision、重复只读调用和写路径锁。

本协调器是 ToolExecutor 的窄协作者，把三套独立的状态机制（revision / 重复只读调用 /
写路径锁）统一编排成调度链上的四步时序：

    prepare -> lock -> check_stale -> (执行 handler) -> complete

- ``prepare``：执行前只读阶段。解析本次调用的读写/锁定路径（委托
  ``resolve_file_resource_paths``），并做重复只读调用检测；命中重复时直接产出一个
  ``early_observation`` 提前返回，不再真正执行 handler。
- ``lock``：对写路径持进程内锁（``FilePathLockRegistry``），串行化同一 task/path 的写。
- ``check_stale``：在持锁后、执行 handler 前，检测待写文件是否自上次观察以来已被外部
  改动（stale revision），是则产出 stale_file/stale_patch 错误，避免基于过期内容写入。
- ``complete``：handler 成功后，把本次观察到的文件快照和写路径记入 revision registry，
  并把重复调用签名标记为可用基线，供下一次 ``prepare`` 复用。

对非文件工具（如 execute_terminal，无 ``filesystem`` 资源键），``prepare`` 返回空计划，
使后续 ``lock`` / ``check_stale`` / ``complete`` 全部短路为 no-op，保持统一管线。

三个 registry 均为进程内、按 task 隔离、LRU 有界的有状态容器，协调器只负责编排与读写，
不持有任何文件内容。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from app.core.tools.display.filesystem_display import build_repeated_call_display_data
from app.core.tools.guard.file_resource_paths import FileResourcePaths, resolve_file_resource_paths
from app.core.tools.guard.file_state import (
    FileFingerprint,
    FilePathLockRegistry,
    FileRevisionRegistry,
    RepeatedCallRegistry,
    get_shared_file_path_lock_registry,
)
from app.core.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.schemas.tool_names import (
    TOOL_APPLY_PATCH,
    TOOL_FIND_FILES,
    TOOL_READ_FILE,
    TOOL_REPLACE,
    TOOL_SEARCH_CONTENT,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_handler.search.file_walker import iter_files
from app.core.tools.tool_handler.search.ignore_rules import load_ignore_rules

_REPEATED_TOOLS = frozenset({TOOL_READ_FILE, TOOL_SEARCH_CONTENT, TOOL_FIND_FILES})


def _canonical_path(root: Path, value: Any) -> Any:
    """把路径类参数归一为以 workspace 根为基准的稳定键。

    参数:
        root: 当前 workspace 根目录；相对路径以此为准解析。
        value: 待归一的任意参数值。

    返回:
        字符串路径经 ``os.path.normcase`` + 以 ``root`` 为基准的 ``abspath`` 归一后的
        结果；非字符串原样返回，避免污染非路径字段。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, str) and value:
        based = value if os.path.isabs(value) else str(root / value)
        return os.path.normcase(os.path.abspath(based))
    return value


def normalize_repeated_call_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    """为重复调用签名提取并归一已知路径字段（公开 API）。

    参数:
        tool_name: 当前工具名称。
        arguments: 已校验工具参数。
        root: 当前 workspace 根目录；路径归一以此为基准，避免 CWD 漂移。

    返回:
        仅含签名相关字段的字典；路径字段经 :func:`_canonical_path` 归一，
        非路径字段原样保留，保证等价路径（相对 / 绝对 / 双斜杠）产生相同签名。
        其中 ``mode`` 字段为**工具语义标记**，用于区分 ``patch_write`` 工具（值
        ``"replace"``，replace 语义）与 ``apply_patch`` 工具（值 ``"apply_patch"``，
        Git unified-diff 语义）。该 ``mode`` 是内部生成的语义标记，**并非**模型传入的
        ``arguments["mode"]`` 入参——拆分后的工具已不再接收 ``mode`` 入参。

    异常:
        无。

    副作用:
        无。
    """

    if tool_name == TOOL_REPLACE:
        # patch_write 工具：固定 replace 语义（mode 为工具语义标记，非用户入参），
        # 以 path 作为重复调用签名键。
        return {"mode": "replace", "path": _canonical_path(root, arguments.get("path"))}
    if tool_name == TOOL_APPLY_PATCH:
        # apply_patch 工具：固定 Git unified-diff 语义（mode 为内部标记），
        # 以实际 schema 参数 patch 的文本作为重复调用签名键。
        return {"mode": "apply_patch", "patch": arguments.get("patch")}
    if tool_name in {TOOL_SEARCH_CONTENT, TOOL_FIND_FILES}:
        # 搜索结果由 path/pattern/file_glob/分页等全部参数共同决定，
        # 仅归一 path 会导致「不同检索词搜索同一范围」被误判为重复而拦截。
        return {
            "path": _canonical_path(root, arguments.get("path")),
            "pattern": arguments.get("pattern"),
            "file_glob": arguments.get("file_glob"),
            "limit": arguments.get("limit"),
            "offset": arguments.get("offset"),
            "context": arguments.get("context"),
        }
    if tool_name == TOOL_READ_FILE:
        # 大文件支持按 offset/limit 分页续读；不同页必须视为不同调用，否则续读
        # 会被误判为「unchanged」而永远只能看到第一页。
        return {
            "path": _canonical_path(root, arguments.get("path")),
            "offset": arguments.get("offset"),
            "limit": arguments.get("limit"),
        }
    path = arguments.get("path")
    return {"path": _canonical_path(root, path)}


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
    """把文件协作状态机制收口为 ToolExecutor 的窄协作者。

    对外只暴露调度链需要的 4 个编排方法（``prepare`` / ``lock`` / ``check_stale`` /
    ``complete``），内部状态全部收敛到三个可注入的 registry；协调器自身无状态。
    """

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
        self._path_locks = path_locks or get_shared_file_path_lock_registry()
        self._repeated_calls = repeated_calls or RepeatedCallRegistry()
        self._max_scope_paths = max_scope_paths

    def prepare(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext,
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
            执行计划；``early_observation`` 非空时执行器应直接返回。

        异常:
            FileResourcePathError: 文件路径被安全策略拒绝时抛出。

        副作用:
            读取文件元数据，并可能更新重复调用计数。
        """
        # 非文件工具（execute_terminal 等）无 filesystem 资源，无需 revision/
        # stale/锁协调：返回空计划，使后续 lock/check_stale/complete 全部 no-op。
        if "filesystem" not in tool.resource_keys:
            return FileToolExecutionPlan(
                resources=FileResourcePaths(),
                observed_paths=(),
            )

        # 第一步：把「工具名 + 已校验参数」解析为本次调用的 read/write/lock 路径与搜索范围。
        resources = resolve_file_resource_paths(tool.name, arguments, execution_context)

        # 第二步：取本次调用「观察到的」路径集合（read 路径 + 目录遍历范围）及其 fingerprint
        # 快照。这份快照会在 complete 时回写，作为后续重复调用检测与 stale 判定的基线。
        observed_paths, snapshot_complete = self._observed_paths(
            resources, execution_context.workspace_root
        )
        observed_snapshot = self._revisions.snapshot_token(observed_paths)
        # 仅 read_file/search_content/find_files 参与重复调用检测；若目录遍历超容量导致快照不完整，
        # 也跳过重复检测（避免「未看全却判重复」误拦截）。其余工具直接返回计划，交由
        # lock/check_stale/complete 走状态协调，但跳过重复拦截。
        if tool.name not in _REPEATED_TOOLS or not snapshot_complete:
            return FileToolExecutionPlan(
                resources=resources,
                observed_paths=observed_paths,
                observed_snapshot=observed_snapshot,
                snapshot_complete=snapshot_complete,
            )

        # 第三步（仅 read_file/search_content/find_files）：构造归一化调用签名并查询
        # 重复调用 registry。
        # 签名含路径归一（等价路径映射到同一键）；快照参与比对，文件变了就不算重复。
        signature = self._signature(
            tool.name,
            normalize_repeated_call_arguments(
                tool.name,
                arguments,
                execution_context.workspace_root,
            ),
        )
        action = self._repeated_calls.check(
            str(execution_context.task_id),
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

        # 无执行上下文，或本次没有待写路径（只读工具）时无需 stale 检查。
        if execution_context is None or not plan.resources.write_paths:
            return None
        # 把「写路径当前 fingerprint」与 revision registry 里上次观察到的基线比对，
        # 找出被外部改动过的路径（从未记录过的路径不算 stale）。
        stale_paths = self._revisions.stale_paths(
            str(execution_context.task_id),
            plan.resources.write_paths,
        )
        if not stale_paths:
            return None
        # patch_write / apply_patch 对文本敏感用 stale_patch 语义；Delete/Move 按文件状态
        # 使用 stale_file。
        reason = "stale_patch" if tool.name in (TOOL_REPLACE, TOOL_APPLY_PATCH) else "stale_file"
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

        # 无上下文或无待锁路径时是空持锁：直接放行，不做任何锁操作。
        if execution_context is None or not plan.resources.lock_paths:
            yield
            return
        # 对本次写路径（含 workspace 祖先链）按稳定顺序获取进程内 RLock，保证同一
        # task 下对相同路径的并发写被串行化；退出 with 块时逆序释放。
        with self._path_locks.acquire(
            str(execution_context.workspace_root.resolve()),
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
        # 只在整个调用成功（而非失败/取消）时才回写状态，避免把「失败尝试」当成可复用基线。
        if observation.status != "success":
            return
        # 回写一：把本次观察到的文件快照记入 revision，作为后续 stale 判定的新基线。
        if plan.observed_snapshot:
            self._revisions.record_snapshots(
                str(execution_context.task_id),
                plan.observed_snapshot,
            )
        # 回写二：把本次写入的文件路径记入 revision，使「自己刚写的文件」不再被判 stale。
        if plan.resources.write_paths:
            self._revisions.record(
                str(execution_context.task_id),
                plan.resources.write_paths,
            )
        # 回写三：把本次成功的重复调用签名登记为可复用基线；下一次相同签名 + 相同
        # 快照的调用会被判为 unchanged/warning/block。
        if plan.repeated_signature:
            self._repeated_calls.record_success(
                str(execution_context.task_id),
                plan.repeated_signature,
                plan.observed_snapshot,
            )

    def clear_task(self, task_id: int) -> None:
        """清除一个 Task 的全部进程内文件协作状态。

        参数:
            task_id: 待清除任务标识。

        返回:
            无。

        异常:
            无。

        副作用:
            清除 revision、路径锁和重复调用 registry 中该 Task 的状态；调用方必须
            在该 Task 的所有工具执行退出后调用。
        """

        key = str(task_id)
        self._revisions.clear_task(key)
        # Physical file locks are scoped by canonical workspace, not task: multiple
        # tasks may share one workspace. Keep the bounded registry entries so clearing
        # one task cannot invalidate locks currently used by another task.
        self._repeated_calls.clear_task(key)

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
        workspace_root: Path,
    ) -> tuple[tuple[Path, ...], bool]:
        """展开一次只读调用实际观察的有界路径集合。

        参数:
            resources: 工具资源描述。
            workspace_root: 当前 workspace 根：既是 ``.cosir/.fileignore`` 规则的来源，也是
                相对路径规则的匹配基准。

        返回:
            ``(路径集合, 是否完整)``；list/search 超过容量时完整标记为 False。

        异常:
            无。目录遍历失败时仅保留 scope 根。

        副作用:
            读取目录结构和文件元数据；规则文件缺失时可能创建
            ``<workspace>/.cosir/.fileignore``。
        """

        paths = list(resources.read_paths)
        scope_root = resources.scope_root
        # 无搜索范围（如 read_file 单文件）时，观察集合就是 read 路径本身。
        if scope_root is None:
            return tuple(paths), True
        paths.append(scope_root)
        # 越界只读根（如 C:/Windows/System32）允许读取但禁止 prepare 阶段全量遍历，
        # 避免模型输入触发目录遍历 DoS；此时仅保留 scope 根本身，不做重复检测。
        if resources.scope_escapes_workspace:
            return tuple(paths), True
        # 正常路径：在容量上限内采样目录内容作为「观察集合」。采样结果用于重复调用
        # 检测；采样不完整（超容量 / 遍历失败）时返回 snapshot_complete=False，调用方
        # 据此跳过重复检测。
        snapshot_complete = True
        try:
            if resources.scope_recursive:
                # list_directory/search_content/find_files：递归遍历 scope 下所有文件，
                # 最多取 max 个；目录忽略规则取自 workspace 的 .cosir/.fileignore。
                sampled = list(
                    islice(
                        iter_files(scope_root, rules=load_ignore_rules(workspace_root)),
                        self._max_scope_paths,
                    )
                )
            elif scope_root.is_dir():
                # list_directory 顶层：仅列一层子项。
                sampled = list(islice(scope_root.iterdir(), self._max_scope_paths))
            else:
                sampled = []
            # scope_root 本身已占 1 个名额，故采样最多再补 (max-1) 个。
            available = self._max_scope_paths - 1
            paths.extend(sampled[:available])
            # 采到的数量不超过可用额度才算「看全」，否则标记不完整。
            snapshot_complete = len(sampled) <= available
        except OSError:
            # 目录遍历失败（权限/不存在）：保留 scope 根本身，标记不完整，跳过重复检测。
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

        # registry 判定的四种动作：
        # - execute：允许真正执行，无需拦截（返回 None）。
        # - block：search_content/find_files 连续重复（>=2 次未变），硬阻断为 error。
        # - warning：search_content/find_files 首次重复，跳过但给 warning（success + 提示）。
        # - unchanged：read_file 重复（每次重复都跳过），成功但提示复用上次结果。
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
            data = build_repeated_call_display_data()
        else:
            content = (
                "Unchanged since the previous read. Use the previous result "
                "instead of reading the same file again."
            )
            data = build_repeated_call_display_data(unchanged=True)
        return ToolObservation(
            tool_name=tool.name,
            status="success",
            content=content,
            permission=tool.permission,
            tool_call_id=tool_call_id,
            display_data=data,
        )
