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
function isTransportState(value: unknown): value is TransportState {
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
 * 收窄 thread selector 暴露的 external state。
 *
 * assistant-ui 通过 adapter 提交 external state，渲染树在不同 runtime 下的可得性并不一致：
 * 尚未提交时，以及只携带 messages 的只读 core 作用域下，``thread.state`` 都不是 Transport
 * state。因此读取端统一经本函数收窄，而不是在组件里用 ``as unknown as TransportState``
 * 把不可信值断言成契约类型。
 */
export function readRuntimeTransportState(value: unknown): TransportState | null {
  return isTransportState(value) ? value : null;
}

/** 在不可信的 thread external state 中按 ``runId`` 定位 Run；未就绪或不存在时返回 undefined。 */
export function findTransportRun(
  value: unknown,
  runId: number | null,
): TransportState["runs"][number] | undefined {
  const state = readRuntimeTransportState(value);
  return state === null || runId === null
    ? undefined
    : state.runs.find((run) => run.runId === runId);
}

/** 从 Task 的 Run 事实中选取最新一条有效上下文窗口测量。 */
export function latestMeasuredContextUsage(value: unknown): {
  runId: number;
  used: number;
  total: number;
} | null {
  const state = readRuntimeTransportState(value);
  if (state === null) return null;
  let latest: { runId: number; used: number; total: number } | null = null;
  for (const run of state.runs) {
    const used = run.usage?.input_tokens;
    const total = run.contextWindowTotal;
    if (used === null || used === undefined || !Number.isFinite(used) || used <= 0) continue;
    if (total === null || !Number.isFinite(total) || total <= 0) continue;
    if (latest === null || run.runId > latest.runId) latest = { runId: run.runId, used, total };
  }
  return latest;
}
