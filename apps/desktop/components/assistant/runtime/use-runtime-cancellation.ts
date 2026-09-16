import { useCallback, useState } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
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
 * state. `cancellingRunId` remains set until a canonical snapshot shows that
 * the requested Run is no longer active.
 */
export function useRuntimeCancellation(
  context: RuntimeSessionContext,
): RuntimeCancellation {
  const [cancellingRunId, setCancellingRunId] = useState<number | null>(null);

  const onRequested = useCallback((runId: number) => {
    if (context.latestStateRef.current.current_run_id !== runId) return;
    context.cancelRequestedRunIdRef.current = runId;
    context.lastTransportErrorRef.current = null;
    setCancellingRunId(runId);
  }, [context]);

  const onResult = useCallback((runId: number, accepted: boolean) => {
    if (accepted) {
      if (context.latestStateRef.current.current_run_id !== runId) return;
      // The endpoint only acknowledges the process-local cancellation signal.
      // The workflow owns the eventual Run transition and publishes it through
      // the canonical snapshot projector.
      context.lastTransportErrorRef.current = null;
      setCancellingRunId(runId);
      context.setIssue(null);
      return;
    }

    if (context.cancelRequestedRunIdRef.current !== runId) return;
    context.cancelRequestedRunIdRef.current = null;
    setCancellingRunId((current) => current === runId ? null : current);
  }, [context]);

  const onStateCommitted = useCallback((state: TransportState) => {
    const requestedRunId = context.cancelRequestedRunIdRef.current;
    if (requestedRunId === null) return;

    const currentRun = currentTransportRun(state);
    const requestedRun = state.runs.find((run) => run.runId === requestedRunId);
    const cancellationSettled = currentRun?.runId !== requestedRunId
      || requestedRun === undefined
      || !ACTIVE_RUN_STATUSES.has(requestedRun.status);
    if (!cancellationSettled) return;

    context.cancelRequestedRunIdRef.current = null;
    context.lastTransportErrorRef.current = null;
    setCancellingRunId((current) => current === requestedRunId ? null : current);
  }, [context]);

  return { cancellingRunId, onRequested, onResult, onStateCommitted };
}
