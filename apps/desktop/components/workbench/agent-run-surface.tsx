import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useState } from "react";
import { ReadonlyThread } from "@/components/conversation/thread/readonly-thread.aui";
import { TransportStatus } from "@/components/conversation-runtime/transport-status";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/state-panel";
import { useTaskAssistantTransportRuntime } from "@/lib/assistant/transport/use-task-assistant-transport-runtime";
import type { TaskTransportConnection } from "@/lib/assistant/transport/task-transport-connection";
import type { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { useTaskTransportConnection } from "@/lib/assistant/transport/use-task-transport-connection";
import { newTraceId } from "@/lib/logging/trace-id";
import { TransportHistoryContext } from "@/lib/assistant/transport/transport-history-context";

/** 只读 runtime：连接已收到首帧，store 一定存在；不挂发送入口。 */
function AgentRunTransport({ taskId, workspaceId, connection, store }: {
  taskId: number;
  workspaceId: number;
  connection: TaskTransportConnection;
  store: TransportFrameStore;
}) {
  const { runtime, loadEarlierHistory } = useTaskAssistantTransportRuntime(connection, store, { readonly: true });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TransportHistoryContext.Provider value={loadEarlierHistory}>
        <ReadonlyThread taskId={taskId} workspaceId={workspaceId} />
      </TransportHistoryContext.Provider>
    </AssistantRuntimeProvider>
  );
}

/**
 * 只读查看子 Task：与主对话共用同一种连接（`TaskTransportConnection`）——首帧即初始状态，断流有限重连，
 * 后端恢复或换代时原地重开订阅，Task 已不存在时说明并可关闭标签。不取消业务 Run。
 *
 * `workspaceId` 是工作台所在工作区：子 Agent 与 Team 节点 Task 都沿用父 Task 的工作区，
 * 工作台又按工作区存放标签，所以它就是子 Task 附件（用户图片、工具截图）所在的工作区。
 */
export function WorkbenchAgentRunSurface({ taskId, workspaceId, onClose }: { taskId: number; workspaceId: number; onClose: () => void }) {
  const [traceId] = useState(() => newTraceId());
  const { connection, status, issue } = useTaskTransportConnection({ taskId, traceId, surface: "workbench" });
  if (status.kind === "missing") return (
    <Alert tone="warning" className="m-4" title="子 Agent 任务已不存在" action={(
      <Button type="button" variant="ghost" size="sm" onClick={onClose}>关闭标签页</Button>
    )} />
  );
  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue} onRetry={connection.resync} />
      <div className="min-h-0 flex-1">
        {status.kind === "ready"
          ? <AgentRunTransport taskId={taskId} workspaceId={workspaceId} connection={connection} store={status.store} />
          : <LoadingState label="正在同步子 Agent…" className="h-full" />}
      </div>
    </div>
  );
}
