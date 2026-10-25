import { describe, expect, it } from "vitest";

import {
  deriveComposerAction,
  getLatestUserMessageId,
  isEditableLatestUserMessage,
  isQueuedUserMessage,
  canResumeStoppedRun,
} from "@/lib/assistant/conversation-actions";
import { activity, assistantMessage, queuedMessage, transportState, userMessage } from "./transport-fixtures";

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
