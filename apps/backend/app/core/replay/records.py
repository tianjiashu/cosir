"""Agent Replay API 输出值对象。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ReplayNode:
    """Replay timeline 中的单个节点。

    参数:
        replay_node_id: 稳定节点标识。
        trace_id: Trace 标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        sequence_start: 节点覆盖的起始 trace sequence。
        sequence_end: 节点覆盖的结束 trace sequence。
        node_type: 节点类型。
        title: 人类可读标题。
        status: 节点状态。
        summary: 列表摘要。
        payload: 详情 payload。
        related_span_ids: 关联 span 标识。
        related_event_ids: 关联 trace event 标识。
        related_artifact_ids: 关联 artifact 标识。
        checkpoint_id: 关联 checkpoint 标识。
        debug: 排查用引用信息，默认不进入列表响应。
        started_at: 节点开始时间。
        ended_at: 节点结束时间。
        duration_ms: 节点耗时。

    返回:
        不可变 replay 节点。

    异常:
        无。

    副作用:
        无。
    """

    replay_node_id: str
    trace_id: str
    run_id: str
    task_id: str
    sequence_start: int
    sequence_end: int
    node_type: str
    title: str
    status: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    related_span_ids: list[str] = field(default_factory=list)
    related_event_ids: list[str] = field(default_factory=list)
    related_artifact_ids: list[str] = field(default_factory=list)
    checkpoint_id: str = ""
    debug: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None

    def to_dict(self, include_payload: bool = True, include_debug: bool = False) -> dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            include_payload: 是否包含详情 payload。
            include_debug: 是否包含排查用 debug 引用。

        返回:
            可 JSON 序列化的字典。

        异常:
            无。

        副作用:
            无。
        """

        data = {
            "replay_node_id": self.replay_node_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sequence_start": self.sequence_start,
            "sequence_end": self.sequence_end,
            "node_type": self.node_type,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "related_span_ids": self.related_span_ids,
            "related_event_ids": self.related_event_ids,
            "related_artifact_ids": self.related_artifact_ids,
            "checkpoint_id": self.checkpoint_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
        }
        if include_payload:
            data["payload"] = self.payload
        if include_debug:
            data["debug"] = self.debug
        return data


@dataclass(frozen=True)
class ReplayTimeline:
    """一次 run 或 trace 的 Replay timeline。

    参数:
        trace_id: Trace 标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        nodes: 有序 replay 节点。
        debug: timeline 级排查信息，默认不进入响应。

    返回:
        不可变 timeline。

    异常:
        无。

    副作用:
        无。
    """

    trace_id: str
    run_id: str
    task_id: str
    nodes: list[ReplayNode]
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_payload: bool = False, include_debug: bool = False) -> dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            include_payload: 是否在节点中包含完整 payload。
            include_debug: 是否包含 timeline 和节点的排查信息。

        返回:
            timeline 字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "nodes": [
                node.to_dict(include_payload=include_payload, include_debug=include_debug)
                for node in self.nodes
            ],
            **({"debug": self.debug} if include_debug else {}),
        }


@dataclass(frozen=True)
class ReplayNodeDetail:
    """Replay 节点详情响应值对象。

    参数:
        node: 基础 replay 节点。
        debug: 详情级排查信息。

    返回:
        不可变节点详情。

    异常:
        无。

    副作用:
        无。
    """

    node: ReplayNode
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_debug: bool = True) -> dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            include_debug: 是否返回排查信息。

        返回:
            包含完整 payload 的节点详情字典。

        异常:
            无。

        副作用:
            无。
        """

        merged_debug = {**self.node.debug, **self.debug}
        data = self.node.to_dict(include_payload=True, include_debug=False)
        if include_debug:
            data["debug"] = merged_debug
        return data
