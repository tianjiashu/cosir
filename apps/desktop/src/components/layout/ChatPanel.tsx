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
import { useEventStore, selectLatestEvent, selectEventCount, selectEventsForTask } from "@/stores/eventStore";
import { useTaskStore, selectActiveTask } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { ScrollArea } from "@/components/ui/scroll-area";
import { UserMessage } from "@/components/chat/UserMessage";
import { AgentMessage } from "@/components/chat/AgentMessage";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { openFileInEditor } from "@/services/backend";
import { StatusBadge } from "@/components/chat/StatusBadge";
import { projectTurnTimeline } from "@/services/timeline/projector";
import type { TurnRecord } from "@shared/turn";

/**
 * 判断当前是否处于"空会话"状态：无活跃任务或零事件且任务从未运行过。
 */
function isEmptySession(activeTask: ReturnType<typeof selectActiveTask>, eventCount: number): boolean {
  if (!activeTask) return true;
  void eventCount;
  return false;
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
  const events = useEventStore(useShallow((s) => selectEventsForTask(s, activeTaskId)));
  const latestEvent = useEventStore(selectLatestEvent);
  const eventCount = useEventStore(selectEventCount);
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
  const projectedTimeline = useMemo(() => projectTurnTimeline(timelineTurns, events), [events, timelineTurns]);

  // 是否显示空状态
  const emptySession = isEmptySession(activeTask, eventCount);

  // 当有新事件或新消息时自动滚动到底部
  useEffect(() => {
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

          {projectedTimeline.map((turn) => {
            return (
              <div key={turn.turnId} className="space-y-4">
                <UserMessage content={turn.userText} />

                {turn.entries.map((entry) => {
                  if (entry.kind === "thinking") {
                    // 过滤纯空白与极短无意义内容（至少 2 个字符才值得展示折叠块）
                    const trimmed = entry.content.trim();
                    return trimmed.length >= 2 ? (
                      <ThinkingBlock key={entry.eventId} content={entry.content} />
                    ) : null;
                  }
                  if (entry.kind === "assistant") {
                    return entry.content.length > 0 ? (
                      <AgentMessage key={entry.eventId} content={entry.content} />
                    ) : null;
                  }
                  if (entry.kind === "status") {
                    return <StatusBadge key={entry.eventId} eventType={entry.eventType} payload={entry.payload} />;
                  }
                  const tool = entry.item;
                  return (
                    <div key={tool.eventId}>
                      {tool.status === "running" ? (
                        <ToolCallCard
                          toolName={tool.toolName}
                          status="running"
                          args={tool.arguments}
                          display={tool.display}
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
                          onOpenFile={(path) => {
                            void openFileInEditor(path);
                          }}
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}

          {/* 滚动锚点 */}
          <div ref={scrollEndRef} className="h-1" />
        </div>
      </ScrollArea>
    </main>
  );
}
