import { describe, expect, it } from "vitest";

import {
  deriveComposerAction,
  getLatestUserMessageId,
  getTransportRunId,
  isEditableLatestRunUserMessage,
  isLatestUserMessage,
  isResumableUserCancelledRun,
} from "@/lib/assistant/conversation-actions";
import type { TransportState } from "@/lib/assistant/contract";

const state = (overrides: Partial<TransportState> = {}): TransportState => ({
  messages: [],
  run: { runId: null, status: "idle" },
  approvals: {},
  usage: {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_hit_tokens: 0,
    cache_miss_tokens: 0,
    reasoning_tokens: 0,
  },
  context_usage: 0,
  error: null,
  ...overrides,
});

describe("conversation actions", () => {
  it("only exposes the last canonical user message as editable", () => {
    const snapshot = state({
      messages: [
        { id: "u1", role: "user", runId: 1, status: "completed", parts: [] },
        { id: "a1", role: "assistant", runId: 1, status: "completed", parts: [] },
        { id: "u2", role: "user", runId: 2, status: "completed", parts: [] },
      ],
    });

    expect(getLatestUserMessageId(snapshot)).toBe("u2");
    expect(isLatestUserMessage(snapshot, "u1")).toBe(false);
    expect(isLatestUserMessage(snapshot, "u2")).toBe(true);
  });

  it("only allows editing the latest user message from the task's latest run", () => {
    const snapshot = state({
      run: { runId: 2, status: "completed" },
      messages: [
        { id: "u1", role: "user", runId: 1, status: "completed", parts: [] },
        { id: "a1", role: "assistant", runId: 1, status: "completed", parts: [] },
        { id: "u2", role: "user", runId: 2, status: "completed", parts: [] },
      ],
    });

    expect(isEditableLatestRunUserMessage(snapshot, "u1")).toBe(false);
    expect(isEditableLatestRunUserMessage(snapshot, "u2")).toBe(true);
    expect(isEditableLatestRunUserMessage({ ...snapshot, run: { runId: 3, status: "completed" } }, "u2")).toBe(false);
  });

  it("requires a user-cancelled assistant message before showing resume", () => {
    const resumable = state({
      run: { runId: 7, status: "cancelled" },
      messages: [{ id: "a7", role: "assistant", runId: 7, status: "cancelled", endReason: "user_cancelled", parts: [] }],
    });
    expect(isResumableUserCancelledRun(resumable)).toBe(true);
    expect(isResumableUserCancelledRun({
      ...resumable,
      messages: [{ ...resumable.messages[0], endReason: "runtime_cancelled" }],
    })).toBe(false);
    expect(isResumableUserCancelledRun({ ...resumable, run: { runId: 7, status: "completed" } })).toBe(false);
  });

  it("keeps the three composer button states deterministic", () => {
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true })).toBe("stop");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: true })).toBe("resume");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: false, canResume: true })).toBe("send");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: false })).toBe("send");
  });

  it("safely reads a run id from an uninitialized assistant-ui thread state", () => {
    expect(getTransportRunId(undefined)).toBeNull();
    expect(getTransportRunId({ run: { runId: 9 } })).toBe(9);
    expect(getTransportRunId({ run: { runId: "9" } })).toBeNull();
  });
});
