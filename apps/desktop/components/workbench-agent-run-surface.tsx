"use client";

import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ReadonlyThread } from "@/components/assistant-ui/elements/readonly-thread.aui";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { AssistantAttachController } from "@/lib/assistant/assistant-attach-controller";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import { useTaskAssistantReadonlyTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import type { TransportState } from "@/lib/assistant/contract";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { getActiveTraceId, newTraceId } from "@/lib/trace";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";
import { userErrorMessage } from "@/lib/errors/user-error-message";

const MAX_AUTOMATIC_RESYNCS = 2;
const RESYNC_DELAYS_MS = [300, 900];
const SNAPSHOT_TIMEOUT_MS = 15_000;

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

function AgentRunTransport({ taskId, initialState, onError }: { taskId: number; initialState: TransportState; onError: (error: unknown) => void }) {
  const backendRuntime = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const [traceId] = useState(() => getActiveTraceId() ?? newTraceId());
  const runId = initialState.current_run_id;
  const currentRun = initialState.runs.find((run) => run.runId === runId);
  const shouldAttach = currentRun?.status === "pending" || currentRun?.status === "running";
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
    void fetchCanonicalSnapshot(taskId, {
      reason: "workbench_child_run",
      timeoutMs: SNAPSHOT_TIMEOUT_MS,
      signal: controller.signal,
    })
      .then((outcome) => {
        // superseded：卸载 / 切换 task，或后端在请求期间变化（本 effect 会随后端快照重新发起）。
        if (outcome.kind === "superseded") return;
        setStale(false);
        setError(null);
        setState(outcome.snapshot);
      })
      .catch((cause: unknown) => {
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
        setError(userErrorMessage(cause, "无法读取子 Agent 状态"));
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
    <Alert
      tone="warning"
      className="m-4"
      title="子 Agent 任务已不存在"
      action={(
        <div className="flex gap-2">
          <Button type="button" variant="outline" size="sm" onClick={retryNow}>重新同步</Button>
          <Button type="button" variant="ghost" size="sm" onClick={onClose}>关闭标签页</Button>
        </div>
      )}
    />
  );
  if (error && !state) return (
    <ErrorState
      className="m-4"
      message={error}
      onRetry={retryCount >= MAX_AUTOMATIC_RESYNCS ? retryNow : undefined}
      retryLabel="重新同步"
    />
  );
  if (!state) return <LoadingState label="正在同步子 Agent…" className="h-full" />;
  return <AgentRunTransport taskId={taskId} initialState={state} onError={scheduleResync} />;
}
