import { describe, expect, it } from "vitest";
import { agentStatusPollInterval } from "@/lib/workbench/agent-status";
import { agentReaderNeedsSync } from "@/lib/workbench/use-agent-reader";
import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";

describe("工作台 Agent 观察策略", () => {
  it.each(["pending", "running", "waiting_for_input"])("%s 采用活动刷新间隔", status => {
    expect(agentStatusPollInterval(status)).toBe(3_000);
  });
  it.each(["completed", "failed", "cancelled", null])("%s 保留低频发现入口", status => {
    expect(agentStatusPollInterval(status)).toBe(10_000);
  });
  it("新 Run 必须重新读 canonical snapshot", () => {
    expect(agentReaderNeedsSync({ runId: 1, status: "running" }, { runId: 2, status: "running" }, false)).toBe(true);
  });
  it("已有流负责同一 Run 的状态变化，结束后才根据摘要重新同步", () => {
    expect(agentReaderNeedsSync({ runId: 1, status: "waiting_for_input" }, { runId: 1, status: "running" }, false)).toBe(false);
    expect(agentReaderNeedsSync({ runId: 1, status: "cancelled" }, { runId: 1, status: "running" }, true)).toBe(true);
    expect(agentReaderNeedsSync({ runId: 1, status: "completed" }, { runId: 1, status: "completed" }, true)).toBe(false);
  });
  it("标签显示完成态，侧栏继续隐藏完成态", () => {
    expect(taskRunStatusPresentation("completed", true)?.label).toBe("已完成");
    expect(taskRunStatusPresentation("completed")).toBeNull();
  });
});
