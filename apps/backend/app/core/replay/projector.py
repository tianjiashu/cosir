"""将 trace ledger 投影为 Agent Replay timeline。"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from app.core.replay.events import ReplayNodeType, TERMINAL_EVENT_TYPES, TOOL_EVENT_TYPES, replay_type_for_event
from app.core.replay.records import ReplayNode, ReplayTimeline
from app.core.trace.records import TraceEventRecord

logger = logging.getLogger("coding_agent.backend")


class ReplayProjector:
    """把有序 trace event 转换为 replay timeline。"""

    def project_events(self, events: Iterable[TraceEventRecord]) -> ReplayTimeline:
        """投影 trace events。

        参数:
            events: 同一 run 或 trace 的 trace event 序列。

        返回:
            ReplayTimeline。没有事件时返回空标识和空节点。

        异常:
            TypeError、ValueError、KeyError 等: 当事件字段缺失、类型不符或比较键不可用时导致投影失败，会重新抛出（已写入 replay_project_failed 结构化日志，由调用方 ReplayService 转换为 RuntimeError）。

        副作用:
            无。
        """

        ordered: list[TraceEventRecord] = []
        try:
            ordered = sorted(events, key=lambda event: (event.sequence_no, event.created_at, event.event_id))
            if not ordered:
                return ReplayTimeline(trace_id="", run_id="", task_id="", nodes=[])
            nodes: list[ReplayNode] = []
            approval_chains = _approval_chains(ordered)
            approval_id_by_resume_command = _approval_id_by_resume_command(ordered)
            nodes.extend(self._project_model_events(ordered))
            nodes.extend(self._project_tool_events(ordered))
            nodes.extend(self._project_approval_events(ordered, approval_chains))
            nodes.extend(self._project_resume_events(ordered, approval_chains, approval_id_by_resume_command))
            nodes.extend(self._project_checkpoint_events(ordered))
            nodes.extend(self._project_simple_events(ordered))
            nodes.sort(key=lambda node: (node.sequence_start, node.sequence_end, node.replay_node_id))
            first = ordered[0]
            return ReplayTimeline(
                trace_id=first.trace_id,
                run_id=first.run_id,
                task_id=first.task_id,
                nodes=nodes,
                debug=_timeline_debug(ordered),
            )
        except Exception:
            first = ordered[0] if ordered else None
            logger.exception(
                "replay_project_failed",
                extra={
                    "trace_id": first.trace_id if first else "",
                    "run_id": first.run_id if first else "",
                    "task_id": first.task_id if first else "",
                    "event_count": len(ordered),
                },
            )
            raise

    def _project_model_events(self, events: list[TraceEventRecord]) -> list[ReplayNode]:
        """聚合模型事件。

        参数:
            events: 有序 trace events。

        返回:
            model_call/model_output 节点列表。

        异常:
            无。

        副作用:
            无。
        """

        groups = _group_model_events(events)
        nodes: list[ReplayNode] = []
        for model_events in groups:
            text = "".join(str(event.payload.get("text", event.payload.get("delta", ""))) for event in model_events if event.event_type == "model_delta")
            status = _model_status(model_events)
            first = model_events[0]
            last = model_events[-1]
            nodes.append(
                self._node(
                    first,
                    last,
                    ReplayNodeType.MODEL_CALL.value,
                    "模型调用",
                    status,
                    _summary(first.payload, "model", "模型调用"),
                    {"events": [event.to_dict() for event in model_events]},
                    model_events,
                )
            )
            if text:
                nodes.append(
                    self._node(
                        first,
                        last,
                        ReplayNodeType.MODEL_OUTPUT.value,
                        "模型输出",
                        status,
                        text[:160],
                        {"text": text},
                        model_events,
                    )
                )
        return nodes

    def _project_tool_events(self, events: list[TraceEventRecord]) -> list[ReplayNode]:
        """聚合工具生命周期事件。

        参数:
            events: 有序 trace events。

        返回:
            tool_call/tool_result 节点列表。

        异常:
            无。

        副作用:
            无。
        """

        grouped: dict[str, list[TraceEventRecord]] = defaultdict(list)
        for event in events:
            if event.event_type in TOOL_EVENT_TYPES:
                grouped[_tool_call_group_key(event)].append(event)
        nodes: list[ReplayNode] = []
        for tool_call_id, tool_events in grouped.items():
            first = tool_events[0]
            last = tool_events[-1]
            status = _tool_status(tool_events)
            tool_name = str(last.payload.get("tool_name") or first.payload.get("tool_name") or "tool")
            nodes.append(
                self._node(
                    first,
                    last,
                    ReplayNodeType.TOOL_CALL.value,
                    f"工具调用 {tool_name}",
                    status,
                    f"{tool_name}: {status}",
                    {"tool_call_id": tool_call_id, "events": [event.to_dict() for event in tool_events]},
                    tool_events,
                )
            )
            if any(event.event_type in {"tool_execution_completed", "tool_execution_failed", "tool_execution_cancelled", "tool_execution_timed_out"} for event in tool_events):
                nodes.append(
                    self._node(
                        first,
                        last,
                        ReplayNodeType.TOOL_RESULT.value,
                        f"工具结果 {tool_name}",
                        status,
                        str(last.payload.get("summary") or last.payload.get("error") or status),
                        {"tool_call_id": tool_call_id, "result": last.payload},
                        tool_events,
                    )
                )
        return nodes

    def _project_approval_events(
        self,
        events: list[TraceEventRecord],
        approval_chains: dict[str, list[TraceEventRecord]],
    ) -> list[ReplayNode]:
        """投影审批事件。

        参数:
            events: 有序 trace events。
            approval_chains: 按 approval_id 聚合的审批事件。

        返回:
            approval_wait/approval_decision 节点列表。

        异常:
            无。

        副作用:
            无。
        """

        nodes = []
        for event in events:
            if event.event_type == "approval_requested":
                chain = _approval_chain_for_event(event, approval_chains)
                nodes.append(
                    self._node(
                        event,
                        event,
                        ReplayNodeType.APPROVAL_WAIT.value,
                        "等待审批",
                        "waiting",
                        _summary(event.payload, "summary", "等待审批"),
                        _approval_payload(event, chain),
                        chain,
                    )
                )
            elif event.event_type == "approval_decided":
                status = str(event.payload.get("decision") or "completed")
                chain = _approval_chain_for_event(event, approval_chains)
                nodes.append(
                    self._node(
                        event,
                        event,
                        ReplayNodeType.APPROVAL_DECISION.value,
                        "审批决策",
                        status,
                        _summary(event.payload, "decision", "审批决策"),
                        _approval_payload(event, chain),
                        chain,
                    )
                )
        return nodes

    def _project_resume_events(
        self,
        events: list[TraceEventRecord],
        approval_chains: dict[str, list[TraceEventRecord]],
        approval_id_by_resume_command: dict[str, str],
    ) -> list[ReplayNode]:
        """聚合 resume 事件。

        参数:
            events: 有序 trace events。
            approval_chains: 按 approval_id 聚合的审批事件。
            approval_id_by_resume_command: resume_command_id 到 approval_id 的映射。

        返回:
            resume 节点列表。

        异常:
            无。

        副作用:
            无。
        """

        grouped: dict[str, list[TraceEventRecord]] = defaultdict(list)
        for event in events:
            if event.event_type in {"resume_started", "resume_completed", "resume_failed"}:
                grouped[str(event.payload.get("resume_command_id") or f"seq-{event.sequence_no}")].append(event)
        nodes = []
        for command_id, resume_events in grouped.items():
            status = _resume_status(resume_events)
            approval_id = approval_id_by_resume_command.get(command_id, "")
            related_approval_events = approval_chains.get(approval_id, [])
            covered_events = related_approval_events + resume_events
            nodes.append(
                self._node(
                    resume_events[0],
                    resume_events[-1],
                    ReplayNodeType.RESUME.value,
                    "恢复运行",
                    status,
                    _resume_summary(status, resume_events),
                    {
                        "resume_command_id": command_id,
                        "approval_id": approval_id,
                        "failed_attempt_count": sum(1 for event in resume_events if event.event_type == "resume_failed"),
                        "related_approval_event_ids": [event.event_id for event in related_approval_events],
                        "events": [event.to_dict() for event in resume_events],
                    },
                    covered_events,
                )
            )
        return nodes

    def _project_checkpoint_events(self, events: list[TraceEventRecord]) -> list[ReplayNode]:
        """投影 checkpoint 事件。

        参数:
            events: 有序 trace events。

        返回:
            checkpoint 节点列表。

        异常:
            无。

        副作用:
            无。
        """

        return [
            self._single(event, ReplayNodeType.CHECKPOINT.value, "Checkpoint", "failed" if event.event_type == "checkpoint_failed" else "completed")
            for event in events
            if event.event_type in {"checkpoint_created", "checkpoint_failed"}
        ]

    def _project_simple_events(self, events: list[TraceEventRecord]) -> list[ReplayNode]:
        """投影无需聚合的简单事件。

        参数:
            events: 有序 trace events。

        返回:
            简单节点列表。

        异常:
            无。

        副作用:
            无。
        """

        skip = {"model_requested", "model_delta", "model_completed", "model_failed", "approval_requested", "approval_decided", "checkpoint_created", "checkpoint_failed", "resume_started", "resume_completed", "resume_failed"} | TOOL_EVENT_TYPES
        nodes = []
        for event in events:
            if event.event_type in skip:
                continue
            node_type = replay_type_for_event(event.event_type).value
            status = _status_for_event(event.event_type)
            nodes.append(self._single(event, node_type, _title_for_event(event.event_type), status))
        return nodes

    def _single(self, event: TraceEventRecord, node_type: str, title: str, status: str) -> ReplayNode:
        """构造单事件节点。

        参数:
            event: trace event。
            node_type: replay 节点类型。
            title: 节点标题。
            status: 节点状态。

        返回:
            ReplayNode。

        异常:
            无。

        副作用:
            无。
        """

        return self._node(event, event, node_type, title, status, _summary(event.payload, "summary", title), event.payload)

    def _node(
        self,
        first: TraceEventRecord,
        last: TraceEventRecord,
        node_type: str,
        title: str,
        status: str,
        summary: str,
        payload: dict,
        events: list[TraceEventRecord] | None = None,
    ) -> ReplayNode:
        """构造 replay 节点。

        参数:
            first: 覆盖范围内第一个事件。
            last: 覆盖范围内最后一个事件。
            node_type: 节点类型。
            title: 节点标题。
            status: 节点状态。
            summary: 节点摘要。
            payload: 节点详情 payload。
            events: 节点实际覆盖的 trace events，省略时只关联首尾事件。

        返回:
            ReplayNode。

        异常:
            无。

        副作用:
            无。
        """

        covered_events = events or ([first] if first.event_id == last.event_id else [first, last])
        return ReplayNode(
            replay_node_id=f"{first.run_id}:{node_type}:{first.sequence_no}:{last.sequence_no}",
            trace_id=first.trace_id,
            run_id=first.run_id,
            task_id=first.task_id,
            sequence_start=first.sequence_no,
            sequence_end=last.sequence_no,
            node_type=node_type,
            title=title,
            status=status,
            summary=summary,
            payload=payload,
            related_span_ids=list(dict.fromkeys(event.span_id for event in covered_events if event.span_id)),
            related_event_ids=list(dict.fromkeys(event.event_id for event in covered_events)),
            related_artifact_ids=_artifact_ids(payload),
            checkpoint_id=_checkpoint_id(payload),
            debug=_debug_payload(covered_events),
            started_at=first.created_at,
            ended_at=last.created_at,
            duration_ms=_duration_ms(first.created_at, last.created_at),
        )


def _tool_call_group_key(event: TraceEventRecord) -> str:
    """返回工具事件聚合键。

    参数:
        event: 单条 trace event。

    返回:
        优先使用 trace payload 中持久化的 tool_call_id；缺失时降级为单事件键。

    异常:
        无。

    副作用:
        无。
    """

    tool_call_id = event.payload.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    return f"missing-tool-call-id:{event.event_id}"


def _tool_status(events: list[TraceEventRecord]) -> str:
    """根据工具事件列表推导状态。

    参数:
        events: 同一 tool_call_id 的事件列表。

    返回:
        工具状态字符串。

    异常:
        无。

    副作用:
        无。
    """

    event_types = {event.event_type for event in events}
    if "tool_execution_failed" in event_types:
        return "failed"
    if "tool_execution_cancelled" in event_types:
        return "cancelled"
    if "tool_execution_timed_out" in event_types:
        return "timed_out"
    if "tool_execution_completed" in event_types:
        return "completed"
    if "tool_execution_started" in event_types:
        return "running"
    return "planned"


def _resume_status(events: list[TraceEventRecord]) -> str:
    """根据 resume 事件的最终状态推导节点状态。

    参数:
        events: 同一 resume_command_id 的事件列表。

    返回:
        completed、failed 或 running。

    异常:
        无。

    副作用:
        无。
    """

    for event in reversed(events):
        if event.event_type == "resume_completed":
            return "completed"
        if event.event_type == "resume_failed":
            return "failed"
        if event.event_type == "resume_started":
            return "running"
    return "running"


def _resume_summary(status: str, events: list[TraceEventRecord]) -> str:
    """返回 resume 节点摘要。

    参数:
        status: 已推导出的 resume 状态。
        events: 同一 resume_command_id 的事件列表。

    返回:
        人类可读摘要。

    异常:
        无。

    副作用:
        无。
    """

    failed_attempt_count = sum(1 for event in events if event.event_type == "resume_failed")
    if failed_attempt_count and status == "completed":
        return f"resume completed after {failed_attempt_count} failed attempt"
    if failed_attempt_count and status == "failed":
        return f"resume failed after {failed_attempt_count} failed attempt"
    return f"resume {status}"


def _approval_chains(events: list[TraceEventRecord]) -> dict[str, list[TraceEventRecord]]:
    """按 approval_id 聚合审批事件。

    参数:
        events: 有序 trace events。

    返回:
        approval_id 到事件列表的映射。

    异常:
        无。

    副作用:
        无。
    """

    grouped: dict[str, list[TraceEventRecord]] = defaultdict(list)
    for event in events:
        if event.event_type not in {"approval_requested", "approval_decided"}:
            continue
        approval_id = event.payload.get("approval_id")
        if isinstance(approval_id, str) and approval_id:
            grouped[approval_id].append(event)
    return grouped


def _approval_chain_for_event(
    event: TraceEventRecord,
    approval_chains: dict[str, list[TraceEventRecord]],
) -> list[TraceEventRecord]:
    """返回当前审批事件所属链路。

    参数:
        event: 审批 trace event。
        approval_chains: 按 approval_id 聚合的审批事件。

    返回:
        同一 approval_id 的事件列表；缺失 approval_id 时返回当前事件。

    异常:
        无。

    副作用:
        无。
    """

    approval_id = event.payload.get("approval_id")
    if isinstance(approval_id, str) and approval_id:
        return approval_chains.get(approval_id, [event])
    return [event]


def _approval_payload(event: TraceEventRecord, chain: list[TraceEventRecord]) -> dict:
    """构造带审批链引用的节点 payload。

    参数:
        event: 当前审批事件。
        chain: 同一 approval_id 的审批事件列表。

    返回:
        包含链路引用的 payload。

    异常:
        无。

    副作用:
        无。
    """

    return {
        **event.payload,
        "approval_chain_event_ids": [item.event_id for item in chain],
        "approval_chain_event_types": [item.event_type for item in chain],
    }


def _approval_id_by_resume_command(events: list[TraceEventRecord]) -> dict[str, str]:
    """建立 resume_command_id 到 approval_id 的映射。

    参数:
        events: 有序 trace events。

    返回:
        resume_command_id 到 approval_id 的映射。

    异常:
        无。

    副作用:
        无。
    """

    mapping: dict[str, str] = {}
    for event in events:
        if event.event_type != "approval_decided":
            continue
        resume_command_id = event.payload.get("resume_command_id")
        approval_id = event.payload.get("approval_id")
        if isinstance(resume_command_id, str) and resume_command_id and isinstance(approval_id, str):
            mapping[resume_command_id] = approval_id
    return mapping


def _group_model_events(events: list[TraceEventRecord]) -> list[list[TraceEventRecord]]:
    """按一次模型调用边界聚合模型事件。

    参数:
        events: 有序 trace events。

    返回:
        每个元素是一组属于同一次模型调用的事件。

    异常:
        无。

    副作用:
        无。
    """

    groups: list[list[TraceEventRecord]] = []
    current: list[TraceEventRecord] = []
    for event in events:
        if event.event_type not in {"model_requested", "model_delta", "model_completed", "model_failed"}:
            continue
        if event.event_type == "model_requested" and current:
            groups.append(current)
            current = []
        current.append(event)
        if event.event_type in {"model_completed", "model_failed"}:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _model_status(events: list[TraceEventRecord]) -> str:
    """根据模型事件组推导节点状态。

    参数:
        events: 同一次模型调用的事件列表。

    返回:
        failed、completed 或 running。

    异常:
        无。

    副作用:
        无。
    """

    event_types = {event.event_type for event in events}
    if "model_failed" in event_types:
        return "failed"
    if "model_completed" in event_types:
        return "completed"
    return "running"


def _status_for_event(event_type: str) -> str:
    """根据事件类型推导节点状态。

    参数:
        event_type: trace event 类型。

    返回:
        状态字符串。

    异常:
        无。

    副作用:
        无。
    """

    if event_type == "run_failed":
        return "failed"
    if event_type == "run_cancelled":
        return "cancelled"
    if event_type in TERMINAL_EVENT_TYPES:
        return "completed"
    if event_type == "run_started":
        return "running"
    return "completed"


def _title_for_event(event_type: str) -> str:
    """返回事件的默认标题。

    参数:
        event_type: trace event 类型。

    返回:
        人类可读标题。

    异常:
        无。

    副作用:
        无。
    """

    return event_type.replace("_", " ").title()


def _summary(payload: dict, preferred_key: str, fallback: str) -> str:
    """从 payload 中提取摘要。

    参数:
        payload: 事件 payload。
        preferred_key: 优先读取的字段。
        fallback: 无摘要时使用的文本。

    返回:
        摘要文本。

    异常:
        无。

    副作用:
        无。
    """

    value = payload.get(preferred_key) or payload.get("status") or payload.get("error") or fallback
    return str(value)[:200]


def _artifact_ids(payload: dict) -> list[str]:
    """从 payload 中提取 artifact id 列表。

    参数:
        payload: 节点 payload。

    返回:
        artifact id 列表。

    异常:
        无。

    副作用:
        无。
    """

    found: list[str] = []

    def visit(value) -> None:
        """递归扫描 trace payload 中的 artifact 引用。

        参数:
            value: 当前被扫描的 JSON 值。

        返回:
            无。

        异常:
            无。

        副作用:
            向外层 found 列表追加已发现的 artifact id。
        """

        if isinstance(value, dict):
            artifact_id = value.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id:
                found.append(artifact_id)
            artifact_ids = value.get("artifact_ids")
            if isinstance(artifact_ids, list):
                found.extend(item for item in artifact_ids if isinstance(item, str) and item)
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return list(dict.fromkeys(found))


def _checkpoint_id(payload: dict) -> str:
    """从 payload 中提取 checkpoint id。

    参数:
        payload: 节点 payload。

    返回:
        checkpoint_id；不存在时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """

    checkpoint_id = payload.get("checkpoint_id")
    if isinstance(checkpoint_id, str):
        return checkpoint_id
    checkpoint = payload.get("checkpoint")
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("checkpoint_id"), str):
        return str(checkpoint["checkpoint_id"])
    return ""


def _debug_payload(events: list[TraceEventRecord]) -> dict:
    """构造节点级 debug 引用。

    参数:
        events: 节点覆盖的 trace events。

    返回:
        包含事件类型、source 和关键关联键的 debug 字典。

    异常:
        无。

    副作用:
        无。
    """

    tool_call_ids = _payload_values(events, "tool_call_id")
    approval_ids = _payload_values(events, "approval_id")
    resume_command_ids = _payload_values(events, "resume_command_id")
    checkpoint_ids = _payload_values(events, "checkpoint_id")
    debug = {
        "event_types": list(dict.fromkeys(event.event_type for event in events)),
        "sources": list(dict.fromkeys(event.source for event in events if event.source)),
        "sequence_range": [events[0].sequence_no, events[-1].sequence_no] if events else [],
    }
    if tool_call_ids:
        debug["tool_call_ids"] = tool_call_ids
    if approval_ids:
        debug["approval_ids"] = approval_ids
    if resume_command_ids:
        debug["resume_command_ids"] = resume_command_ids
    if checkpoint_ids:
        debug["checkpoint_ids"] = checkpoint_ids
    return debug


def _timeline_debug(events: list[TraceEventRecord]) -> dict:
    """构造 timeline 级 debug 信息。

    参数:
        events: 同一 run 或 trace 的有序事件。

    返回:
        事件数量、source 和 sequence 范围。

    异常:
        无。

    副作用:
        无。
    """

    return {
        "event_count": len(events),
        "sources": list(dict.fromkeys(event.source for event in events if event.source)),
        "sequence_range": [events[0].sequence_no, events[-1].sequence_no] if events else [],
    }


def _payload_values(events: list[TraceEventRecord], key: str) -> list[str]:
    """提取一组事件 payload 中的字符串字段值。

    参数:
        events: trace events。
        key: 需要读取的 payload 字段名。

    返回:
        去重后的非空字符串列表。

    异常:
        无。

    副作用:
        无。
    """

    values = [event.payload.get(key) for event in events]
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _duration_ms(started_at: datetime, ended_at: datetime) -> int:
    """计算两个时间之间的毫秒差。

    参数:
        started_at: 开始时间。
        ended_at: 结束时间。

    返回:
        非负毫秒数。

    异常:
        无。

    副作用:
        无。
    """

    return max(0, int((ended_at - started_at).total_seconds() * 1000))
