import { useCallback, useEffect, useRef } from "react";

import { requestJson, requestRaw } from "@/lib/http/client";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import {
  currentTransportRun,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

type RuntimeRecovery = {
  reconcileAfterTransportFinish: () => Promise<void>;
  confirmCancellation: (runId: number, onConfirmationEnded?: () => void) => void;
  cancellationSettled: (runId: number) => void;
  resumeBusinessRun: () => Promise<void>;
  resetRecovery: () => void;
};

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAYS_MS = [0, 250, 500, 1_000, 2_000] as const;
const MAX_CANCELLATION_CHECKS = 7;
const CANCELLATION_CHECK_DELAYS_MS = [0, 250, 500, 1_000, 2_000, 4_000, 8_000] as const;
const CANCELLATION_CONFIRMATION_TIMEOUT_MS = 30_000;
const RECONCILE_TIMEOUT_MS = 15_000;
const BUSINESS_RESUME_TIMEOUT_MS = 15_000;

/** Own backend changes, bounded transport recovery, cancellation confirmation, and business resume. */
export function useRuntimeRecovery(
  context: RuntimeSessionContext,
): RuntimeRecovery {
  const previousBackendRuntimeGenerationRef = useRef(context.backendRuntimeGeneration);
  const reconnectAttemptRef = useRef(0);
  const reconcileInFlightRef = useRef(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconcileAbortControllerRef = useRef<AbortController | null>(null);
  const recoveryGenerationRef = useRef(0);
  const disposedRef = useRef(false);
  const cancellationCheckGenerationRef = useRef(0);
  const cancellationCheckAttemptRef = useRef(0);
  const cancellationCheckRunIdRef = useRef<number | null>(null);
  const cancellationCheckTimerRef = useRef<number | null>(null);
  const cancellationCheckDeadlineRef = useRef<number | null>(null);
  const cancellationCheckAbortControllerRef = useRef<AbortController | null>(null);
  const cancellationCheckInFlightRef = useRef(false);
  const cancellationCheckOnEndedRef = useRef<(() => void) | undefined>(undefined);

  const resetRecovery = useCallback(() => {
    recoveryGenerationRef.current += 1;
    reconnectAttemptRef.current = 0;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconcileAbortControllerRef.current?.abort();
    reconcileInFlightRef.current = false;
  }, []);

  const stopCancellationConfirmation = useCallback((runId?: number) => {
    if (runId !== undefined && cancellationCheckRunIdRef.current !== runId) return;
    cancellationCheckGenerationRef.current += 1;
    cancellationCheckRunIdRef.current = null;
    cancellationCheckAttemptRef.current = 0;
    cancellationCheckInFlightRef.current = false;
    cancellationCheckOnEndedRef.current = undefined;
    if (cancellationCheckTimerRef.current !== null) {
      window.clearTimeout(cancellationCheckTimerRef.current);
      cancellationCheckTimerRef.current = null;
    }
    if (cancellationCheckDeadlineRef.current !== null) {
      window.clearTimeout(cancellationCheckDeadlineRef.current);
      cancellationCheckDeadlineRef.current = null;
    }
    cancellationCheckAbortControllerRef.current?.abort();
    cancellationCheckAbortControllerRef.current = null;
  }, []);

  const finishCancellationUnconfirmed = useCallback((runId: number) => {
    if (cancellationCheckRunIdRef.current !== runId) return;
    cancellationCheckGenerationRef.current += 1;
    cancellationCheckRunIdRef.current = null;
    cancellationCheckInFlightRef.current = false;
    if (cancellationCheckTimerRef.current !== null) {
      window.clearTimeout(cancellationCheckTimerRef.current);
      cancellationCheckTimerRef.current = null;
    }
    if (cancellationCheckDeadlineRef.current !== null) {
      window.clearTimeout(cancellationCheckDeadlineRef.current);
      cancellationCheckDeadlineRef.current = null;
    }
    cancellationCheckAbortControllerRef.current?.abort();
    cancellationCheckAbortControllerRef.current = null;
    context.cancelRequestedRunIdRef.current = null;
    const onConfirmationEnded = cancellationCheckOnEndedRef.current;
    cancellationCheckOnEndedRef.current = undefined;
    onConfirmationEnded?.();
    context.setIssue({
      message: "已请求停止，但暂时无法确认运行状态。请重新同步后再操作。",
      retryable: true,
    });
  }, [context]);

  useEffect(() => {
    // React StrictMode replays effects on the mounted hook instance in
    // development. Restore the live flag in every setup, not only on mount.
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconcileAbortControllerRef.current?.abort();
      stopCancellationConfirmation();
    };
  }, [stopCancellationConfirmation]);

  const reconcileAfterTransportFinish = useCallback(async () => {
    if (cancellationCheckRunIdRef.current !== null) return;
    if (disposedRef.current || reconcileInFlightRef.current || reconnectTimerRef.current !== null) return;
    if (reconnectAttemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
      context.setIssue({ message: "本机后端连接多次中断，请重试恢复对话。", retryable: true });
      return;
    }

    const attempt = reconnectAttemptRef.current;
    const generation = recoveryGenerationRef.current;
    reconnectAttemptRef.current += 1;
    reconcileInFlightRef.current = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), RECONCILE_TIMEOUT_MS);
    reconcileAbortControllerRef.current = controller;
    try {
      void frontendLog("INFO", "assistant_transport_reconcile_started", "Assistant Transport 流结束后读取最新快照", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          lastRunId: context.latestStateRef.current.current_run_id,
          lastRunStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
          attempt: attempt + 1,
        },
      });

      const snapshot = parseTransportState(await requestJson<unknown>(`/tasks/${context.taskId}/assistant/state`, {
        signal: controller.signal,
        traceId: context.traceId,
      }));
      if (disposedRef.current || controller.signal.aborted || generation !== recoveryGenerationRef.current) return;
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

      if (disposedRef.current || generation !== recoveryGenerationRef.current) return;
      const status = currentTransportRun(snapshot)?.status;
      if (status === "pending" || status === "running") {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          if (disposedRef.current) return;
          context.runtimeControlsRef.current?.resume();
        }, delay);
        return;
      }

      resetRecovery();
      context.runtimeControlsRef.current?.importState(snapshot);
      context.setIssue(null);
    } catch (error) {
      if (disposedRef.current || controller.signal.aborted || generation !== recoveryGenerationRef.current) return;
      context.setIssue({
        message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
        retryable: true,
      });
      if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
        const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)] ?? 2_000;
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          void reconcileAfterTransportFinish();
        }, delay);
      }
    } finally {
      window.clearTimeout(timeout);
      if (reconcileAbortControllerRef.current === controller) {
        reconcileAbortControllerRef.current = null;
      }
      if (generation === recoveryGenerationRef.current) reconcileInFlightRef.current = false;
    }
  }, [context, resetRecovery]);

  const reconcileCancellation = useCallback(async function reconcileCancellation(
    runId: number,
    generation: number,
  ): Promise<void> {
    if (
      disposedRef.current
      || generation !== cancellationCheckGenerationRef.current
      || cancellationCheckRunIdRef.current !== runId
      || cancellationCheckInFlightRef.current
    ) return;

    if (cancellationCheckAttemptRef.current >= MAX_CANCELLATION_CHECKS) {
      finishCancellationUnconfirmed(runId);
      return;
    }

    const attempt = cancellationCheckAttemptRef.current;
    cancellationCheckAttemptRef.current += 1;
    cancellationCheckInFlightRef.current = true;
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, RECONCILE_TIMEOUT_MS);
    cancellationCheckAbortControllerRef.current = controller;

    const scheduleNextCheck = () => {
      if (cancellationCheckAttemptRef.current >= MAX_CANCELLATION_CHECKS) {
        finishCancellationUnconfirmed(runId);
        return;
      }
      const delay = CANCELLATION_CHECK_DELAYS_MS[
        Math.min(attempt + 1, CANCELLATION_CHECK_DELAYS_MS.length - 1)
      ] ?? 8_000;
      cancellationCheckTimerRef.current = window.setTimeout(() => {
        cancellationCheckTimerRef.current = null;
        void reconcileCancellation(runId, generation);
      }, delay);
    };

    try {
      void frontendLog("INFO", "assistant_cancel_reconcile_started", "停止请求已接受，读取权威运行状态", {
        traceId: context.traceId,
        data: { taskId: context.taskId, runId, attempt: attempt + 1 },
      });
      const snapshot = parseTransportState(await requestJson<unknown>(
        `/tasks/${context.taskId}/assistant/state`,
        { signal: controller.signal, traceId: context.traceId },
      ));
      if (
        disposedRef.current
        || generation !== cancellationCheckGenerationRef.current
        || controller.signal.aborted
        || cancellationCheckRunIdRef.current !== runId
      ) return;

      const requestedRun = snapshot.runs.find((run) => run.runId === runId);
      const status = requestedRun?.status;
      context.latestStateRef.current = snapshot;
      void frontendLog("INFO", "assistant_cancel_reconcile_snapshot", "已读取停止请求对应的权威运行状态", {
        traceId: context.traceId,
        data: { taskId: context.taskId, runId, runStatus: status ?? null, attempt: attempt + 1 },
      });

      if (status === "pending" || status === "running") {
        scheduleNextCheck();
        return;
      }

      context.cancelRequestedRunIdRef.current = null;
      context.runtimeControlsRef.current?.importState(snapshot);
      context.setIssue(null);
      context.onTaskStateChanged?.();
      const onConfirmationEnded = cancellationCheckOnEndedRef.current;
      cancellationCheckOnEndedRef.current = undefined;
      stopCancellationConfirmation(runId);
      onConfirmationEnded?.();

      // A newer Run can become current after this cancellation was accepted.
      // The old Run is settled, but the imported snapshot may still need an
      // attach so its active stream is not silently left without a subscriber.
      const currentRun = currentTransportRun(snapshot);
      if (
        currentRun
        && currentRun.runId !== runId
        && (currentRun.status === "pending" || currentRun.status === "running")
      ) {
        const settledGeneration = cancellationCheckGenerationRef.current;
        window.setTimeout(() => {
          if (
            disposedRef.current
            || settledGeneration !== cancellationCheckGenerationRef.current
          ) return;
          const latestRun = currentTransportRun(context.latestStateRef.current);
          if (
            latestRun?.runId === currentRun.runId
            && (latestRun.status === "pending" || latestRun.status === "running")
          ) context.runtimeControlsRef.current?.resume();
        }, 0);
      }
    } catch (error) {
      if (
        disposedRef.current
        || generation !== cancellationCheckGenerationRef.current
        || cancellationCheckRunIdRef.current !== runId
      ) return;
      if (!timedOut && controller.signal.aborted) return;
      if (cancellationCheckAttemptRef.current < MAX_CANCELLATION_CHECKS) {
        scheduleNextCheck();
      } else {
        finishCancellationUnconfirmed(runId);
        void frontendLog("WARNING", "assistant_cancel_reconcile_exhausted", "停止请求状态确认达到重试上限", {
          traceId: context.traceId,
          data: { taskId: context.taskId, runId, attempt: cancellationCheckAttemptRef.current },
          error,
        });
      }
    } finally {
      window.clearTimeout(timeout);
      if (cancellationCheckAbortControllerRef.current === controller) {
        cancellationCheckAbortControllerRef.current = null;
      }
      if (generation === cancellationCheckGenerationRef.current) {
        cancellationCheckInFlightRef.current = false;
      }
    }
  }, [context, finishCancellationUnconfirmed, stopCancellationConfirmation]);

  const confirmCancellation = useCallback((runId: number, onConfirmationEnded?: () => void) => {
    stopCancellationConfirmation();
    if (disposedRef.current) return;
    cancellationCheckRunIdRef.current = runId;
    cancellationCheckOnEndedRef.current = onConfirmationEnded;
    const generation = cancellationCheckGenerationRef.current;
    cancellationCheckDeadlineRef.current = window.setTimeout(() => {
      cancellationCheckDeadlineRef.current = null;
      finishCancellationUnconfirmed(runId);
      void frontendLog("WARNING", "assistant_cancel_reconcile_deadline_reached", "停止请求状态确认超过总时限", {
        traceId: context.traceId,
        data: { taskId: context.taskId, runId, timeoutMs: CANCELLATION_CONFIRMATION_TIMEOUT_MS },
      });
    }, CANCELLATION_CONFIRMATION_TIMEOUT_MS);
    void reconcileCancellation(runId, generation);
  }, [context, finishCancellationUnconfirmed, reconcileCancellation, stopCancellationConfirmation]);

  const cancellationSettled = useCallback((runId: number) => {
    if (cancellationCheckRunIdRef.current !== runId) return;
    stopCancellationConfirmation(runId);
  }, [stopCancellationConfirmation]);

  useEffect(() => {
    const previousGeneration = previousBackendRuntimeGenerationRef.current;
    previousBackendRuntimeGenerationRef.current = context.backendRuntimeGeneration;
    if (previousGeneration === context.backendRuntimeGeneration) return;

    void frontendLog("INFO", "assistant_backend_runtime_changed", "Assistant 后端实例已变化", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        previousGeneration,
        generation: context.backendRuntimeGeneration,
        backendBaseUrl: context.backendBaseUrl,
      },
    });

    // 后端实例变化后，无论前端旧缓存是 active 还是 terminal，都必须以 canonical
    // snapshot 重新对齐。active Run 会继续 attach，cancelled Run 则只恢复按钮资格。
    resetRecovery();
    context.setIssue({ message: "本机后端已重启，正在同步当前对话…", retryable: true });
    void reconcileAfterTransportFinish();
  }, [context, reconcileAfterTransportFinish, resetRecovery]);

  const resumeBusinessRun = useCallback(async () => {
    const runId = context.latestStateRef.current.current_run_id;
    if (runId == null) throw new Error("当前没有可恢复的运行");

    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), BUSINESS_RESUME_TIMEOUT_MS);
    try {
      const response = await requestRaw("/assistant", {
        method: "POST",
        signal: controller.signal,
        traceId: context.traceId,
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          commands: [],
          taskId: context.taskId,
          threadId: `task-${context.taskId}`,
          runId,
          ...(context.workspaceId != null ? { workspaceId: context.workspaceId } : {}),
        }),
      });
      if (!response.ok) throw new Error(`继续运行失败（HTTP ${response.status}）`);
      await response.body?.cancel();
    } finally {
      window.clearTimeout(timeout);
    }
    resetRecovery();
    context.runtimeControlsRef.current?.resume();
  }, [context, resetRecovery]);

  return {
    reconcileAfterTransportFinish,
    confirmCancellation,
    cancellationSettled,
    resumeBusinessRun,
    resetRecovery,
  };
}

export type { RuntimeRecovery };
