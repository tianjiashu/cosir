"use client";

import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ReadonlyThread } from "@/components/assistant-ui/elements/readonly-thread.aui";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { AssistantAttachController } from "@/lib/assistant/assistant-attach-controller";
import { useAgentReader } from "@/lib/workbench/use-agent-reader";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";
import { useTaskAssistantReadonlyTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import { isTerminalRunStatus, type TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

function AttachBridge({ runId, backendAvailable, controller }: { runId: number | null; backendAvailable: boolean; controller: AssistantAttachController }) {
  useEffect(() => {
    if (runId === null || !backendAvailable) {
      controller.cancel(runId ?? undefined);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      void frontendLog("INFO", "workbench_agent_attach", "Workbench 子 Agent 开始只读订阅", { data: { runId } });
      controller.request(runId);
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.cancel(runId);
    };
  }, [backendAvailable, controller, runId]);
  return null;
}

function AgentRunTransport({ taskId, initialState, onError, onStateCommit, onFinish }: {
  taskId: number; initialState: TransportState; onError: (error: unknown) => void;
  onStateCommit: (state: TransportState) => void;
  onFinish: (status: { targetRunStatus: string | null }) => void;
}) {
  const backendRuntime = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const [traceId] = useState(() => newTraceId());
  const runId = initialState.current_run_id;
  const currentRun = currentTransportRun(initialState);
  const shouldAttach = currentRun !== undefined && !isTerminalRunStatus(currentRun.status);
  const attachController = useMemo(
    () => new AssistantAttachController((error) => onError(error)),
    [onError],
  );
  const runtime = useTaskAssistantReadonlyTransportRuntime(taskId, {
    initialState,
    readonly: true,
    api: `${backendRuntime.backendBaseUrl}/assistant`,
    resumeApi: `${backendRuntime.backendBaseUrl}/tasks/${taskId}/assistant/attach`,
    headers: async () => ({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Trace-Id": traceId,
    }),
    body: async () => ({ taskId, threadId: `conversation_run-${taskId}`, runId }),
    onError: async (error) => onError(error),
    onAttachReady: attachController.setAttach,
    onStateCommit,
    onFinish,
  });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <AttachBridge
        runId={shouldAttach ? runId : null}
        backendAvailable={backendRuntime.available}
        controller={attachController}
      />
      <AgentReadonlyMessages taskId={taskId} />
    </AssistantRuntimeProvider>
  );
}

function AgentReadonlyMessages({ taskId }: { taskId: number }) {
  return <ReadonlyThread taskId={taskId} />;
}

/** 后端换代或任务切换时释放旧观察会话；不取消业务 Run。 */
export function WorkbenchAgentRunSurface({ taskId, onClose }: { taskId: number; onClose: () => void }) {
  const backend = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  return <AgentReader key={`${taskId}:${backend.generation}`} taskId={taskId} onClose={onClose} />;
}

function AgentReader({ taskId, onClose }: { taskId: number; onClose: () => void }) {
  const reader = useAgentReader(taskId);
  if (reader.missing) return (
    <Alert tone="warning" className="m-4" title="子 Agent 任务已不存在" action={(
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" onClick={reader.retry}>重新同步</Button>
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>关闭标签页</Button>
      </div>
    )} />
  );
  if (!reader.available) return <ErrorState className="m-4" message="本机后端暂不可用，正在等待恢复…" />;
  if (reader.error && !reader.snapshot) return (
    <ErrorState className="m-4" message={reader.error.message}
      onRetry={reader.error.exhausted ? reader.retry : undefined} retryLabel="重新同步" />
  );
  if (!reader.snapshot) return <LoadingState label="正在同步子 Agent…" className="h-full" />;
  return <div className="flex h-full min-h-0 flex-col">
    {reader.statusError && <Alert tone="warning" className="m-4 mb-0" title="运行状态暂未同步" action={
      <Button type="button" variant="outline" size="sm" disabled={reader.refreshingStatus} onClick={reader.refreshStatus}>刷新状态</Button>
    }>下方保留已读取的内容。</Alert>}
    <div className="min-h-0 flex-1">
      <AgentRunTransport taskId={taskId} initialState={reader.snapshot} onError={reader.resync}
        onStateCommit={reader.onStateCommit} onFinish={reader.onFinish} />
    </div>
  </div>;
}
