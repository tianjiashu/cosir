"use client";

import { AssistantRuntimeProvider, useAui, useAuiState, useAssistantTransportRuntime } from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ReadonlyThread } from "@/components/assistant-ui/elements/readonly-thread.aui";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
import { createTransportViewConverter } from "@/lib/assistant/transport-view-converter";
import type { TransportState } from "@/lib/assistant/contract";
import { HttpError } from "@/lib/http/errors";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import { getActiveTraceId, newTraceId } from "@/lib/trace";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

const MAX_AUTOMATIC_RESYNCS = 2;
const RESYNC_DELAYS_MS = [300, 900];

function AttachBridge({ runId, backendAvailable, backendGeneration }: { runId: number | null; backendAvailable: boolean; backendGeneration: number }) {
  const aui = useAui();
  const backendAvailableRef = useRef(backendAvailable);
  const backendGenerationRef = useRef(backendGeneration);
  backendAvailableRef.current = backendAvailable;
  backendGenerationRef.current = backendGeneration;
  useEffect(() => {
    if (runId === null || !backendAvailable) return;
    let cancelled = false;
    const scheduledBackendGeneration = backendGeneration;
    const timer = window.setTimeout(() => {
      if (
        cancelled
        || !backendAvailableRef.current
        || backendGenerationRef.current !== scheduledBackendGeneration
      ) return;
      void frontendLog("INFO", "workbench_agent_attach", "Workbench 子 Agent 开始只读订阅", { data: { runId } });
      aui.thread.resumeRun({ parentId: null });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [aui, backendAvailable, backendGeneration, runId]);
  return null;
}

function AgentRunTransport({ taskId, initialState, onError }: { taskId: number; initialState: TransportState; onError: (error: unknown) => void }) {
  const backendRuntime = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const [traceId] = useState(() => getActiveTraceId() ?? newTraceId());
  const converter = useMemo(() => createTransportViewConverter(), []);
  const runId = initialState.current_run_id;
  const currentRun = initialState.runs.find((run) => run.runId === runId);
  const shouldAttach = currentRun?.status === "pending" || currentRun?.status === "running";
  const runtime = useAssistantTransportRuntime<TransportState>({
    initialState,
    protocol: "assistant-transport",
    capabilities: {},
    api: `${backendRuntime.backendBaseUrl}/assistant`,
    resumeApi: `${backendRuntime.backendBaseUrl}/tasks/${taskId}/assistant/attach`,
    headers: async () => ({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Trace-Id": traceId,
    }),
    body: async () => ({ taskId, threadId: `task-${taskId}`, runId }),
    converter,
    onError: async (error) => onError(error),
  });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <AttachBridge
        runId={shouldAttach ? runId : null}
        backendAvailable={backendRuntime.available}
        backendGeneration={backendRuntime.generation}
      />
      <AgentReadonlyMessages />
    </AssistantRuntimeProvider>
  );
}

function AgentReadonlyMessages() {
  const messages = useAuiState((state) => state.thread.messages);
  return <ReadonlyThread messages={messages} />;
}

/** Load a canonical child snapshot, then mount one active readonly transport. */
export function WorkbenchAgentRunSurface({ taskId, onClose }: { taskId: number; onClose: () => void }) {
  const backendRuntime = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const [state, setState] = useState<TransportState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [syncToken, setSyncToken] = useState(0);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<number | null>(null);
  const taskIdRef = useRef(taskId);

  const scheduleResync = useCallback(() => {
    setState(null);
    setError("子 Agent 连接中断，正在重新同步…");
    const current = retryCountRef.current;
    if (current >= MAX_AUTOMATIC_RESYNCS || retryTimerRef.current !== null) return;
    retryCountRef.current = current + 1;
    setRetryCount(current + 1);
    void frontendLog("WARNING", "workbench_agent_resync_scheduled", "Workbench 子 Agent 计划有限重同步", {
      data: { taskId, attempt: current + 1 },
    });
    const delay = RESYNC_DELAYS_MS[current] ?? RESYNC_DELAYS_MS.at(-1) ?? 900;
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      setSyncToken((token) => token + 1);
    }, delay);
  }, [taskId]);

  useEffect(() => {
    const controller = new AbortController();
    const taskChanged = taskIdRef.current !== taskId;
    if (taskChanged) {
      taskIdRef.current = taskId;
      if (retryTimerRef.current !== null) {
        window.clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      retryCountRef.current = 0;
      setRetryCount(0);
    }
    setState(null);
    setError(null);
    if (!backendRuntime.available) {
      setError("本机后端暂不可用，正在等待恢复…");
      return;
    }
    const requestBackendGeneration = backendRuntime.generation;
    const isCurrentBackend = () => {
      const latestBackendRuntime = getBackendRuntimeSnapshot();
      return latestBackendRuntime.available && latestBackendRuntime.generation === requestBackendGeneration;
    };
    void requestAssistantSnapshot(taskId, { signal: controller.signal })
      .then((snapshot) => {
        if (controller.signal.aborted || !isCurrentBackend()) return;
        setStale(false);
        setError(null);
        setState(snapshot);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || !isCurrentBackend()) return;
        if (cause instanceof HttpError && cause.status === 404) {
          void frontendLog("WARNING", "workbench_agent_stale", "Workbench 子 Agent 进入 stale 状态", {
            data: { taskId, status: cause.status },
          });
          setStale(true);
          setState(null);
          setError("子 Agent 任务已不存在");
          return;
        }
        if (retryCount < MAX_AUTOMATIC_RESYNCS) {
          scheduleResync();
          return;
        }
        setError(safeFrontendErrorMessage(cause, "无法读取子 Agent 状态"));
      });
    return () => controller.abort();
  }, [backendRuntime.available, backendRuntime.generation, scheduleResync, syncToken, taskId]);

  useEffect(() => () => {
    if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current);
  }, []);

  const retryNow = () => {
    void frontendLog("INFO", "workbench_agent_resync_manual", "用户请求重新同步 Workbench 子 Agent", {
      data: { taskId },
    });
    setRetryCount(0);
    setStale(false);
    setError(null);
    setSyncToken((token) => token + 1);
  };

  if (stale) return (
    <div className="m-4 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
      <p className="text-foreground">子 Agent 任务已不存在</p>
      <div className="mt-3 flex gap-2">
        <button type="button" className="text-muted-foreground underline" onClick={retryNow}>重新同步</button>
        <button type="button" className="text-muted-foreground underline" onClick={onClose}>关闭标签页</button>
      </div>
    </div>
  );
  if (error && !state) return (
    <div className="text-destructive m-4 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm">
      <p>{error}</p>
      {retryCount >= MAX_AUTOMATIC_RESYNCS && <button type="button" className="mt-3 underline" onClick={retryNow}>重新同步</button>}
    </div>
  );
  if (!state) return <div className="text-muted-foreground flex h-full items-center justify-center text-sm">正在同步子 Agent…</div>;
  return <AgentRunTransport taskId={taskId} initialState={state} onError={scheduleResync} />;
}
