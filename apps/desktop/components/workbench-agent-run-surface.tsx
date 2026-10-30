"use client";

import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ReadonlyThread } from "@/components/assistant-ui/elements/readonly-thread.aui";
import { useSubscriptionRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import { TransportStatus, type TransportIssue } from "@/components/assistant/transport-status";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { useAgentReader } from "@/lib/workbench/use-agent-reader";
import { useTaskAssistantReadonlyTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";
import { TransportHistoryContext } from "@/lib/assistant/transport-history-context";

/**
 * 只读查看子 Task：runtime 挂载即打开一条 Task 长订阅，卸载时关闭；不按 runId 去重或重订。
 * 订阅意外结束交给与主对话共用的 `SubscriptionRecovery`：只重开订阅（首帧即完整状态），
 * 已显示的内容与 runtime 都保留；重连耗尽后由用户「重新同步」。
 */
function AgentRunTransport({ taskId, initialState, onDisconnect, onStateCommit }: {
  taskId: number; initialState: TransportState;
  onDisconnect: () => void;
  onStateCommit: (state: TransportState) => void;
}) {
  const backendRuntime = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const [traceId] = useState(() => newTraceId());
  const [issue, setIssue] = useState<TransportIssue | null>(null);
  const openRef = useRef<(() => Promise<void>) | null>(null);
  const recovery = useSubscriptionRecovery(useMemo(() => ({
    taskId,
    traceId,
    attach: () => openRef.current,
    backend: getBackendRuntimeSnapshot,
    setIssue,
  }), [taskId, traceId]));
  const { runtime, open, loadEarlierHistory } = useTaskAssistantReadonlyTransportRuntime(taskId, {
    initialState,
    readonly: true,
    // 阅读页按后端 generation 整体重挂（见 WorkbenchAgentRunSurface），这里只需随可用性开关。
    connectionKey: backendRuntime.available ? String(backendRuntime.generation) : null,
    traceId,
    onResponse: () => {
      void frontendLog("INFO", "workbench_agent_subscription_opened", "Workbench 子 Agent 只读订阅已建立", { traceId, data: { taskId } });
      recovery.markConnected();
      setIssue(null);
    },
    onDisconnect: ({ error, resync }) => {
      void frontendLog("WARNING", "workbench_agent_subscription_closed", "Workbench 子 Agent 只读订阅意外结束", {
        traceId, data: { taskId, resync }, error: error ?? undefined,
      });
      recovery.reconnect(resync ? "resync_required" : "disconnected");
      onDisconnect();
    },
    onStateCommit,
  });
  useEffect(() => {
    openRef.current = open;
    return () => { openRef.current = null; };
  }, [open]);
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TransportHistoryContext.Provider value={loadEarlierHistory}>
        <div className="flex h-full min-h-0 flex-col">
          <TransportStatus issue={issue} onRetry={() => recovery.resync(null)} />
          <div className="min-h-0 flex-1">
            <AgentReadonlyMessages taskId={taskId} />
          </div>
        </div>
      </TransportHistoryContext.Provider>
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
    <ErrorState className="m-4" message={reader.error.message} onRetry={reader.retry} retryLabel="重新同步" />
  );
  if (!reader.snapshot) return <LoadingState label="正在同步子 Agent…" className="h-full" />;
  return <div className="flex h-full min-h-0 flex-col">
    {reader.statusError && <Alert tone="warning" className="m-4 mb-0" title="运行状态暂未同步" action={
      <Button type="button" variant="outline" size="sm" disabled={reader.refreshingStatus} onClick={reader.refreshStatus}>刷新状态</Button>
    }>下方保留已读取的内容。</Alert>}
    <div className="min-h-0 flex-1">
      {/* 订阅断开时顺带刷新状态摘要：任务被删除时由 404 显示「已不存在」。 */}
      <AgentRunTransport taskId={taskId} initialState={reader.snapshot} onDisconnect={reader.refreshStatus}
        onStateCommit={reader.onStateCommit} />
    </div>
  </div>;
}
