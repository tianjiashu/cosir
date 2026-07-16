"""Agent Replay 只读查询服务。"""

from app.core.replay.compaction import ReplayCompactor
from app.core.replay.projector import ReplayProjector
from app.core.replay.records import ReplayNode, ReplayNodeDetail, ReplayTimeline
from app.storage.trace_store import TraceStore


class ReplayService:
    """从 trace ledger 构建 Agent Replay 响应。"""

    def __init__(
        self,
        trace_store: TraceStore,
        projector: ReplayProjector | None = None,
        event_limit: int = 1000,
        compactor: ReplayCompactor | None = None,
    ) -> None:
        """初始化 Replay 查询服务。

        参数:
            trace_store: Trace Backbone 的 SQLite 事实源。
            projector: 可选 replay 投影器，省略时使用默认投影器。
            event_limit: 单次投影读取的最大 trace event 数量。
            compactor: 可选列表响应压缩器。

        返回:
            无。

        异常:
            ValueError: 如果 event_limit 小于 1。

        副作用:
            在服务实例上保存依赖引用，不写入数据库。
        """

        if event_limit < 1:
            raise ValueError("event_limit must be greater than zero")
        self._trace_store = trace_store
        self._projector = projector or ReplayProjector()
        self._event_limit = event_limit
        self._compactor = compactor or ReplayCompactor()

    def get_run_replay(
        self,
        run_id: str,
        include_payload: bool = False,
        include_debug: bool = False,
    ) -> dict:
        """返回指定 run 的 replay timeline。

        参数:
            run_id: Durable Run 标识。
            include_payload: 是否在列表节点中包含完整 payload。
            include_debug: 是否返回排查用 debug 信息。

        返回:
            replay timeline 字典。

        异常:
            ValueError: 如果 run_id 为空。
            KeyError: 如果 run 没有 trace 事件。
            RuntimeError: 如果 trace 事件投影失败（详细原因已写入 replay_project_failed 日志）。

        副作用:
            只读查询 SQLite，不修改运行状态、不消费 resume command。
        """

        _ensure_not_blank(run_id, "run_id")
        timeline = self._project_run(run_id)
        timeline = self._compactor.compact_timeline(timeline)
        return timeline.to_dict(include_payload=include_payload, include_debug=include_debug)

    def get_trace_replay(
        self,
        trace_id: str,
        include_payload: bool = False,
        include_debug: bool = False,
    ) -> dict:
        """返回指定 trace 的 replay timeline。

        参数:
            trace_id: Trace 标识。
            include_payload: 是否在列表节点中包含完整 payload。
            include_debug: 是否返回排查用 debug 信息。

        返回:
            replay timeline 字典。

        异常:
            ValueError: 如果 trace_id 为空。
            KeyError: 如果 trace 没有事件。
            RuntimeError: 如果 trace 事件投影失败（详细原因已写入 replay_project_failed 日志）。

        副作用:
            只读查询 SQLite，不修改运行状态。
        """

        _ensure_not_blank(trace_id, "trace_id")
        events = self._trace_store.list_events(trace_id=trace_id, limit=self._event_limit)
        if not events:
            raise KeyError(trace_id)
        try:
            timeline = self._compactor.compact_timeline(self._projector.project_events(events))
        except Exception as exc:
            raise RuntimeError(f"failed to project replay timeline for trace {trace_id}") from exc
        return timeline.to_dict(include_payload=include_payload, include_debug=include_debug)

    def get_node_detail(self, node_id: str, include_debug: bool = True) -> dict:
        """返回 replay 节点详情。

        参数:
            node_id: replay_node_id，格式由 ReplayProjector 生成。
            include_debug: 是否返回排查用 debug 信息。

        返回:
            包含完整 payload 的 replay 节点字典。

        异常:
            ValueError: 如果 node_id 为空或格式非法。
            KeyError: 如果 run 或节点不存在。
            RuntimeError: 如果 trace 事件投影失败（详细原因已写入 replay_project_failed 日志）。

        副作用:
            只读查询 SQLite，不产生工具或恢复副作用。
        """

        _ensure_not_blank(node_id, "node_id")
        run_id = self._run_id_from_node_id(node_id)
        timeline = self._project_run(run_id)
        node = _find_node(timeline.nodes, node_id)
        return ReplayNodeDetail(node=node).to_dict(include_debug=include_debug)

    def _project_run(self, run_id: str) -> ReplayTimeline:
        """读取并投影指定 run 的 trace events。

        参数:
            run_id: Durable Run 标识。

        返回:
            ReplayTimeline 值对象。

        异常:
            KeyError: 如果 run 没有 trace 事件。
            RuntimeError: 如果 trace 事件投影失败（详细原因已写入 replay_project_failed 日志）。

        副作用:
            只读查询 SQLite。
        """

        events = self._trace_store.list_events(run_id=run_id, limit=self._event_limit)
        if not events:
            raise KeyError(run_id)
        try:
            return self._projector.project_events(events)
        except Exception as exc:
            raise RuntimeError(f"failed to project replay timeline for run {run_id}") from exc

    def _run_id_from_node_id(self, node_id: str) -> str:
        """从 replay_node_id 中解析 run_id。

        参数:
            node_id: replay_node_id。

        返回:
            run_id。

        异常:
            ValueError: 如果 node_id 不符合 ``run_id:node_type:start:end`` 格式。

        副作用:
            无。
        """

        parts = node_id.rsplit(":", 3)
        if len(parts) != 4 or not all(parts):
            raise ValueError("node_id is invalid")
        return parts[0]


def _find_node(nodes: list[ReplayNode], node_id: str) -> ReplayNode:
    """在 timeline 节点列表中查找指定节点。

    参数:
        nodes: replay 节点列表。
        node_id: 目标 replay_node_id。

    返回:
        匹配的 ReplayNode。

    异常:
        KeyError: 如果节点不存在。

    副作用:
        无。
    """

    for node in nodes:
        if node.replay_node_id == node_id:
            return node
    raise KeyError(node_id)


def _ensure_not_blank(value: str, field_name: str) -> None:
    """校验字符串字段不为空。

    参数:
        value: 待校验字符串。
        field_name: 字段名称，用于错误信息。

    返回:
        无。

    异常:
        ValueError: 如果 value 为空白。

    副作用:
        无。
    """

    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
