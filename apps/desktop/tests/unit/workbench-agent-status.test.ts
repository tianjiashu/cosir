import { describe, expect, it } from "vitest";
import { agentStatusPollInterval } from "@/lib/workbench/agent-status";
import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";

describe("工作台 Agent 观察策略", () => {
  it.each(["pending", "running", "waiting_for_input"])("%s 采用活动刷新间隔", status => {
    expect(agentStatusPollInterval(status)).toBe(3_000);
  });
  it.each(["completed", "failed", "cancelled", null])("%s 保留低频发现入口", status => {
    expect(agentStatusPollInterval(status)).toBe(10_000);
  });
  it("标签显示完成态，侧栏继续隐藏完成态", () => {
    expect(taskRunStatusPresentation("completed", true)?.label).toBe("已完成");
    expect(taskRunStatusPresentation("completed")).toBeNull();
  });
});
