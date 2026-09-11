import { useCallback } from "react";

import { markTransportStateCancelled } from "@/lib/assistant/transport-state-operations";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

type RuntimeCancellation = {
  onRequested: (runId: number) => void;
  onResult: (runId: number, accepted: boolean) => void;
};

/** Coordinate backend-authoritative cancellation with the local AUI runtime. */
export function useRuntimeCancellation(
  context: RuntimeSessionContext,
): RuntimeCancellation {
  const onRequested = useCallback((runId: number) => {
    if (context.latestStateRef.current.current_run_id !== runId) return;
    context.cancelRequestedRunIdRef.current = runId;
    context.lastTransportErrorRef.current = null;
  }, [context]);

  const onResult = useCallback((runId: number, accepted: boolean) => {
    if (accepted) {
      if (context.latestStateRef.current.current_run_id !== runId) return;
      context.lastTransportErrorRef.current = null;
      const cancelledState = markTransportStateCancelled(context.latestStateRef.current, runId);
      context.latestStateRef.current = cancelledState;
      context.runtimeControlsRef.current?.importState(cancelledState);
      context.cancelRequestedRunIdRef.current = null;
      context.setIssue(null);
      context.onTaskStateChanged?.();
      return;
    }

    if (context.cancelRequestedRunIdRef.current !== runId) return;
    context.cancelRequestedRunIdRef.current = null;
  }, [context]);

  return { onRequested, onResult };
}
