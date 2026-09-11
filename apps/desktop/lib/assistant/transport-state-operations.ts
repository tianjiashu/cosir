import type { TransportState } from "@/lib/assistant/contract";

/** Return the run selected by the task-level canonical snapshot. */
export function currentTransportRun(
  state: TransportState,
): TransportState["runs"][number] | undefined {
  return state.current_run_id === null
    ? undefined
    : state.runs.find((run) => run.runId === state.current_run_id);
}

/** Count messages across all persisted runs in a transport snapshot. */
export function transportMessageCount(state: TransportState): number {
  return state.runs.reduce((count, run) => count + run.messages.length, 0);
}

/** Perform the shallow runtime guard used by the Assistant UI state bridge. */
export function isTransportState(value: unknown): value is TransportState {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { runs?: unknown; current_run_id?: unknown };
  if (!Array.isArray(candidate.runs)) return false;
  if (candidate.current_run_id !== null
    && (!Number.isInteger(candidate.current_run_id) || (candidate.current_run_id as number) < 0)) return false;
  return candidate.runs.every((run) => {
    if (typeof run !== "object" || run === null || Array.isArray(run)) return false;
    const candidateRun = run as { runId?: unknown; status?: unknown; messages?: unknown };
    return Number.isInteger(candidateRun.runId)
      && (candidateRun.runId as number) >= 0
      && typeof candidateRun.status === "string"
      && Array.isArray(candidateRun.messages);
  });
}

/**
 * Project an accepted backend cancellation into the local transport state.
 *
 * This is only a race fallback for a stream that closes before its terminal
 * snapshot arrives. A later canonical snapshot may replace this projection.
 */
export function markTransportStateCancelled(
  state: TransportState,
  runId: number,
): TransportState {
  const runIndex = state.runs.findIndex((run) => run.runId === runId);
  if (runIndex < 0) return state;

  const runs = state.runs.map((candidate, index) => index === runIndex
    ? {
      ...candidate,
      status: "cancelled",
      endReason: "user_cancelled",
      messages: candidate.messages.map((message) => ({
        ...message,
        parts: message.parts.map((part) => (
          part.type === "tool-call" && (part.status === "pending" || part.status === "running")
            ? { ...part, status: "cancelled", error: "已取消", isError: false }
            : part
        )),
      })),
    }
    : candidate);

  return {
    ...state,
    runs,
    current_run_id: runId,
  };
}
