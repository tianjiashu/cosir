import { describe, expect, it } from "vitest";

import {
  deriveComposerAction,
  editBlockedReason,
  getLatestUserMessageId,
  isEditableLatestUserMessage,
  isQueuedUserMessage,
  canResumeStoppedRun,
} from "@/lib/assistant/composer/action-policy";
import { activity, agentMessage, agentMessagePart, assistantMessage, queuedMessage, transportState, userMessage } from "./transport-fixtures";

describe("conversation actions", () => {
  it("only exposes the last canonical user message as editable", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1), userMessage("u2", 2)],
      activity: activity(2, "completed"),
    });
    expect(getLatestUserMessageId(snapshot)).toBe("u2");
  });

  it("只在 Task 已停下时允许编辑最新一条已并入的用户消息，不比对 runId", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), userMessage("u2", 2)],
      activity: activity(2, "completed"),
    });
    expect(isEditableLatestUserMessage(snapshot, "u1")).toBe(false);
    expect(isEditableLatestUserMessage(snapshot, "u2")).toBe(true);
    for (const status of ["running", "suspended"] as const) {
      expect(isEditableLatestUserMessage({ ...snapshot, activity: activity(2, status) }, "u2")).toBe(false);
    }
    expect(isEditableLatestUserMessage({ ...snapshot, activity: null }, "u2")).toBe(false);
  });

  it("最新一条是 Agent 消息时没有可编辑的消息：它不可编辑，也不能越过它改更早的用户消息", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1), agentMessage("a2", 2)],
      activity: activity(2, "completed"),
    });
    expect(isEditableLatestUserMessage(snapshot, "a2")).toBe(false);
    expect(isEditableLatestUserMessage(snapshot, "u1")).toBe(false);
    expect(editBlockedReason(snapshot, "u1")).not.toBeNull();
  });

  it("途中来信之后用户消息不可编辑：后端把它算作最新输入", () => {
    const snapshot = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-u1", 1, [agentMessagePart("a2")])],
      activity: activity(1, "completed"),
    });
    expect(isEditableLatestUserMessage(snapshot, "u1")).toBe(false);
    expect(editBlockedReason(snapshot, "u1")).toBe("已有更新的消息，这一条无法再编辑并重跑。");
    const inherited = transportState({
      messages: [userMessage("u1", 1), assistantMessage("assistant-context", 1, [agentMessagePart("a2")])],
      activity: activity(1, "completed"),
    });
    inherited.messages[1] = { ...inherited.messages[1], runId: null };
    expect(isEditableLatestUserMessage(inherited, "u1")).toBe(true);
  });

  it("allows a cancelled or failed activity to resume", () => {
    const resumable = transportState({ messages: [userMessage("u7", 7)], activity: activity(7, "cancelled") });
    expect(canResumeStoppedRun(resumable)).toBe(true);
    expect(canResumeStoppedRun({ ...resumable, activity: activity(7, "failed") })).toBe(true);
    expect(canResumeStoppedRun({ ...resumable, activity: activity(7, "completed") })).toBe(false);
    expect(canResumeStoppedRun({ ...resumable, activity: activity(7, "suspended") })).toBe(false);
    expect(canResumeStoppedRun(transportState())).toBe(false);
  });

  it("keeps the composer button states deterministic", () => {
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true })).toBe("stop");
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true, isCancelling: true })).toBe("cancelling");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: true })).toBe("resume");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: false, canResume: true })).toBe("send");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: false })).toBe("send");
  });
});

it("edits queued messages in place and still allows editing the latest consumed message", () => {
  const snapshot = transportState({
    messages: [userMessage("consumed", 1)],
    activity: activity(1, "cancelled"),
    queued_messages: [queuedMessage("q1", "a", "next_run"), queuedMessage("q2", "b", "immediate")],
  });
  expect(getLatestUserMessageId(snapshot)).toBe("consumed");
  expect(isEditableLatestUserMessage(snapshot, "consumed")).toBe(true);
  expect(isEditableLatestUserMessage(snapshot, "q2")).toBe(false);
  expect(isQueuedUserMessage(snapshot, "q2")).toBe(true);
  expect(isQueuedUserMessage(snapshot, "consumed")).toBe(false);
  expect(canResumeStoppedRun(transportState({ activity: activity(1, "failed"), queued_messages: [queuedMessage("q")] }))).toBe(true);
});

describe("edit blocked reason", () => {
  it("重跑编辑在出现更新的消息或重新运行后失效", () => {
    const stopped = transportState({
      messages: [userMessage("u1", 1), userMessage("u2", 2)],
      activity: activity(2, "completed"),
    });
    expect(editBlockedReason(stopped, "u2")).toBeNull();
    expect(editBlockedReason(stopped, "u1")).toContain("更新的消息");
    expect(editBlockedReason({ ...stopped, activity: activity(3, "running") }, "u2"))
      .toContain("正在运行");
  });
});
