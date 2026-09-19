import { useCallback, useEffect, useRef } from "react";

import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
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
};

/**
 * Confirm cancellation against the canonical snapshot without creating a
 * second Run state machine. The hook owns only cancellation polling, its
 * bounded timers, and the attach-only handoff for a newer active Run.
 */
export function useCancellationConfirmation(
  context: RuntimeSessionContext,
): CancellationConfirmation {
  const previousBackendAvailableRef = useRef(context.backendRuntimeAvailable);
  const previousBackendGenerationRef = useRef(context.backendRuntimeGeneration);
  const backendAvailableRef = useRef(context.backendRuntimeAvailable);
  const backendGenerationRef = useRef(context.backendRuntimeGeneration);
  backendAvailableRef.current = context.backendRuntimeAvailable;
  backendGenerationRef.current = context.backendRuntimeGeneration;

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
      if (!backendAvailableRef.current || disposedRef.current) return;
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
      || !backendAvailableRef.current
      || generation !== cancellationGenerationRef.current
      || cancellationRunIdRef.current !== runId
      || cancellationInFlightRef.current
    ) return;

    if (cancellationAttemptRef.current >= MAX_CANCELLATION_CHECKS) {
      finishCancellationUnconfirmed(runId);
      return;
    }

    const attempt = cancellationAttemptRef.current;
    const backendGeneration = backendGenerationRef.current;
    cancellationAttemptRef.current += 1;
    cancellationInFlightRef.current = true;
    let timedOut = false;
    const { controller, signal, clear } = createTimeoutAbort(RECONCILE_TIMEOUT_MS, () => {
      timedOut = true;
    });
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
      const snapshot = await requestAssistantSnapshot(context.taskId, { signal, traceId: context.traceId });
      if (
        disposedRef.current
        || !backendAvailableRef.current
        || backendGeneration !== backendGenerationRef.current
        || generation !== cancellationGenerationRef.current
        || controller.signal.aborted
        || cancellationRunIdRef.current !== runId
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
        const settledBackendGeneration = backendGenerationRef.current;
        newerRunAttachTimerRef.current = window.setTimeout(() => {
          newerRunAttachTimerRef.current = null;
          if (
            disposedRef.current
            || !backendAvailableRef.current
            || settledGeneration !== cancellationGenerationRef.current
            || settledBackendGeneration !== backendGenerationRef.current
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
      if (
        disposedRef.current
        || !backendAvailableRef.current
        || generation !== cancellationGenerationRef.current
        || cancellationRunIdRef.current !== runId
      ) return;
      if (!timedOut && controller.signal.aborted) return;
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
      clear();
      if (cancellationAbortControllerRef.current === controller) {
        cancellationAbortControllerRef.current = null;
      }
      if (generation === cancellationGenerationRef.current) {
        cancellationInFlightRef.current = false;
      }
    }
  }, [clearNewerRunAttachTimer, context, finishCancellationUnconfirmed, stopCancellationConfirmation]);

  useEffect(() => {
    const previousAvailable = previousBackendAvailableRef.current;
    const previousGeneration = previousBackendGenerationRef.current;
    const generationChanged = previousGeneration !== context.backendRuntimeGeneration;
    const becameUnavailable = previousAvailable && !context.backendRuntimeAvailable;
    const becameAvailable = !previousAvailable && context.backendRuntimeAvailable;
    previousBackendAvailableRef.current = context.backendRuntimeAvailable;
    previousBackendGenerationRef.current = context.backendRuntimeGeneration;

    if (generationChanged || becameUnavailable) {
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
    }

    if (context.backendRuntimeAvailable) {
      if ((becameAvailable || generationChanged) && cancellationRunIdRef.current !== null) {
        scheduleCancellationDeadline(cancellationRunIdRef.current);
        void reconcileCancellation(
          cancellationRunIdRef.current,
          cancellationGenerationRef.current,
        );
      }
      return;
    }
  }, [context.backendRuntimeAvailable, context.backendRuntimeGeneration, reconcileCancellation, scheduleCancellationDeadline]);

  const confirmCancellation = useCallback((runId: number, onConfirmationEnded?: () => void) => {
    stopCancellationConfirmation();
    if (disposedRef.current || !backendAvailableRef.current) return;
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

  return { confirmCancellation, cancellationSettled, hasActiveConfirmation };
}
