import { beforeEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ requestOk: vi.fn() }));
vi.mock("@/lib/http/client", async (importOriginal) => ({ ...(await importOriginal<object>()), requestOk: http.requestOk }));

import { approvalTarget, commandPending, decisionTarget, useCommandRequests } from "@/lib/assistant/command-request-store";
import { backgroundToolCall, cancelSession, cancelToolCall, decideTerminalApproval, submitToolDecision } from "@/lib/assistant/session-commands";
import { HttpError } from "@/lib/http/http-error";

/**
 * 占用命令暂态槽位的命令都必须有界：一直不返回的请求会让槽位永远停在进行中、按钮永远锁住。
 * 时限由 session-commands 的占位入口统一施加，这里守住「每个槽位命令都带上时限」与超时后的结算。
 */
describe("slot commands", () => {
  beforeEach(() => {
    http.requestOk.mockReset();
    useCommandRequests.setState({ sessions: {} });
  });

  it("every slot command is sent with the idempotent timeout", async () => {
    http.requestOk.mockResolvedValue(undefined);
    await cancelSession(1);
    await cancelToolCall(1, "c1");
    await backgroundToolCall(1, "c2");
    await submitToolDecision(1, "c3", { decision: "skip" } as never);
    await decideTerminalApproval(1, "a1", "approve");
    expect(http.requestOk).toHaveBeenCalledTimes(5);
    for (const [, init] of http.requestOk.mock.calls) expect(init.timeoutMs).toBe(15_000);
  });

  it("a timed-out approval leaves the slot retryable instead of locked", async () => {
    http.requestOk.mockRejectedValue(new DOMException("timed out", "TimeoutError"));
    await expect(decideTerminalApproval(1, "a1", "reject")).rejects.toThrow();
    const slot = useCommandRequests.getState().sessions[1]?.[approvalTarget("a1")];
    expect(slot?.error).toBeTruthy();
    expect(commandPending(slot)).toBe(false);
  });

  it("a rejected decision releases the slot", async () => {
    http.requestOk.mockRejectedValue(new HttpError("这次决定已失效", { status: 409, code: "DECISION_ALREADY_SUBMITTED" }));
    await expect(submitToolDecision(1, "c3", { decision: "skip" } as never)).rejects.toThrow();
    expect(useCommandRequests.getState().sessions[1]?.[decisionTarget("c3")]).toBeUndefined();
  });
});
