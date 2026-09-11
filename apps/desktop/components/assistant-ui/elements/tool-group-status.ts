import type { TransportToolStatus } from "@/lib/assistant/contract";

export type ToolGroupPhase = "running" | "completed" | "failed" | "cancelled" | "unknown";

export type ToolGroupSummary = {
  phase: ToolGroupPhase;
  total: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  cancelled: number;
  unknown: number;
};

/**
 * Aggregate the backend lifecycle states of the tool calls in one visual group.
 *
 * `running` is reserved for groups that still contain a pending or running
 * call. Once every call is terminal, failures take precedence over unknown
 * states, cancellations, and success so the collapsed row remains actionable.
 * This is a pure UI projection; it does not mutate transport state or infer
 * status from the assistant message/run lifecycle.
 */
export function summarizeToolGroup(
  statuses: readonly TransportToolStatus[],
): ToolGroupSummary {
  const summary: Omit<ToolGroupSummary, "phase"> = {
    total: statuses.length,
    pending: 0,
    running: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    unknown: 0,
  };

  for (const status of statuses) {
    switch (status) {
      case "pending":
        summary.pending += 1;
        break;
      case "running":
        summary.running += 1;
        break;
      case "completed":
        summary.completed += 1;
        break;
      case "failed":
        summary.failed += 1;
        break;
      case "cancelled":
        summary.cancelled += 1;
        break;
      case "unknown":
        summary.unknown += 1;
        break;
    }
  }

  const phase: ToolGroupPhase = summary.pending + summary.running > 0
    ? "running"
    : summary.failed > 0
      ? "failed"
      : summary.unknown > 0
        ? "unknown"
        : summary.cancelled > 0
          ? "cancelled"
          : "completed";

  return { phase, ...summary };
}

function settledCounts(summary: ToolGroupSummary): string[] {
  const counts: string[] = [];
  if (summary.completed > 0) counts.push(`${summary.completed} 成功`);
  if (summary.failed > 0) counts.push(`${summary.failed} 失败`);
  if (summary.cancelled > 0) counts.push(`${summary.cancelled} 取消`);
  if (summary.unknown > 0) counts.push(`${summary.unknown} 待确认`);
  return counts;
}

/**
 * Create the concise, accessible label shown by the collapsed ToolGroup row.
 * The label keeps the total call count and exposes mixed terminal outcomes.
 */
export function toolGroupSummaryLabel(summary: ToolGroupSummary): string {
  const count = `${summary.total} 个工具调用`;
  const settled = summary.completed + summary.failed + summary.cancelled + summary.unknown;

  switch (summary.phase) {
    case "running": {
      const progress = `${settled}/${summary.total} 已结束`;
      const details = settledCounts(summary);
      return details.length > 0
        ? `${count} · 执行中 · ${progress} · ${details.join(" · ")}`
        : `${count} · 执行中`;
    }
    case "failed":
      return summary.failed === summary.total
        ? `${count} · 全部失败`
        : `${count} · ${settledCounts(summary).join(" · ")}`;
    case "cancelled":
      return summary.cancelled === summary.total
        ? `${count} · 全部取消`
        : `${count} · ${settledCounts(summary).join(" · ")}`;
    case "unknown":
      return `${count} · ${settledCounts(summary).join(" · ")}`;
    case "completed":
      return `${count} · 全部成功`;
  }
}
