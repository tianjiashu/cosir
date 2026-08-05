/**
 * 单轮（turn）timeline 渲染组件。
 *
 * 设计要点（性能）：把「整棵 timeline 一次性重投影」拆成「每个 turn 独立投影 + memo」。
 * 当其他 turn 收到流式 delta 时，本组件因 `events` 引用不变而被 `memo` 跳过，
 * 既不重投影也不重渲染——这是「多次对话后整棵 timeline 重算」卡顿的根因解法，
 * 并使单次 token 的渲染代价只与「活跃 turn 体积」成正比，与总 turn 数 / 总事件数解耦，
 * 对未来 subagent、context compaction、新事件类型等高迭代场景天然可扩展。
 *
 * @module components/layout/TurnTimeline
 */

import { memo, useEffect, useMemo, useReducer, useRef } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { UserMessage } from "@/components/chat/UserMessage";
import { AgentMessage } from "@/components/chat/AgentMessage";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { TerminalCallCard } from "@/components/chat/TerminalCallCard";
import { StatusBadge } from "@/components/chat/StatusBadge";
import {
  createTimelineProjectorState,
  type TimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
} from "@/services/timeline/projector";
import { openFileInEditor } from "@/services/backend";
import { logInfo, logWarn } from "@/lib/logger";

/** TurnTimeline 的 props。 */
interface TurnTimelineProps {
  /** 轮次记录（用户输入 + 元数据）。 */
  turn: TurnRecord;
  /** 该轮次自身的运行时事件（已按 turn_id 分片，引用在其它 turn 收事件时保持不变）。 */
  events: RuntimeEvent[];
}

/**
 * 单轮 timeline 渲染实现。
 *
 * 每个 turn 独立投影并保持 memo：仅当本 turn 的 `events` 或 `turn` 引用变化时
 * 才重投影，其余 turn 在父组件重渲染时整块跳过。
 *
 * 增量投影（性能核心）：
 * 本组件持有可续算的 {@link TimelineProjectorState}，每帧只把 `events` 的「新增尾部」
 * （append-only 保证 delta = events.slice(lastLen)）增量投影进既有状态，而非从零全量重建。
 * 投影结果 `entries` 引用稳定（未变项沿用旧引用），使下游 `memo` 精确跳过未变化项，
 * 把「每帧 O(n) 全量重投影」降为「每帧 O(delta)」，彻底消除流式期整棵 timeline 重算卡顿。
 *
 * @param props.turn - 轮次记录。
 * @param props.events - 该轮次事件列表。
 */
