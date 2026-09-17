import { useCallback, useState } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";

type RuntimeCancellation = {
  /** Frontend-only state: the signal was accepted and the workflow is settling. */
  cancellingRunId: number | null;
  onRequested: (runId: number) => void;
  onResult: (runId: number, accepted: boolean) => void;
  onStateCommitted: (state: TransportState) => void;
};

const ACTIVE_RUN_STATUSES = new Set(["pending", "running"]);

/**
 * Coordinate the cancellation signal with the local Assistant UI runtime.
 *
 * The signal acknowledgement is deliberately not projected as a terminal Run
 * state. `cancellingRunId` remains set until a canonical snapshot confirms the
 * requested Run is no longer active, or bounded confirmation expires and the
 * UI offers an explicit state resync.
 */
export function useRuntimeCancellation(
  context: RuntimeSessionContext,
  recovery: RuntimeRecovery,
): RuntimeCancellation {
  const [cancellingRunId, setCancellingRunId] = useState<number | null>(null);
  const {
    cancellationSettled,
    confirmCancellation,
    reconcileAfterTransportFinish,
  } = recovery;

  const onRequested = useCallback((runId: number) => {
    if (context.latestStateRef.current.current_run_id !== runId) return;
    context.cancelRequestedRunIdRef.current = runId;
    context.lastTransportErrorRef.current = null;
    setCancellingRunId(runId);
  }, [context]);

  const onResult = useCallback((runId: number, accepted: boolean) => {
    if (accepted) {
      if (context.latestStateRef.current.current_run_id !== runId) {
        if (context.cancelRequestedRunIdRef.current === runId) {
          context.cancelRequestedRunIdRef.current = null;
        }
        cancellationSettled(runId);
        setCancellingRunId((current) => current === runId ? null : current);
        void reconcileAfterTransportFinish();
        return;
      }
      // The endpoint only acknowledges the process-local cancellation signal.
      // The workflow owns the eventual Run transition and publishes it through
      // the canonical snapshot projector.
      context.lastTransportErrorRef.current = null;
      setCancellingRunId(runId);
      context.setIssue(null);
      confirmCancellation(runId, () => {
        setCancellingRunId((current) => current === runId ? null : current);
      });
      return;
    }

    if (context.cancelRequestedRunIdRef.current !== runId) return;
    context.cancelRequestedRunIdRef.current = null;
    setCancellingRunId((current) => current === runId ? null : current);
    void reconcileAfterTransportFinish();
  }, [cancellationSettled, confirmCancellation, context, reconcileAfterTransportFinish]);

  const onStateCommitted = useCallback((state: TransportState) => {
    const requestedRunId = context.cancelRequestedRunIdRef.current;
    if (requestedRunId === null) return;

    const currentRun = currentTransportRun(state);
    const requestedRun = state.runs.find((run) => run.runId === requestedRunId);
    const isCancellationSettled = currentRun?.runId !== requestedRunId
      || requestedRun === undefined
      || !ACTIVE_RUN_STATUSES.has(requestedRun.status);
    if (!isCancellationSettled) return;

    context.cancelRequestedRunIdRef.current = null;
    context.lastTransportErrorRef.current = null;
    cancellationSettled(requestedRunId);
    setCancellingRunId((current) => current === requestedRunId ? null : current);
  }, [cancellationSettled, context]);

  return { cancellingRunId, onRequested, onResult, onStateCommitted };
}
