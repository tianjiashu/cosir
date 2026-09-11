import { describe, expect, it } from "vitest";

import {
  summarizeToolGroup,
  toolGroupSummaryLabel,
} from "@/components/assistant-ui/elements/tool-group-status";

describe("tool group status aggregation", () => {
  it("keeps the group running while any tool is pending or running", () => {
    const summary = summarizeToolGroup(["completed", "failed", "running", "cancelled"]);

    expect(summary).toMatchObject({
      phase: "running",
      total: 4,
      completed: 1,
      failed: 1,
      running: 1,
      cancelled: 1,
    });
    expect(toolGroupSummaryLabel(summary)).toBe("4 个工具调用 · 执行中 · 3/4 已结束 · 1 成功 · 1 失败 · 1 取消");
  });

  it("shows all-success as a settled group", () => {
    const summary = summarizeToolGroup(["completed", "completed"]);

    expect(summary.phase).toBe("completed");
    expect(toolGroupSummaryLabel(summary)).toBe("2 个工具调用 · 全部成功");
  });

  it("preserves failure precedence for mixed terminal outcomes", () => {
    const summary = summarizeToolGroup(["completed", "failed", "cancelled"]);

    expect(summary.phase).toBe("failed");
    expect(toolGroupSummaryLabel(summary)).toBe("3 个工具调用 · 1 成功 · 1 失败 · 1 取消");
  });

  it("shows all-cancelled as cancelled rather than failed", () => {
    const summary = summarizeToolGroup(["cancelled", "cancelled"]);

    expect(summary.phase).toBe("cancelled");
    expect(toolGroupSummaryLabel(summary)).toBe("2 个工具调用 · 全部取消");
  });
});
