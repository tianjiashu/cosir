"""Workspace 索引准备结果值对象。

单一职责：承载 ``CodeGraphLifecycleService.ensure_ready`` 的返回结果——索引是否就绪、
采取了哪个动作、摘要与降级原因。不承载任何编排逻辑（编排归 LifecycleService）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceIndexReadiness:
    """workspace 索引准备结果。

    ``ready=False`` 表示 CodeGraph 不可用，调用方应降级到文件搜索（但不阻断任务）。
    """

    ready: bool
    state: str
    action_taken: str
    files_changed: int
    duration_ms: int
    degraded_reason: str | None
