/**
 * 中央主会话区（ChatPanel）。
 *
 * 展示：
 * - 用户消息气泡（从 taskStore 读取活跃任务的 input_text）
 * - Agent 输出流（从 eventStore 的 model_output_delta 事件累积渲染）
 * - 代码块、文件链接
 * - 可折叠工具调用卡片
 * - 运行状态标签
 * - 审批请求只读渲染
 *
 * @module components/layout/ChatPanel
 */

import { useEffect, useMemo, useRef } from "react";
import { useEventStore, selectLatestEvent, selectEventCount } from "@/stores/eventStore";
import { useTaskStore, selectActiveTask } from "@/stores/taskStore";
import { ScrollArea } from "@/components/ui/scroll-area";
import { UserMessage } from "@/components/chat/UserMessage";
import { AgentMessage } from "@/components/chat/AgentMessage";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { StatusBadge } from "@/components/chat/StatusBadge";

/**
 * 从事件流中累积模型文本输出。
 *
 * 遍历所有 model_output_delta 事件，按顺序拼接 delta 片段，
 * 构建完整的 Agent 文本回复。无 delta 事件时返回空字符串。
 *
 * @param events - 当前任务的事件流数组。
 * @returns 累积后的完整文本输出。
 */
function accumulateModelOutput(events: Array<{ event_type: string; payload: Record<string, unknown> }>): string {
  return events
    .filter((e) => e.event_type === "model_output_delta")
    .map((e) => String(e.payload.delta ?? ""))
    .join("");
}

/**
 * 判断当前是否处于"空会话"状态：无活跃任务或零事件且任务从未运行过。
 */
function isEmptySession(activeTask: ReturnType<typeof selectActiveTask>, eventCount: number): boolean {
  if (!activeTask) return true;
  // pending 状态 + 无事件 = 任务刚创建、SSE 尚未开始推送
  return activeTask.status === "pending" && eventCount === 0;
}

/**
 * ChatPanel 中央主会话区组件。
 *
 * 消息列表区域使用 ScrollArea 包裹，
 * 新消息自动滚动到底部（通过 scrollIntoView 实现）。
 *
 * 数据来源：
 * - 用户消息：taskStore.activeTask.input_text
 * - Agent 输出：eventStore.events 中 model_output_delta 事件的累积
 * - 工具调用 / 状态：eventStore.events 中对应类型事件
 */
export function ChatPanel() {
  const events = useEventStore((s) => s.events);
  const latestEvent = useEventStore(selectLatestEvent);
  const eventCount = useEventStore(selectEventCount);
  const activeTask = useTaskStore(selectActiveTask);
  const scrollEndRef = useRef<HTMLDivElement>(null);

  // 累积 Agent 文本输出（仅当有事件时计算）
  const agentOutputText = useMemo(() => accumulateModelOutput(events), [events]);
  const hasAgentOutput = agentOutputText.length > 0;

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

          {/* 用户消息（有活跃任务时展示） */}
          {!emptySession && activeTask && (
            <UserMessage content={activeTask.input_text} />
          )}

          {/* Agent 输出（有增量输出时展示） */}
          {hasAgentOutput && (
            <AgentMessage key="agent-output" content={agentOutputText} />
          )}

          {/* 从事件流渲染工具调用和状态信息 */}
          {events
            .filter((e) =>
              [
                "tool_call_requested",
                "tool_call_started",
                "tool_call_finished",
                "tool_approval_required",
                "observation_added",
              ].includes(e.event_type),
            )
            .map((event) => (
              <div key={event.event_id}>
                {event.event_type === "tool_approval_required" ? (
                  <ToolCallCard
                    toolName={String(event.payload.tool_name ?? "未知工具")}
                    permission={String(event.payload.permission ?? "未知权限")}
                    reason={String(event.payload.reason ?? "")}
                    status="approval-required"
                  />
                ) : event.event_type === "tool_call_requested" ||
                  event.event_type === "tool_call_started" ? (
                  <ToolCallCard
                    toolName={String(event.payload.tool_name ?? "未知工具")}
                    status="running"
                  />
                ) : event.event_type === "tool_call_finished" ? (
                  <ToolCallCard
                    toolName={String(event.payload.tool_name ?? "未知工具")}
                    status={String(event.payload.status) === "error" ? "error" : "completed"}
                    error={event.payload.error as string | undefined}
                  />
                ) : null}
              </div>
            ))}

          {/* 终态运行状态展示 */}
          {(latestEvent?.event_type === "run_finished" ||
            latestEvent?.event_type === "run_failed" ||
            latestEvent?.event_type === "run_cancelled") && (
            <StatusBadge eventType={latestEvent.event_type} payload={latestEvent.payload} />
          )}

          {/* 滚动锚点 */}
          <div ref={scrollEndRef} className="h-1" />
        </div>
      </ScrollArea>
    </main>
  );
}
