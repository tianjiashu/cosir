import { useCallback, useEffect, useRef } from "react";

import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

const MAX_CANCELLATION_CHECKS = 7;
const CANCELLATION_CHECK_DELAYS_MS = [0, 250, 500, 1_000, 2_000, 4_000, 8_000] as const;
const CANCELLATION_CONFIRMATION_TIMEOUT_MS = 30_000;
const RECONCILE_TIMEOUT_MS = 15_000;

export type CancellationConfirmation = {
  confirmCancellation: (runId: number, onConfirmationEnded?: () => void) => void;
  cancellationSettled: (runId: number) => void;
  hasActiveConfirmation: () => boolean;
  /** 后端不可用：暂停确认轮询，保留待确认的 runId 与总时限。 */
  suspendForBackend: () => void;
  /** 后端恢复：对仍待确认的取消重新读取权威状态。 */
  resumeAfterBackendRestored: () => void;
};

/**
 * Confirm cancellation against the canonical snapshot without creating a
 * second Run state machine. The hook owns only cancellation polling, its
 * bounded timers, and the attach-only handoff for a newer active Run.
 */
export function useCancellationConfirmation(
  context: RuntimeSessionContext,
): CancellationConfirmation {
  const backendRef = context.backendRef;
  const cancellationGenerationRef = useRef(0);
  const cancellationAttemptRef = useRef(0);
  const cancellationRunIdRef = useRef<number | null>(null);
  const cancellationTimerRef = useRef<number | null>(null);
  const cancellationDeadlineRef = useRef<number | null>(null);
  const cancellationDeadlineAtRef = useRef<number | null>(null);
  const cancellationAbortControllerRef = useRef<AbortController | null>(null);
  const cancellationInFlightRef = useRef(false);
  const cancellationOnEndedRef = useRef<(() => void) | undefined>(undefined);
  const newerRunAttachTimerRef = useRef<number | null>(null);
  const disposedRef = useRef(false);

  const clearNewerRunAttachTimer = useCallback(() => {
    if (newerRunAttachTimerRef.current === null) return;
    window.clearTimeout(newerRunAttachTimerRef.current);
    newerRunAttachTimerRef.current = null;
  }, []);

  const stopCancellationConfirmation = useCallback((runId?: number) => {
    if (runId !== undefined && cancellationRunIdRef.current !== runId) return;
    cancellationGenerationRef.current += 1;
    cancellationRunIdRef.current = null;
    cancellationAttemptRef.current = 0;
    cancellationInFlightRef.current = false;
    cancellationOnEndedRef.current = undefined;
    if (cancellationTimerRef.current !== null) {
      window.clearTimeout(cancellationTimerRef.current);
      cancellationTimerRef.current = null;
    }
    if (cancellationDeadlineRef.current !== null) {
      window.clearTimeout(cancellationDeadlineRef.current);
      cancellationDeadlineRef.current = null;
    }
    cancellationDeadlineAtRef.current = null;
    cancellationAbortControllerRef.current?.abort();
    cancellationAbortControllerRef.current = null;
    clearNewerRunAttachTimer();
  }, [clearNewerRunAttachTimer]);

  const finishCancellationUnconfirmed = useCallback((runId: number) => {
    if (cancellationRunIdRef.current !== runId) return;
    cancellationGenerationRef.current += 1;
    cancellationRunIdRef.current = null;
    cancellationInFlightRef.current = false;
    if (cancellationTimerRef.current !== null) {
      window.clearTimeout(cancellationTimerRef.current);
      cancellationTimerRef.current = null;
    }
    if (cancellationDeadlineRef.current !== null) {
      window.clearTimeout(cancellationDeadlineRef.current);
      cancellationDeadlineRef.current = null;
    }
    cancellationDeadlineAtRef.current = null;
    cancellationAbortControllerRef.current?.abort();
    cancellationAbortControllerRef.current = null;
    context.cancelRequestedRunIdRef.current = null;
    const onConfirmationEnded = cancellationOnEndedRef.current;
    cancellationOnEndedRef.current = undefined;
    onConfirmationEnded?.();
    context.setIssue({
      message: "已请求停止，但暂时无法确认运行状态。请重新同步后再操作。",
      canResync: true,
    });
  }, [context]);

  const scheduleCancellationDeadline = useCallback((runId: number) => {
    if (cancellationDeadlineRef.current !== null) {
      window.clearTimeout(cancellationDeadlineRef.current);
    }
    const deadlineAt = cancellationDeadlineAtRef.current ?? (
      Date.now() + CANCELLATION_CONFIRMATION_TIMEOUT_MS
    );
    cancellationDeadlineAtRef.current = deadlineAt;
    const remainingMs = Math.max(0, deadlineAt - Date.now());
    cancellationDeadlineRef.current = window.setTimeout(() => {
      cancellationDeadlineRef.current = null;
      if (!backendRef.current.available || disposedRef.current) return;
      finishCancellationUnconfirmed(runId);
      void frontendLog("WARNING", "assistant_cancel_reconcile_deadline_reached", "停止请求状态确认超过总时限", {
        traceId: context.traceId,
        data: { taskId: context.taskId, runId, timeoutMs: CANCELLATION_CONFIRMATION_TIMEOUT_MS },
      });
    }, remainingMs);
  }, [context, finishCancellationUnconfirmed]);

  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      stopCancellationConfirmation();
    };
  }, [stopCancellationConfirmation]);

  const reconcileCancellation = useCallback(async function reconcileCancellation(
    runId: number,
    generation: number,
  ): Promise<void> {
    if (
      disposedRef.current
      || !backendRef.current.available
      || generation !== cancellationGenerationRef.current
      || cancellationRunIdRef.current !== runId
      || cancellationInFlightRef.current
    ) return;

    if (cancellationAttemptRef.current >= MAX_CANCELLATION_CHECKS) {
      finishCancellationUnconfirmed(runId);
      return;
    }

    const attempt = cancellationAttemptRef.current;
    cancellationAttemptRef.current += 1;
    cancellationInFlightRef.current = true;
    const controller = new AbortController();
    cancellationAbortControllerRef.current = controller;

    const scheduleNextCheck = () => {
      if (cancellationAttemptRef.current >= MAX_CANCELLATION_CHECKS) {
        finishCancellationUnconfirmed(runId);
        return;
      }
      const delay = CANCELLATION_CHECK_DELAYS_MS[
        Math.min(attempt + 1, CANCELLATION_CHECK_DELAYS_MS.length - 1)
      ] ?? 8_000;
      cancellationTimerRef.current = window.setTimeout(() => {
        cancellationTimerRef.current = null;
        void reconcileCancellation(runId, generation);
      }, delay);
    };

    try {
      void frontendLog("INFO", "assistant_cancel_reconcile_started", "停止请求已接受，读取权威运行状态", {
        traceId: context.traceId,
        data: { taskId: context.taskId, runId, attempt: attempt + 1 },
      });
      const outcome = await fetchCanonicalSnapshot(context.taskId, {
        reason: "cancellation_confirmation",
        timeoutMs: RECONCILE_TIMEOUT_MS,
        traceId: context.traceId,
        signal: controller.signal,
      });
      if (
        outcome.kind === "superseded"
        || disposedRef.current
        || generation !== cancellationGenerationRef.current
        || cancellationRunIdRef.current !== runId
      ) return;
      const snapshot = outcome.snapshot;

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
      const onConfirmationEnded = cancellationOnEndedRef.current;
      cancellationOnEndedRef.current = undefined;
      stopCancellationConfirmation(runId);
      onConfirmationEnded?.();

      const currentRun = currentTransportRun(snapshot);
      if (
        currentRun
        && currentRun.runId !== runId
        && (currentRun.status === "pending" || currentRun.status === "running")
      ) {
        clearNewerRunAttachTimer();
        const settledGeneration = cancellationGenerationRef.current;
        const settledBackendGeneration = backendRef.current.generation;
        newerRunAttachTimerRef.current = window.setTimeout(() => {
          newerRunAttachTimerRef.current = null;
          if (
            disposedRef.current
            || !backendRef.current.available
            || settledGeneration !== cancellationGenerationRef.current
            || settledBackendGeneration !== backendRef.current.generation
            || context.cancelRequestedRunIdRef.current === currentRun.runId
          ) return;
          const latestRun = currentTransportRun(context.latestStateRef.current);
          if (
            latestRun?.runId === currentRun.runId
            && (latestRun.status === "pending" || latestRun.status === "running")
          ) context.runtimeControlsRef.current?.resume();
        }, 0);
      }
    } catch (error) {
      // 取消与后端变化已收敛为 superseded；到这里的都是真实失败（含超时），按预算重试。
      if (
        disposedRef.current
        || generation !== cancellationGenerationRef.current
        || cancellationRunIdRef.current !== runId
      ) return;
      if (cancellationAttemptRef.current < MAX_CANCELLATION_CHECKS) {
        scheduleNextCheck();
      } else {
        finishCancellationUnconfirmed(runId);
        void frontendLog("WARNING", "assistant_cancel_reconcile_exhausted", "停止请求状态确认达到重试上限", {
          traceId: context.traceId,
          data: { taskId: context.taskId, runId, attempt: cancellationAttemptRef.current },
          error,
        });
      }
    } finally {
      if (cancellationAbortControllerRef.current === controller) {
        cancellationAbortControllerRef.current = null;
      }
      if (generation === cancellationGenerationRef.current) {
        cancellationInFlightRef.current = false;
      }
    }
  }, [clearNewerRunAttachTimer, context, finishCancellationUnconfirmed, stopCancellationConfirmation]);

  const suspendForBackend = useCallback(() => {
    cancellationGenerationRef.current += 1;
    cancellationAttemptRef.current = 0;
    cancellationAbortControllerRef.current?.abort();
    cancellationAbortControllerRef.current = null;
    cancellationInFlightRef.current = false;
    if (cancellationTimerRef.current !== null) {
      window.clearTimeout(cancellationTimerRef.current);
      cancellationTimerRef.current = null;
    }
    if (cancellationDeadlineRef.current !== null) {
      window.clearTimeout(cancellationDeadlineRef.current);
      cancellationDeadlineRef.current = null;
    }
    clearNewerRunAttachTimer();
  }, [clearNewerRunAttachTimer]);

  const resumeAfterBackendRestored = useCallback(() => {
    // 换代时先作废旧实例上的轮询，再在新实例上重新确认；总时限沿用最初的截止时间。
    suspendForBackend();
    const runId = cancellationRunIdRef.current;
    if (runId === null) return;
    scheduleCancellationDeadline(runId);
    void reconcileCancellation(runId, cancellationGenerationRef.current);
  }, [reconcileCancellation, scheduleCancellationDeadline, suspendForBackend]);

  const confirmCancellation = useCallback((runId: number, onConfirmationEnded?: () => void) => {
    stopCancellationConfirmation();
    if (disposedRef.current || !backendRef.current.available) return;
    cancellationRunIdRef.current = runId;
    cancellationOnEndedRef.current = onConfirmationEnded;
    const generation = cancellationGenerationRef.current;
    cancellationDeadlineAtRef.current = Date.now() + CANCELLATION_CONFIRMATION_TIMEOUT_MS;
    scheduleCancellationDeadline(runId);
    void reconcileCancellation(runId, generation);
  }, [reconcileCancellation, scheduleCancellationDeadline, stopCancellationConfirmation]);

  const cancellationSettled = useCallback((runId: number) => {
    if (cancellationRunIdRef.current !== runId) return;
    stopCancellationConfirmation(runId);
  }, [stopCancellationConfirmation]);

  const hasActiveConfirmation = useCallback(() => cancellationRunIdRef.current !== null, []);

  return {
    confirmCancellation,
    cancellationSettled,
    hasActiveConfirmation,
    suspendForBackend,
    resumeAfterBackendRestored,
  };
}
