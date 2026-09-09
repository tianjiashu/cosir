import { describe, expect, it } from "vitest";

import {
  toMessageStatus,
  toThreadMessage,
  toToolCallPart,
  toTransportThreadView,
} from "@/lib/assistant/converter";
import type { TransportMessage, TransportState, TransportToolCallPart } from "@/lib/assistant/contract";

const emptyState = (): TransportState => ({
  messages: [],
  run: { runId: null, status: "idle" },
  approvals: {},
  context_usage: 0,
  context_revision: null,
  usage_run_id: null,
  context_usage_used: null,
  context_window_total: null,
  usage: {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_hit_tokens: 0,
    cache_miss_tokens: 0,
    reasoning_tokens: 0,
  },
  error: null,
});

const tool = (status: TransportToolCallPart["status"], extra: Partial<TransportToolCallPart> = {}): TransportToolCallPart => ({
  type: "tool-call",
  toolCallId: `tool-${status}`,
  toolName: "execute_terminal",
  status,
  args: { command: "npm test" },
  presentation: { expand_layout: "terminal" },
  ...extra,
});

describe("assistant transport converter", () => {
  it("keeps an empty snapshot empty and does not invent a run", () => {
    const state = emptyState();
    const result = toTransportThreadView(state, { pendingCommands: [], isSending: false });
    expect(result.messages).toEqual([]);
    expect(result.isRunning).toBe(false);
    expect(result.state).toBe(state);
  });

  it("maps streaming text and reasoning without losing their running status", () => {
    const message: TransportMessage = {
      id: "assistant-1",
      role: "assistant",
      status: "running",
      endReason: null,
      parts: [
        { type: "text", text: "partial **markdown", status: "running" },
        { type: "reasoning", text: "checking", status: "completed" },
      ],
    };
    const converted = toThreadMessage(message);
    expect(converted.content[0]).toMatchObject({ type: "text", text: "partial **markdown", status: { type: "running" } });
    expect(converted.content[1]).toMatchObject({ type: "reasoning", text: "checking", status: { type: "complete" } });
    expect(converted.status).toEqual({ type: "running" });
  });

  it("maps all backend tool lifecycle states and preserves artifact data", () => {
    const pending = toToolCallPart(tool("pending"));
    const running = toToolCallPart(tool("running"));
    const completed = toToolCallPart(tool("completed", { result: { exit_code: 0 }, data: { kind: "terminal-result" } }));
    const failed = toToolCallPart(tool("failed", { error: "denied", errorCode: "DENIED" }));
    const cancelled = toToolCallPart(tool("cancelled"));

    expect(pending).toMatchObject({ type: "tool-call", argsText: '{\n  "command": "npm test"\n}', artifact: { backendStatus: "pending" } });
    expect(running).toMatchObject({ artifact: { backendStatus: "running" } });
    expect(completed).toMatchObject({ result: { exit_code: 0 }, isError: false, artifact: { backendStatus: "completed", data: { kind: "terminal-result" } } });
    expect(failed).toMatchObject({ isError: true, artifact: { backendStatus: "failed", errorCode: "DENIED" } });
    expect(failed).not.toHaveProperty("result");
    expect(cancelled).toMatchObject({ isError: false, artifact: { backendStatus: "cancelled" } });
    expect(cancelled).not.toHaveProperty("result");
  });

  it("keeps web extract UI data status-only and drops its document result", () => {
    const converted = toToolCallPart(tool("completed", {
      toolName: "web_extract",
      result: "private document body",
      data: {
        kind: "web-extract-status",
        provider: "fake",
        sites: [{
          site: "example.com",
          url: "https://example.com",
          status: "success",
          content: "should never render",
        }],
      },
    }));

    expect(converted).not.toHaveProperty("result");
    expect(converted).toMatchObject({
      artifact: {
        data: {
          kind: "web-extract-status",
          sites: [{ site: "example.com", status: "success" }],
        },
      },
    });
    expect(JSON.stringify(converted)).not.toContain("should never render");
  });

  it("does not treat unknown message status as success", () => {
    expect(toMessageStatus({ id: "m", role: "assistant", status: "future-status", endReason: null, parts: [] })).toEqual({ type: "incomplete", reason: "other" });
  });

  it("keeps unknown tool and run states explicitly non-success", () => {
    const unknownTool = toToolCallPart(tool("future-status" as TransportToolCallPart["status"]));
    expect(unknownTool).toMatchObject({
      isError: true,
      result: { kind: "tool-unknown" },
      artifact: { backendStatus: "unknown" },
    });
    const state = emptyState();
    state.run.status = "future-run-status";
    const result = toTransportThreadView(state, { pendingCommands: [], isSending: false });
    expect(result.isRunning).toBe(true);
    expect(result.state).toBe(state);
  });

  it("keeps canonical errors visible and shows pending user commands optimistically", () => {
    const state = emptyState();
    state.error = { code: "MODEL_SELECTION_REQUIRED", message: "请先选择模型", retryable: false };
    const command = { type: "add-message", message: { role: "user", parts: [{ type: "text", text: "你好" }] } };
    const result = toTransportThreadView(state, {
      isSending: true,
      pendingCommands: [command],
    });
    const repeated = toTransportThreadView(state, {
      isSending: true,
      pendingCommands: [command],
    });
    expect(result.isRunning).toBe(true);
    expect(result.messages).toHaveLength(2);
    expect(result.messages[0]).toMatchObject({ role: "assistant", status: { type: "incomplete", reason: "error" } });
    expect(result.messages[1]).toMatchObject({ id: expect.stringMatching(/^pending-transport-/), role: "user" });
    expect(repeated.messages[1].id).toBe(result.messages[1].id);
  });

  it("marks only the last assistant message in each run for task fork actions", () => {
    const state = emptyState();
    state.messages = [
      { id: "u1", role: "user", runId: 1, status: "completed", endReason: null, parts: [] },
      { id: "a1", role: "assistant", runId: 1, status: "completed", endReason: null, parts: [] },
      { id: "a1-tool-followup", role: "assistant", runId: 1, status: "completed", endReason: null, parts: [] },
      { id: "u2", role: "user", runId: 2, status: "completed", endReason: null, parts: [] },
      { id: "a2", role: "assistant", runId: 2, status: "completed", endReason: null, parts: [] },
    ];

    const messages = toTransportThreadView(state, { pendingCommands: [], isSending: false }).messages;
    expect(messages[1].metadata?.custom).toMatchObject({ runId: 1, isLastRunMessage: false });
    expect(messages[2].metadata?.custom).toMatchObject({ runId: 1, isLastRunMessage: true });
    expect(messages[4].metadata?.custom).toMatchObject({ runId: 2, isLastRunMessage: true });
  });
});
