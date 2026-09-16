import { describe, expect, it } from "vitest";

import {
  deriveComposerAction,
  getLatestUserMessageId,
  getTransportRunId,
  isEditableLatestRunUserMessage,
  isLatestUserMessage,
  isResumableCancelledRun,
} from "@/lib/assistant/conversation-actions";
import type { TransportMessage, TransportState } from "@/lib/assistant/contract";

const state = (overrides: Partial<TransportState> = {}): TransportState => ({
  runs: [],
  current_run_id: null,
  approvals: {},
  context_usage_ratio: null,
  context_usage_used: null,
  context_window_total: null,
  error: null,
  ...overrides,
});

const run = (runId: number, status: string = "completed", messages: TransportMessage[] = []) => ({
  runId,
  status,
  endReason: null,
  messages,
  usage: null,
});

describe("conversation actions", () => {
  it("only exposes the last canonical user message as editable", () => {
    const snapshot = state({
      runs: [
        run(1, "completed", [{ id: "u1", role: "user", parts: [] }, { id: "a1", role: "assistant", parts: [] }]),
        run(2, "completed", [{ id: "u2", role: "user", parts: [] }]),
      ],
      current_run_id: 2,
    });
    expect(getLatestUserMessageId(snapshot)).toBe("u2");
    expect(isLatestUserMessage(snapshot, "u1")).toBe(false);
    expect(isLatestUserMessage(snapshot, "u2")).toBe(true);
  });

  it("only allows editing the latest user message from the current run", () => {
    const snapshot = state({
      runs: [
        run(1, "completed", [{ id: "u1", role: "user", parts: [] }]),
        run(2, "completed", [{ id: "u2", role: "user", parts: [] }]),
      ],
      current_run_id: 2,
    });
    expect(isEditableLatestRunUserMessage(snapshot, "u1")).toBe(false);
    expect(isEditableLatestRunUserMessage(snapshot, "u2")).toBe(true);
    expect(isEditableLatestRunUserMessage({ ...snapshot, current_run_id: 3 }, "u2")).toBe(false);
  });

  it("allows a cancelled current run to resume", () => {
    const resumable = state({
      runs: [run(7, "cancelled", [{ id: "u7", role: "user", parts: [] }])],
      current_run_id: 7,
    });
    expect(isResumableCancelledRun(resumable)).toBe(true);
    expect(isResumableCancelledRun({ ...resumable, runs: [run(7, "completed", resumable.runs[0].messages)] })).toBe(false);
  });

  it("keeps the three composer button states deterministic", () => {
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true })).toBe("stop");
    expect(deriveComposerAction({ isRunning: true, isDraftEmpty: true, canResume: true, isCancelling: true })).toBe("cancelling");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: true })).toBe("resume");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: false, canResume: true })).toBe("send");
    expect(deriveComposerAction({ isRunning: false, isDraftEmpty: true, canResume: false })).toBe("send");
  });

  it("reads only current_run_id from canonical state", () => {
    expect(getTransportRunId(undefined)).toBeNull();
    expect(getTransportRunId({ current_run_id: 9 })).toBe(9);
    expect(getTransportRunId({ current_run_id: "9" })).toBeNull();
  });
});
