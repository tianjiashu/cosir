import { postJson, requestJson } from "@/lib/http/client";

export type ChangeNetDiff = {
  state: "verified" | "conflict" | "unverifiable" | string;
  additions: number;
  deletions: number;
  patch: string | null;
  truncated: boolean;
  has_unrendered_changes: boolean;
};

export type TaskFileChange = {
  change_id: string;
  paths: string[];
  action: string;
  status: string;
  last_run_id: number | null;
  operation_count: number;
  net_diff: ChangeNetDiff | null;
};

export type TaskChangeSet = {
  task_id: number;
  files: TaskFileChange[];
};

export type TaskChangesResponse = TaskChangeSet;

export type TaskChangeOutcome =
  | "kept"
  | "already_kept"
  | "reverted"
  | "already_reverted"
  | "stale_change_id"
  | "conflict"
  | "snapshot_unverifiable"
  | "failed"
  | string;

export type TaskChangeResult = {
  change_id: string;
  outcome: TaskChangeOutcome;
  reason_code?: string | null;
  message?: string | null;
};

export type TaskChangeActionResponse = {
  task_id: number;
  results: TaskChangeResult[];
  change_set: TaskChangeSet;
};

type ReadRequestOptions = Pick<RequestInit, "signal">;

/** Read the current task-scoped ChangeSet without touching Assistant Transport state. */
export const getTaskChanges = (taskId: number, options?: ReadRequestOptions) =>
  requestJson<TaskChangesResponse>(`/tasks/${taskId}/changes`, options);

function postChangeAction(
  taskId: number,
  action: "keep" | "revert",
  changeIds: string[],
  signal?: AbortSignal,
): Promise<TaskChangeActionResponse> {
  return postJson<TaskChangeActionResponse>(
    `/tasks/${taskId}/changes/${action}`,
    { change_ids: changeIds },
    signal ? { signal } : undefined,
  );
}

/** Keep one or more pending file groups at their current state as a new baseline. */
export const keepTaskChanges = (
  taskId: number,
  changeIds: string[],
  signal?: AbortSignal,
) => postChangeAction(taskId, "keep", changeIds, signal);

/** Revert one or more pending file groups uniformly to their latest baseline. */
export const revertTaskChanges = (
  taskId: number,
  changeIds: string[],
  signal?: AbortSignal,
) => postChangeAction(taskId, "revert", changeIds, signal);
