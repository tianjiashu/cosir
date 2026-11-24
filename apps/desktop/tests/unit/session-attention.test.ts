import { describe, expect, it } from "vitest";

import { sessionAttention } from "@/lib/session-status/session-attention";
import type { SessionStatus } from "@/lib/session-status/session-status-store";

const status = (patch: Partial<SessionStatus>): SessionStatus => ({
  executionStatus: null, awaitingDecisions: 0, awaitingApprovals: 0, unread: false, ...patch,
});

// 目的：侧栏一眼分清「卡在我这、我没看过、还在跑」；潜在缺陷：几种提醒互相覆盖错位，或已看过的结果仍挂着标记。
describe("sessionAttention", () => {
  it("在等你的两类优先，与 Run 状态无关；待审批先于待回答", () => {
    expect(sessionAttention(status({ executionStatus: "cancelled", awaitingApprovals: 1, awaitingDecisions: 1 }))).toBe("approval");
    expect(sessionAttention(status({ executionStatus: "completed", awaitingDecisions: 1, unread: true }))).toBe("answer");
  });

  it("运行与挂起等后台结果都是进行中", () => {
    expect(sessionAttention(status({ executionStatus: "running" }))).toBe("working");
    expect(sessionAttention(status({ executionStatus: "suspended", unread: true }))).toBe("working");
  });

  it("没看过的结果按结局分失败与完成；正开着这个会话时不提醒", () => {
    expect(sessionAttention(status({ executionStatus: "failed", unread: true }))).toBe("failed");
    expect(sessionAttention(status({ executionStatus: "completed", unread: true }))).toBe("unread");
    expect(sessionAttention(status({ executionStatus: "completed", unread: true }), true)).toBeNull();
    expect(sessionAttention(status({ executionStatus: "running" }), true)).toBe("working");
  });

  it("看过的、被停下的、没有 Run 的、还没推送的都不留标记", () => {
    for (const quiet of [status({ executionStatus: "completed" }), status({ executionStatus: "failed" }), status({ executionStatus: "cancelled" }), status({})]) {
      expect(sessionAttention(quiet)).toBeNull();
    }
    expect(sessionAttention(null)).toBeNull();
  });
});
