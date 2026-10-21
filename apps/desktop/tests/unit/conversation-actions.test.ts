import { describe, expect, it } from "vitest";

import {
  deriveComposerAction,
  getLatestUserMessageId,
  getTransportRunId,
  isEditableLatestRunUserMessage,
  canResumeStoppedRun,
} from "@/lib/assistant/conversation-actions";
import type { TransportMessage, TransportState } from "@/lib/assistant/contract";

const state = (overrides: Partial<TransportState> = {}): TransportState => ({
  runs: [],
  current_run_id: null,
  approvals: {},
  error: null,
  ...overrides,
});

const run = (runId: number, status: string = "completed", messages: TransportMessage[] = []) => ({
  runId,
  status,
  endReason: null,
  contextWindowTotal: null,
  messages,
  usage: null,
  error: null,
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
    for (const status of ["pending", "running", "waiting_for_input"]) {
      expect(isEditableLatestRunUserMessage({ ...snapshot, runs: [snapshot.runs[0], run(2, status, snapshot.runs[1].messages)] }, "u2")).toBe(false);
    }
  });

  it("allows a cancelled current run to resume", () => {
    const resumable = state({
      runs: [run(7, "cancelled", [{ id: "u7", role: "user", parts: [] }])],
      current_run_id: 7,
    });
    expect(canResumeStoppedRun(resumable)).toBe(true);
    expect(canResumeStoppedRun({ ...resumable, runs: [run(7, "completed", resumable.runs[0].messages)] })).toBe(false);
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

it("never exposes queued messages for editing and blocks editing while inputs are queued", () => {
  const snapshot = state({
    runs: [run(1, "cancelled", [{ id: "consumed", role: "user", parts: [] }])],
    current_run_id: 1,
    queued_messages: [
      { id: "q1", role: "user", parts: [], delivery: "after_run" },
      { id: "q2", role: "user", parts: [], delivery: "immediate" },
    ],
  });
  expect(getLatestUserMessageId(snapshot)).toBe("consumed");
  expect(isEditableLatestRunUserMessage(snapshot, "consumed")).toBe(false);
  expect(isEditableLatestRunUserMessage(snapshot, "q2")).toBe(false);
  expect(isEditableLatestRunUserMessage({ ...snapshot, queued_messages: [] }, "consumed")).toBe(true);
  expect(canResumeStoppedRun(state({ runs: [run(1, "failed")], current_run_id: 1, queued_messages: [{ id: "q", role: "user", parts: [], delivery: "after_run" }] }))).toBe(true);
});
