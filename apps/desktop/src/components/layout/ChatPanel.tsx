/**
 * 中央主会话区（ChatPanel）。
 *
 * 展示当前任务下的 turn timeline：用户输入来自 turnStore，
 * assistant/tool/status 项由 timeline projector 按 runtime event 顺序投影。
 *
 * @module components/layout/ChatPanel
 */

import { useEffect, useMemo, useRef } from "react";
import { useShallow } from "zustand/react/shallow";
import { useEventStore, selectLatestEvent, selectEventsForTask, EMPTY_EVENTS } from "@/stores/eventStore";
import { useTaskStore, selectActiveTask } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { ScrollArea } from "@/components/ui/scroll-area";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import type { TurnRecord } from "@shared/turn";

/**
 * 判断当前是否无活跃任务（即空会话）。
 *
 * 真实语义仅取决于是否存在活跃任务；空状态是否展示由 timelineTurns 是否为空决定，
 * 不由事件计数驱动。函数名如实反映行为，避免误导后续维护者。
 *
 * @param activeTask - 当前活跃任务（可能为 null）。
 * @returns 无活跃任务时返回 true。
 */
function isNoActiveTask(activeTask: ReturnType<typeof selectActiveTask>): boolean {
  return !activeTask;
}

/**
 * ChatPanel 中央主会话区组件。
 *
 * 消息列表区域使用 ScrollArea 包裹，
 * 新消息自动滚动到底部（通过 scrollIntoView 实现）。
 *
 * 数据来源：
 * - 用户消息：turnStore 中的 turn.input_text
 * - Agent 输出、工具、状态：timeline projector 按事件顺序投影后的显示项
 */
export function ChatPanel() {
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  // 按 turn 分片的事件映射：每个 turn 的数组引用在其它 turn 收事件时保持不变，
  // 使 <TurnTimeline> 的 memo 能精确跳过未变化的 turn，只重渲染活跃 turn。
  const eventsByTurnId = useEventStore(useShallow((s) => s.eventsByTurnId));
  const events = useEventStore(useShallow((s) => selectEventsForTask(s, activeTaskId)));
  const latestEvent = useEventStore((s) => selectLatestEvent(s, activeTaskId));
  const activeTask = useTaskStore(selectActiveTask);
  const turns = useTurnStore(useShallow((s) => (activeTask ? s.turnsByTaskId[activeTask.task_id] ?? [] : [])));
  const scrollEndRef = useRef<HTMLDivElement>(null);

  const timelineTurns = useMemo(() => {
    if (turns.length > 0) {
      return turns;
    }
    if (activeTask) {
      const fallbackTurn: TurnRecord = {
        turn_id: activeTask.latest_turn_id ?? activeTask.task_id,
        task_id: activeTask.task_id,
        input_text: activeTask.input_text,
        status: "pending",
        end_reason: null,
        response_text: null,
        created_at: activeTask.created_at,
        updated_at: activeTask.updated_at,
      };
      return [fallbackTurn];
    }
    return [];
  }, [activeTask, turns]);

  // 是否显示空状态（仅取决于是否有活跃任务；具体空态由 timelineTurns 决定）
  const emptySession = isNoActiveTask(activeTask);

  // 当有新事件或新消息时自动滚动到底部。
  // 节流：流式期间事件高频到达，若每次都触发 smooth 滚动动画会导致大量重排重绘而卡顿。
  // 用 rAF + 节流（每 200ms 至多滚动一次），保证跟随最新内容的同时不阻塞渲染。
  const lastScrollAt = useRef(0);
  useEffect(() => {
    const now = Date.now();
    if (now - lastScrollAt.current < 200) {
      return;
    }
    lastScrollAt.current = now;
    if (scrollEndRef.current) {
      scrollEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [events.length, latestEvent]);

  return (
    <main className="flex flex-1 flex-col overflow-hidden bg-background">
      {/* 消息列表区域 */}
      <ScrollArea className="flex-1 scrollbar-thin">
        <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
          {/* 空会话提示 */}
          {emptySession && (
            <div className="flex items-center justify-center py-20 text-sm text-muted-foreground">
              在下方输入框发送指令开始对话
            </div>
          )}

          {/* 每个 turn 独立投影 + memo：只有收事件的活跃 turn 重渲染，历史 turn 整块跳过 */}
          {timelineTurns.map((turn) => (
            <TurnTimeline
              key={turn.turn_id}
              turn={turn}
              events={eventsByTurnId[turn.turn_id] ?? EMPTY_EVENTS}
            />
          ))}

          {/* 滚动锚点 */}
          <div ref={scrollEndRef} className="h-1" />
        </div>
      </ScrollArea>
    </main>
  );
}
