import { useMemo, useState } from "react";

import { AssistantRuntime } from "@/components/conversation-runtime/assistant-runtime";
import { ThreadSkeleton } from "@/components/conversation-runtime/thread-skeleton";
import { TransportStatus } from "@/components/conversation-runtime/transport-status";
import { ErrorState } from "@/components/ui/state-panel";
import { createAssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { TransportState } from "@/lib/assistant/contract";
import type { ToolCalls } from "@/lib/assistant/tools/tool-call-index";
import { useSessionTransportConnection } from "@/lib/assistant/transport/use-session-transport-connection";
import { useBackgroundTasksStore } from "@/lib/background-tasks/background-tasks-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/logging/trace-id";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";

/**
 * 对话页外壳：持有这个 Session 的连接，按连接的生命周期渲染——首帧到达前是骨架，Session 已不存在时说明，
 * 就绪后挂载 runtime。连接问题（断流、重连耗尽、后端恢复中）在顶部横幅呈现，首帧前后同一来源。
 *
 * 首屏不另读快照：连接打开订阅，首帧就是完整状态（见 `SessionTransportConnection`）。runtime 渲染出错只重挂
 * runtime 子树，连接与 store 不受影响。`SessionPage` 按 sessionId 重挂，所以一个外壳只服务一个 Session。
 */
export const Assistant = ({
  sessionId,
  workspaceId,
  workspaceRoot,
  forkingMessageId,
  onForkMessage,
}: {
  sessionId: number;
  workspaceId: number;
  workspaceRoot?: string;
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
}) => {
  // AppLayout may finish its session-list refresh later; freeze the workspace identity for this session
  // so a late parent refresh cannot replace the Assistant UI transport while a run is active.
  const [sessionWorkspaceId] = useState(workspaceId);
  const [traceId] = useState(() => newTraceId());
  const performanceProbe = useMemo(() => {
    if (!import.meta.env.DEV) return null;
    return createAssistantPerformanceProbe({
      onReport: (report) => {
        void frontendLog("INFO", "assistant_render_performance_report", "Assistant 开发期渲染性能报告已采集", {
          traceId,
          data: { sessionId, ...report },
        });
      },
    });
  }, [sessionId, traceId]);
  const { connection, status, issue } = useSessionTransportConnection({
    sessionId,
    traceId,
    surface: "conversation",
    performanceProbe,
    onStateCommit: (state: TransportState, toolCalls: ToolCalls) => {
      // 终端会话、后台工作与交接是 snapshot 投影；镜像给 runtime 之外的页头按钮、终端工作台与浏览器面板。
      useTerminalWorkbenchStore.getState().syncSessions(sessionId, state.terminal_sessions);
      useBackgroundTasksStore.getState().sync(sessionId, { ...state, toolCalls });
      useHandoffStore.getState().sync(sessionId, state.handoffs);
    },
  });

  if (status.kind === "missing") {
    return (
      <div className="flex h-full min-h-0 items-center justify-center p-6">
        <ErrorState className="w-full max-w-md" message="这个会话已不存在" />
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue} onRetry={connection.resync} />
      <div className="min-h-0 flex-1">
        {status.kind === "ready"
          ? (
            <AssistantRuntime
              sessionId={sessionId}
              workspaceId={sessionWorkspaceId}
              workspaceRoot={workspaceRoot}
              connection={connection}
              store={status.store}
              traceId={traceId}
              performanceProbe={performanceProbe}
              forkingMessageId={forkingMessageId}
              onForkMessage={onForkMessage}
            />
          )
          : <ThreadSkeleton />}
      </div>
    </div>
  );
};
