import { isActiveRunStatus, type TransportState } from "@/lib/assistant/contract";

/** Count the canonical messages of a transport snapshot (queued inputs excluded). */
export function transportMessageCount(state: TransportState): number {
  return state.messages.length;
}

/** 当前活动是否处于活动态（运行中或挂起）；Task 尚无 Run 时为 false。 */
export function isActivityActive(state: TransportState): boolean {
  return isActiveRunStatus(state.activity?.status);
}

/** Perform the shallow runtime guard used by the Assistant UI state bridge. */
function isTransportState(value: unknown): value is TransportState {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { messages?: unknown; activity?: unknown };
  if (!Array.isArray(candidate.messages)) return false;
  return candidate.activity === null
    || (typeof candidate.activity === "object" && candidate.activity !== undefined
      && typeof (candidate.activity as { status?: unknown }).status === "string");
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

/**
 * Task 当前的上下文窗口占用。
 *
 * 「已用」与「上限」必须来自同一个 Run（窗口随 Run 冻结，两轮之间可能换了模型）：优先取当前活动
 * 的用量与窗口；当前活动刚开始还没有用量时，回落到最近一条带用量的历史回复结局，用它自己记下的
 * 窗口（同一 Task 的上下文前缀连续，作为新一轮开始前的占用）。选中的那一对窗口未知时不显示，不拿
 * 别的 Run 的窗口凑。
 */
export function latestMeasuredContextUsage(value: unknown): {
  used: number;
  total: number;
} | null {
  const state = readRuntimeTransportState(value);
  if (state === null) return null;
  const measured = (count: number | null | undefined): count is number => (
    count !== null && count !== undefined && Number.isFinite(count) && count > 0
  );
  const pair = (used: number, total: number | null) => (
    measured(total) ? { used, total } : null
  );
  const current = state.activity?.usage?.input_tokens;
  if (measured(current)) return pair(current, state.activity?.contextWindowTotal ?? null);
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const outcome = state.messages[index].outcome;
    const used = outcome?.usage?.input_tokens;
    if (outcome && measured(used)) return pair(used, outcome.contextWindowTotal);
  }
  return null;
}