function TurnTimelineImpl({ turn, events }: TurnTimelineProps) {
  // 可续算投影状态（持久引用，跨帧累积；不随 render 重建）。
  const stateRef = useRef<TimelineProjectorState>(createTimelineProjectorState());
  // 已投影到 stateRef 的 events 长度；events 为 append-only，delta = events.slice(lastLen)。
  const lastLenRef = useRef(0);
  // 已投影 events 的首个 event_id；用于检测「头部插入」式整体替换
  // （setEvents 经排序/合并后可能在数组头部插入更早的历史事件，此时长度可能不减反增，
  // 仅比长度无法识别，必须靠首事件身份）。身份变化则整体重建而非按尾部切片续算。
  const lastFirstEventIdRef = useRef<string | null>(null);
  // 触发重渲染的轻量信号；renderTick 同时作为下游 useMemo 的显式依赖——
  // ref 的 .current 变化 React 侦测不到，必须靠递增计数传达「投影状态已更新」。
  const [renderTick, forceRender] = useReducer((x: number) => x + 1, 0);

  useEffect(() => {
    // 首帧或 turn 切换：重建投影状态（turn 变了，旧累积态无效）。
    if (lastLenRef.current === 0 && events.length > 0) {
      stateRef.current = projectTimelineIncrementally(createTimelineProjectorState(), events);
      lastLenRef.current = events.length;
      lastFirstEventIdRef.current = events[0]?.event_id ?? null;
      forceRender();
      return;
    }
    const firstEventId = events[0]?.event_id ?? null;
    if (
      events.length < lastLenRef.current ||
      (firstEventId !== null && firstEventId !== lastFirstEventIdRef.current)
    ) {
      // events 被整体替换（如回放/重连，或头部插入更早历史事件）：重建而非续算，
      // 避免脏累积或把尾部误当新增；projectTimelineIncrementally 幂等，重建安全。
      stateRef.current = projectTimelineIncrementally(createTimelineProjectorState(), events);
      lastLenRef.current = events.length;
      lastFirstEventIdRef.current = firstEventId;
      forceRender();
      return;
    }
    const delta = events.slice(lastLenRef.current);
    if (delta.length > 0) {
      stateRef.current = projectTimelineIncrementally(stateRef.current, delta);
      lastLenRef.current = events.length;
      lastFirstEventIdRef.current = firstEventId;
      forceRender();
    }
  }, [events]);

  const turnItem = useMemo(() => {
    const entries = selectVisibleEntries(stateRef.current).slice();
    if (entries.length === 0 && turn.response_text) {
      entries.push({
        kind: "assistant",
        eventId: `turn-response-${turn.turn_id}`,
        content: turn.response_text,
      });
    }
    return { turnId: turn.turn_id, userText: turn.input_text, entries };
    // renderTick 是「stateRef.current 已更新」的唯一可观测信号；
    // ESLint 无法追踪 ref 读取，故此依赖必要而非冗余。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turn, renderTick]);

  if (turnItem.entries.length === 0 && !turn.response_text) {
    return null;
  }

  return (
    <div className="space-y-3">
      <div className="mx-auto w-full min-w-0 max-w-content">
        <UserMessage content={turnItem.userText} />
      </div>

      {turnItem.entries.map((entry) => {
        // 所有 timeline 条目统一限宽 content 令牌，与用户消息、输入栏保持宽度对齐，
        // 避免 diff/write 工具卡片单独 breakout 导致右侧参差不齐。
        const widthClass = "mx-auto w-full min-w-0 max-w-content";

        if (entry.kind === "thinking") {
          // 过滤纯空白与极短无意义内容（至少 2 个字符才值得展示折叠块）
          const trimmed = entry.content.trim();
          if (trimmed.length >= 2) {
            logInfo("thinking_rendered", {
              module: "TurnTimeline",
              event_id: entry.eventId,
              content_len: entry.content.length,
              trimmed_len: trimmed.length,
            });
            return (
              <div key={entry.eventId} className={widthClass}>
                <ThinkingBlock content={entry.content} streaming={entry.streaming} />
              </div>
            );
          }
          logWarn("thinking_filtered_out", {
            module: "TurnTimeline",
            event_id: entry.eventId,
            content_len: entry.content.length,
            trimmed_len: trimmed.length,
            reason: "content too short (<2 chars), block hidden",
          });
          return null;
        }
        if (entry.kind === "assistant") {
          return entry.content.length > 0 ? (
            <div key={entry.eventId} className={widthClass}>
              <AgentMessage content={entry.content} streaming={entry.streaming} />
            </div>
          ) : null;
        }
        if (entry.kind === "status") {
          return (
            <div key={entry.eventId} className={widthClass}>
              <StatusBadge eventType={entry.eventType} payload={entry.payload} />
            </div>
          );
        }
        const tool = entry.item;
        const card = tool.display?.expandLayout === "terminal" ? (
          <TerminalCallCard
            toolName={tool.toolName}
            status={tool.status}
            command={tool.arguments?.command as string | undefined}
            args={tool.arguments}
            display={tool.display}
            result={tool.result}
            error={tool.error}
            reason={tool.reason}
            retryable={tool.retryable}
            resultData={tool.resultData}
          />
        ) : tool.status === "running" ? (
          <ToolCallCard
            toolName={tool.toolName}
            status="running"
            args={tool.arguments}
            display={tool.display}
            requestSummary={tool.requestSummary}
            resultData={tool.resultData}
            onOpenFile={(path) => {
              void openFileInEditor(path);
            }}
          />
        ) : (
          <ToolCallCard
            toolName={tool.toolName}
            status={tool.status}
            error={tool.error}
            args={tool.arguments}
            display={tool.display}
            resultSummary={tool.resultSummary}
            result={tool.result}
            reason={tool.reason}
            retryable={tool.retryable}
            requestSummary={tool.requestSummary}
            listEntries={tool.listEntries}
            emptyLabel={tool.emptyLabel}
            resultData={tool.resultData}
            onOpenFile={(path) => {
              void openFileInEditor(path);
            }}
          />
        );
        return <div key={tool.eventId} className={widthClass}>{card}</div>;
      })}
    </div>
  );
}

/**
 * 单轮 timeline 渲染组件（memo 包裹）。
 *
 * 仅当 `turn` 或 `events` 引用变化时才重渲染，使其它 turn 的事件流不触发本组件
 * 投影与渲染，从结构上消除「多次对话后整棵 timeline 重算」的卡顿。
 */
export const TurnTimeline = memo(TurnTimelineImpl);
