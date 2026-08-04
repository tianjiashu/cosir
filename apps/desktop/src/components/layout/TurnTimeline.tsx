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

import { memo, useMemo } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { UserMessage } from "@/components/chat/UserMessage";
import { AgentMessage } from "@/components/chat/AgentMessage";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { TerminalCallCard } from "@/components/chat/TerminalCallCard";
import { StatusBadge } from "@/components/chat/StatusBadge";
import { projectTurnTimeline } from "@/services/timeline/projector";
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
 * @param props.turn - 轮次记录。
 * @param props.events - 该轮次事件列表。
 */
function TurnTimelineImpl({ turn, events }: TurnTimelineProps) {
  // 仅依赖本 turn 的 events / turn，过去轮次引用不变时 memo 跳过，不重投影。
  const projected = useMemo(() => projectTurnTimeline([turn], events), [turn, events]);
  const turnItem = projected[0];
  logInfo("turn_timeline_rendered", {
    module: "TurnTimeline",
    turn_id: turn.turn_id,
    user_text_len: turnItem?.userText?.length ?? 0,
    user_text_preview: turnItem?.userText?.slice(0, 80) ?? "",
    entries_count: turnItem?.entries?.length ?? 0,
  });
  if (!turnItem) {
    return null;
  }

  return (
    <div className="space-y-4">
      <div className="mx-auto max-w-3xl">
        <UserMessage content={turnItem.userText} />
      </div>

      {turnItem.entries.map((entry) => {
        // 所有 timeline 条目统一限宽 max-w-3xl，与用户消息、输入栏保持宽度对齐，
        // 避免 diff/write 工具卡片单独 breakout 导致右侧参差不齐。
        const widthClass = "mx-auto max-w-3xl";

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
