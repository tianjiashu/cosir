import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useState } from "react";
import { ReadonlyThread } from "@/components/conversation/thread/readonly-thread.aui";
import { TransportStatus } from "@/components/conversation-runtime/transport-status";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/state-panel";
import { useSessionAssistantTransportRuntime } from "@/lib/assistant/transport/use-session-assistant-transport-runtime";
import type { SessionTransportConnection } from "@/lib/assistant/transport/session-transport-connection";
import type { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { useSessionTransportConnection } from "@/lib/assistant/transport/use-session-transport-connection";
import { newTraceId } from "@/lib/logging/trace-id";
import { TransportHistoryContext } from "@/lib/assistant/transport/transport-history-context";

/** 只读 runtime：连接已收到首帧，store 一定存在；不挂发送入口。 */
function AgentRunTransport({ sessionId, workspaceId, connection, store }: {
  sessionId: number;
  workspaceId: number;
  connection: SessionTransportConnection;
  store: TransportFrameStore;
}) {
  const { runtime, loadEarlierHistory } = useSessionAssistantTransportRuntime(connection, store, { readonly: true });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TransportHistoryContext.Provider value={loadEarlierHistory}>
        <ReadonlyThread sessionId={sessionId} workspaceId={workspaceId} />
      </TransportHistoryContext.Provider>
    </AssistantRuntimeProvider>
  );
}

/**
 * 只读查看子 Session：与主对话共用同一种连接（`SessionTransportConnection`）——首帧即初始状态，断流有限重连，
 * 后端恢复或换代时原地重开订阅，Session 已不存在时说明并可关闭标签。不取消业务 Run。
 *
 * `workspaceId` 是工作台所在工作区：子 Agent Session 沿用父 Session 的工作区，
 * 工作台又按工作区存放标签，所以它就是子 Session 附件（用户图片、工具截图）所在的工作区。
 */
export function WorkbenchAgentRunSurface({ sessionId, workspaceId, onClose }: { sessionId: number; workspaceId: number; onClose: () => void }) {
  const [traceId] = useState(() => newTraceId());
  const { connection, status, issue } = useSessionTransportConnection({ sessionId, traceId, surface: "workbench" });
  if (status.kind === "missing") return (
    <Alert tone="warning" className="m-4" title="子 Agent 会话已不存在" action={(
      <Button type="button" variant="ghost" size="sm" onClick={onClose}>关闭标签页</Button>
    )} />
  );
  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue} onRetry={connection.resync} />
      <div className="min-h-0 flex-1">
        {status.kind === "ready"
          ? <AgentRunTransport sessionId={sessionId} workspaceId={workspaceId} connection={connection} store={status.store} />
          : <LoadingState label="正在同步子 Agent…" className="h-full" />}
      </div>
    </div>
  );
}
