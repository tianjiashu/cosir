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

import { memo, useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { UserMessage } from "@/components/chat/UserMessage";
import { ThinkingIndicator } from "@/components/chat/ThinkingIndicator";
import { AgentMessage } from "@/components/chat/AgentMessage";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { ToolCallGroup } from "@/components/chat/ToolCallGroup";
import { TerminalCallCard } from "@/components/chat/TerminalCallCard";
import { StatusBadge } from "@/components/chat/StatusBadge";
import { DelegationTimelineEntry } from "@/components/chat/DelegationTimelineEntry";
import {
  createTimelineProjectorState,
  type TimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
} from "@/services/timeline/projector";
import {
  groupConsecutiveTools,
  type RenderEntry,
} from "@/services/timeline/groupTools";
import { openFileInEditor } from "@/services/backend";
import { logInfo, logWarn } from "@/lib/logger";
import { PerfTrace } from "@/lib/perf";

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
  // 已投影 events 的末位 event_id；用于检测「中段插入」式乱序归位
  // （appendOrderedShard 遇乱序会整体重排，新事件可能落在已投影区间内部，
  // 此时首事件身份与长度增长方向均不变，纯尾部切片会把中段事件永久漏投）。
  // 校验点：append-only 时 events[lastLen-1] 必为旧末位；不等于旧末位即发生过中段插入。
  const lastTailEventIdRef = useRef<string | null>(null);
  // 触发重渲染的轻量信号；renderTick 同时作为下游 useMemo 的显式依赖——
  // ref 的 .current 变化 React 侦测不到，必须靠递增计数传达「投影状态已更新」。
  const [renderTick, forceRender] = useReducer((x: number) => x + 1, 0);

  useEffect(() => {
    const t0 = performance.now();
    // 首帧或 turn 切换：重建投影状态（turn 变了，旧累积态无效）。
    if (lastLenRef.current === 0 && events.length > 0) {
      stateRef.current = projectTimelineIncrementally(createTimelineProjectorState(), events);
      lastLenRef.current = events.length;
      lastFirstEventIdRef.current = events[0]?.event_id ?? null;
      lastTailEventIdRef.current = events[events.length - 1]?.event_id ?? null;
      PerfTrace.markCurrent("timeline:project-first-frame", {
        turn_id: turn.turn_id,
        events: events.length,
        project_ms: Number((performance.now() - t0).toFixed(2)),
      });
      // 首帧投影完成 = 用户操作链路语义终点：结束当前 PerfTrace 链路，
      // 避免模块级 currentTrace 残留导致 openTask 等非用户操作渲染误挂旧 traceId。
      PerfTrace.endCurrent();
      forceRender();
      return;
    }
    const firstEventId = events[0]?.event_id ?? null;
    // 中段插入探测：append-only 语义下，已投影区间的末位（events[lastLen-1]）必然仍是
    // 上次投影的旧末位；若身份不同，说明乱序归位把新事件插进了已投影区间内部，
    // 尾部切片会漏投该事件，必须整体重建（projectTimelineIncrementally 幂等，重建安全）。
    const midInsertDetected =
      lastLenRef.current > 0 &&
      events.length >= lastLenRef.current &&
      (events[lastLenRef.current - 1]?.event_id ?? null) !== lastTailEventIdRef.current;
    if (
      events.length < lastLenRef.current ||
      (firstEventId !== null && firstEventId !== lastFirstEventIdRef.current) ||
      midInsertDetected
    ) {
      // events 被整体替换（如回放/重连，或头部插入更早历史事件，或中段乱序归位）：
      // 重建而非续算，避免脏累积或把尾部误当新增。
      stateRef.current = projectTimelineIncrementally(createTimelineProjectorState(), events);
      lastLenRef.current = events.length;
      lastFirstEventIdRef.current = firstEventId;
      lastTailEventIdRef.current = events[events.length - 1]?.event_id ?? null;
      PerfTrace.markCurrent("timeline:project-rebuild", {
        turn_id: turn.turn_id,
        events: events.length,
        project_ms: Number((performance.now() - t0).toFixed(2)),
      });
      forceRender();
      return;
    }
    const delta = events.slice(lastLenRef.current);
    if (delta.length > 0) {
      stateRef.current = projectTimelineIncrementally(stateRef.current, delta);
      lastLenRef.current = events.length;
      lastFirstEventIdRef.current = firstEventId;
      lastTailEventIdRef.current = events[events.length - 1]?.event_id ?? null;
      PerfTrace.markCurrent("timeline:project-incremental", {
        turn_id: turn.turn_id,
        delta: delta.length,
        total: events.length,
        project_ms: Number((performance.now() - t0).toFixed(2)),
      });
      forceRender();
    }
  }, [events, turn.turn_id]);

  const turnItem = useMemo(() => {
    // turn 处于活动态（pending/running）才允许 pending 块以 streaming 推入；
    // 终态下即便 stateRef 仍有未 flush 的 pending 残留（典型：SSE 断开 →
    // 后端不再投递 run_* 终态事件，flushPending 永远不被触发），
    // 也按定稿态渲染，避免思考块永久展开 + 与后续 turn 渲染区重叠挤压空间。
    const isTurnActive = turn.status === "pending" || turn.status === "running";
    const entries = selectVisibleEntries(stateRef.current, isTurnActive).slice();
    if (entries.length === 0 && turn.response_text) {
      entries.push({
        kind: "assistant",
        eventId: `turn-response-${turn.turn_id}`,
        content: turn.response_text,
      });
    }
    return {
      turnId: turn.turn_id,
      userText: turn.input_text,
      entries,
      // 把「长度 ≥ 2 的连续 tool 段」聚合成一行摘要，避免同 turn 多工具纵向过长。
      renderEntries: groupConsecutiveTools(entries),
    };
    // renderTick 是「stateRef.current 已更新」的唯一可观测信号；
    // ESLint 无法追踪 ref 读取，故此依赖必要而非冗余。
    // turn.status 变化需触发重算（活动 → 终态应即时折叠 pending 块）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turn, turn.status, renderTick]);

  // 打开文件回调必须保持引用稳定：本组件每帧重渲染都会重建内联箭头函数，
  // 若直接内联传入 ToolCallCard/TerminalCallCard，会击穿其 memo（props 引用变化），
  // 导致所有工具卡片每帧重渲染。用 useCallback 固定引用，使未变化的工具条目真正跳过。
  const handleOpenFile = useCallback((path: string) => {
    void openFileInEditor(path);
  }, []);

  // 「等待首 token」判定：用户已输入（input_text 非空）、请求已提交，但模型首 token
  // 尚未返回的空窗期——既无投影条目也无最终回复。此时渲染「思考中」指示器，
  // 填补用户输入与首个 runtime 事件（thinking/assistant/tool）之间的视觉空档。
  // 注意：判定基于「有输入 + 无条目 + 无回复」而非单纯依赖 turn.status（真实 turn
  // 回写后可能为 running，而首 token 仍未到达，entries 依旧为空），与下方空态早返回
  // 条件互斥（空态还要求 input_text 也为空）。
  const isAwaitingFirstToken =
    !!turn.input_text && turnItem.entries.length === 0 && !turn.response_text;

  // 空态早返回：仅当「无用户输入、无事件、无最终回复」三者皆空时才视为空白 turn 不渲染。
  // 注意：有 input_text 的 pending turn（乐观更新插入的临时 turn / 首个 SSE 事件到达前的真实
  // turn）不能在此被丢弃，否则用户刚输入的指令要等首个事件才出现，违背「立即渲染用户输入」的预期。
  // 有 input_text 即至少渲染 <UserMessage>，让用户在请求响应前就能看到自己发的消息。
  if (!turn.input_text && turnItem.entries.length === 0 && !turn.response_text) {
    return null;
  }

  return (
    <div className="space-y-3">
      <div className="mx-auto w-full min-w-0 max-w-content">
        <UserMessage content={turnItem.userText} />
      </div>

      {isAwaitingFirstToken && (
        <div className="mx-auto w-full min-w-0 max-w-content">
          <ThinkingIndicator />
        </div>
      )}

      {turnItem.renderEntries.map((entry) => (
        // 以稳定 key 配合下方 memo 包裹的 TimelineEntry：
        // 当投影器保证「未变化条目沿用旧引用」时，父组件每帧重渲染只会真正重算
        // 内容/引用变化的那一条目（如流式追加的 assistant 块），其余条目被 React 跳过。
        // tool 条目优先用 callId 作 key：entry.item.eventId 在 running→completed 时会
        // 从 started 事件 id 变为 finished 事件 id（projector 行 259），若直接作 key 会导致
        // 工具完成瞬间 React 卸载旧 TimelineEntry、挂载新实例，重置 ToolCallCard 展开态。
        // 用 callId 可保证 key 在条目整个生命周期内恒定；
        // toolGroup 用聚合 groupId 作 key，保证流式期组引用稳定、不重置展开态。
        <TimelineEntry
          key={
            entry.kind === "toolGroup"
              ? entry.groupId
              : entry.kind === "delegation"
                ? entry.item.delegationId
              : entry.kind === "tool"
                ? entry.item.callId ?? entry.item.eventId
                : entry.eventId
          }
          entry={entry}
          onOpenFile={handleOpenFile}
        />
      ))}
    </div>
  );
}

/**
 * 单条 timeline 条目的渲染单元，使用 React.memo 包裹。
 *
 * 设计目的：TurnTimelineImpl 在流式期每帧都会因 forceRender 重渲染并重建 entries 数组，
 * 但投影器保证「内容未变化」的条目沿用旧引用。把单条目渲染抽成独立 memo 组件后，
 * 父重渲染时只要 entry 引用/内容不变（浅比较命中），React 会直接跳过该条目，
 * 不会重新执行内部 ThinkingBlock / AgentMessage 等渲染与 Markdown 解析逻辑，
 * 从源头消除「历史流每帧重复解析已完成块」的卡顿。
 */
const TimelineEntry = memo(function TimelineEntry({
  entry,
  onOpenFile,
}: {
  entry: RenderEntry;
  onOpenFile: (path: string) => void;
}) {
  // 所有 timeline 条目统一限宽 content 令牌，与用户消息、输入栏保持宽度对齐，
  // 避免 diff/write 工具卡片单独 breakout 导致右侧参差不齐。
  const widthClass = "mx-auto w-full min-w-0 max-w-content";

  // 连续工具段聚合：收成一行「运行了 N 个工具 ▾」，展开才见明细。
  if (entry.kind === "toolGroup") {
    return (
      <div className={widthClass}>
        <ToolCallGroup groupId={entry.groupId} items={entry.items} onOpenFile={onOpenFile} />
      </div>
    );
  }

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
        <div className={widthClass}>
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
      <div className={widthClass}>
        <AgentMessage content={entry.content} streaming={entry.streaming} />
      </div>
    ) : null;
  }
  if (entry.kind === "status") {
    return (
      <div className={widthClass}>
        <StatusBadge eventType={entry.eventType} payload={entry.payload} />
      </div>
    );
  }
  if (entry.kind === "delegation") {
    const delegation = entry.item;
    return (
      <div className={widthClass}>
        <DelegationTimelineEntry
          childAgentId={delegation.childAgentId}
          status={delegation.status}
          childTurnId={delegation.childTurnId}
          delegationType={delegation.delegationType}
          summary={delegation.summary}
          error={delegation.error}
          childEntries={[]}
        />
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
      output={tool.output}
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
      onOpenFile={onOpenFile}
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
      notice={tool.notice}
      resultData={tool.resultData}
      onOpenFile={onOpenFile}
    />
  );
  return <div className={widthClass}>{card}</div>;
});

/**
 * 单轮 timeline 渲染组件（memo 包裹）。
 *
 * 仅当 `turn` 或 `events` 引用变化时才重渲染，使其它 turn 的事件流不触发本组件
 * 投影与渲染，从结构上消除「多次对话后整棵 timeline 重算」的卡顿。
 */
export const TurnTimeline = memo(TurnTimelineImpl);
