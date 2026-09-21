import { useCallback, useEffect, useRef } from "react";

import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
import {
  currentTransportRun,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import type { TransportState } from "@/lib/assistant/contract";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

export type RuntimeRecovery = {
  reconcileAfterTransportFinish: (target?: { runId: number | null; status: string | null }) => Promise<void>;
  resetTransportRecoveryBudget: () => void;
};

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAYS_MS = [0, 250, 500, 1_000, 2_000] as const;
const RECONCILE_TIMEOUT_MS = 15_000;

function findRun(state: TransportState, runId: number | null) {
  return runId === null ? undefined : state.runs.find((run) => run.runId === runId);
}

/** Own backend changes and bounded Transport snapshot recovery orchestration. */
export function useRuntimeRecovery(
  context: RuntimeSessionContext,
  hasActiveCancellation?: () => boolean,
): RuntimeRecovery {
  const previousBackendRuntimeGenerationRef = useRef(context.backendRuntimeGeneration);
  const previousBackendRuntimeAvailableRef = useRef(context.backendRuntimeAvailable);
  const backendRuntimeAvailableRef = useRef(context.backendRuntimeAvailable);
  const backendRuntimeGenerationRef = useRef(context.backendRuntimeGeneration);
  backendRuntimeAvailableRef.current = context.backendRuntimeAvailable;
  backendRuntimeGenerationRef.current = context.backendRuntimeGeneration;
  const reconnectAttemptRef = useRef(0);
  const reconcileInFlightRef = useRef(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconcileAbortControllerRef = useRef<AbortController | null>(null);
  const recoveryGenerationRef = useRef(0);
  const disposedRef = useRef(false);

  const resetTransportRecoveryBudget = useCallback(() => {
    recoveryGenerationRef.current += 1;
    reconnectAttemptRef.current = 0;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconcileAbortControllerRef.current?.abort();
    reconcileInFlightRef.current = false;
  }, []);

  useEffect(() => {
    // React StrictMode replays effects on the mounted hook instance in
    // development. Restore the live flag in every setup, not only on mount.
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconcileAbortControllerRef.current?.abort();
    };
  }, []);

  const reconcileAfterTransportFinish = useCallback(async (
    target?: { runId: number | null; status: string | null },
  ) => {
    if (hasActiveCancellation?.()) return;
    if (
      disposedRef.current
      || !backendRuntimeAvailableRef.current
      || reconcileInFlightRef.current
      || reconnectTimerRef.current !== null
    ) return;
    if (reconnectAttemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
      context.setIssue({ message: "本机后端连接多次中断，请重试恢复对话。", canResync: true });
      return;
    }

    const attempt = reconnectAttemptRef.current;
    const generation = recoveryGenerationRef.current;
    reconnectAttemptRef.current += 1;
    reconcileInFlightRef.current = true;
    const { controller, signal, clear } = createTimeoutAbort(RECONCILE_TIMEOUT_MS);
    reconcileAbortControllerRef.current = controller;
    try {
      void frontendLog("INFO", "assistant_transport_reconcile_started", "Assistant Transport 流结束后开始有界恢复", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          lastRunId: context.latestStateRef.current.current_run_id,
          lastRunStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
          attempt: attempt + 1,
        },
      });

      const knownRun = target?.runId === null || target?.runId === undefined
        ? currentTransportRun(context.latestStateRef.current)
        : findRun(context.latestStateRef.current, target.runId);
      const knownStatus = target?.status ?? knownRun?.status ?? null;
      if (knownRun && (knownStatus === "pending" || knownStatus === "running")) {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        const scheduledRunId = target?.runId ?? knownRun.runId;
        const scheduledGeneration = recoveryGenerationRef.current;
        const scheduledBackendGeneration = backendRuntimeGenerationRef.current;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          if (
            disposedRef.current
            || !backendRuntimeAvailableRef.current
            || scheduledGeneration !== recoveryGenerationRef.current
            || scheduledBackendGeneration !== backendRuntimeGenerationRef.current
            || context.cancelRequestedRunIdRef.current === scheduledRunId
          ) return;
          const latestRun = findRun(context.latestStateRef.current, scheduledRunId);
          if (
            !latestRun
            || latestRun.runId !== scheduledRunId
            || (latestRun.status !== "pending" && latestRun.status !== "running")
          ) return;
          void context.attachTransportRef.current?.();
        }, delay);
        void frontendLog("INFO", "assistant_transport_reconcile_attach_scheduled", "已安排原子 attach 恢复流", {
          traceId: context.traceId,
          data: { taskId: context.taskId, runId: scheduledRunId, delay, attempt: attempt + 1 },
        });
        return;
      }

      const snapshot = await requestAssistantSnapshot(context.taskId, {
        signal,
        traceId: context.traceId,
      });
      if (
        disposedRef.current
        || !backendRuntimeAvailableRef.current
        || hasActiveCancellation?.()
        || controller.signal.aborted
        || generation !== recoveryGenerationRef.current
      ) return;
      context.latestStateRef.current = snapshot;

      void frontendLog("INFO", "assistant_transport_reconcile_completed", "Assistant Transport 最新快照已读取", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          runId: snapshot.current_run_id,
          runStatus: currentTransportRun(snapshot)?.status ?? null,
          messageCount: transportMessageCount(snapshot),
        },
      });

      if (
        disposedRef.current
        || !backendRuntimeAvailableRef.current
        || hasActiveCancellation?.()
        || generation !== recoveryGenerationRef.current
      ) return;
      const status = currentTransportRun(snapshot)?.status;
      if (status === "pending" || status === "running") {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        const scheduledRunId = snapshot.current_run_id;
        const scheduledGeneration = recoveryGenerationRef.current;
        const scheduledBackendGeneration = backendRuntimeGenerationRef.current;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          if (
            disposedRef.current
            || !backendRuntimeAvailableRef.current
            || scheduledGeneration !== recoveryGenerationRef.current
            || scheduledBackendGeneration !== backendRuntimeGenerationRef.current
            || (scheduledRunId !== null && context.cancelRequestedRunIdRef.current === scheduledRunId)
          ) return;
          const latestRun = currentTransportRun(context.latestStateRef.current);
          if (
            !latestRun
            || latestRun.runId !== scheduledRunId
            || (latestRun.status !== "pending" && latestRun.status !== "running")
          ) return;
          context.runtimeControlsRef.current?.resume();
        }, delay);
        return;
      }

      resetTransportRecoveryBudget();
      context.runtimeControlsRef.current?.importState(snapshot);
      context.setIssue(null);
    } catch (error) {
      if (
        disposedRef.current
        || !backendRuntimeAvailableRef.current
        || hasActiveCancellation?.()
        || controller.signal.aborted
        || generation !== recoveryGenerationRef.current
      ) return;
      context.setIssue({
        message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
        canResync: true,
      });
      if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        const scheduledGeneration = recoveryGenerationRef.current;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          if (
            disposedRef.current
            || !backendRuntimeAvailableRef.current
            || scheduledGeneration !== recoveryGenerationRef.current
            || hasActiveCancellation?.()
          ) return;
          void reconcileAfterTransportFinish();
        }, delay);
      }
    } finally {
      clear();
      if (reconcileAbortControllerRef.current === controller) {
        reconcileAbortControllerRef.current = null;
      }
      if (generation === recoveryGenerationRef.current) reconcileInFlightRef.current = false;
    }
  }, [context, hasActiveCancellation, resetTransportRecoveryBudget]);

  useEffect(() => {
    const previousGeneration = previousBackendRuntimeGenerationRef.current;
    const previousAvailable = previousBackendRuntimeAvailableRef.current;
    previousBackendRuntimeGenerationRef.current = context.backendRuntimeGeneration;
    previousBackendRuntimeAvailableRef.current = context.backendRuntimeAvailable;
    const generationChanged = previousGeneration !== context.backendRuntimeGeneration;
    const runtimeRecovered = !previousAvailable && context.backendRuntimeAvailable;
    if (!generationChanged && !runtimeRecovered) return;

    void frontendLog("INFO", "assistant_backend_runtime_changed", "Assistant 后端实例已变化", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        previousGeneration,
        generation: context.backendRuntimeGeneration,
        runtimeRecovered,
        backendBaseUrl: context.backendBaseUrl,
      },
    });

    // 后端实例变化后，无论前端旧缓存是 active 还是 terminal，都必须以 canonical
    // snapshot 重新对齐。active Run 会继续 attach，cancelled Run 则只恢复按钮资格。
    resetTransportRecoveryBudget();
    context.setIssue({ message: "本机后端已重启，正在同步当前对话…", canResync: true });
    void reconcileAfterTransportFinish();
  }, [context, reconcileAfterTransportFinish, resetTransportRecoveryBudget]);

  return {
    reconcileAfterTransportFinish,
    resetTransportRecoveryBudget,
  };
}
