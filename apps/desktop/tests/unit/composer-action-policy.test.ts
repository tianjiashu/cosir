import { describe, expect, it } from "vitest";

import {
  deriveComposerAction,
  isWithdrawableLatestUserMessage,
  isQueuedUserMessage,
} from "@/lib/assistant/composer/action-policy";
import { activity, agentMessage, agentMessagePart, assistantMessage, queuedMessage, transportState, userMessage } from "./transport-fixtures";

describe("conversation actions", () => {
  it("只在 Task 已停下时允许撤回最新一条已并入的用户消息，只看活动是否已落定", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1), userMessage("u2", 2)],
      activity: activity(2, "completed"),
    });
    expect(isWithdrawableLatestUserMessage(snapshot, "u1")).toBe(false);
    expect(isWithdrawableLatestUserMessage(snapshot, "u2")).toBe(true);
    for (const status of ["running", "suspended"] as const) {
      expect(isWithdrawableLatestUserMessage({ ...snapshot, activity: activity(2, status) }, "u2")).toBe(false);
    }
    expect(isWithdrawableLatestUserMessage({ ...snapshot, activity: null }, "u2")).toBe(false);
  });

  it("最新一条是 Agent 消息时没有可撤回的消息：它不可撤回，也不能越过它撤回更早的用户消息", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1), agentMessage("a2", 2)],
      activity: activity(2, "completed"),
    });
    expect(isWithdrawableLatestUserMessage(snapshot, "a2")).toBe(false);
    expect(isWithdrawableLatestUserMessage(snapshot, "u1")).toBe(false);
  });

  it("途中来信之后用户消息不可撤回：后端把它算作最新输入", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1, [agentMessagePart("a2")])],
      activity: activity(1, "completed"),
    });
    expect(isWithdrawableLatestUserMessage(snapshot, "u1")).toBe(false);
    const inherited = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1, [agentMessagePart("a2")])],
      activity: activity(1, "completed"),
    });
    inherited.messages[1] = { ...inherited.messages[1], inherited: true };
    expect(isWithdrawableLatestUserMessage(inherited, "u1")).toBe(true);
  });

  it("keeps the composer button states deterministic", () => {
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true })).toBe("stop");
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true, isCancelling: true })).toBe("cancelling");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: true })).toBe("resume");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: false, canResume: true })).toBe("send");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: false })).toBe("send");
  });
});

it("queued messages are not withdrawable and do not block withdrawing the latest consumed message", () => {
  const snapshot = transportState({
    messages: [userMessage("consumed", 1)],
    activity: activity(1, "cancelled"),
    queued_messages: [queuedMessage("q1", "a", "next_run"), queuedMessage("q2", "b", "immediate")],
  });
  expect(isWithdrawableLatestUserMessage(snapshot, "consumed")).toBe(true);
  expect(isWithdrawableLatestUserMessage(snapshot, "q2")).toBe(false);
  expect(isQueuedUserMessage(snapshot, "q2")).toBe(true);
  expect(isQueuedUserMessage(snapshot, "consumed")).toBe(false);
});
