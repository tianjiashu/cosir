import { describe, expect, it } from "vitest";

import {
  extractUserAddMessageAttachments,
  toMessageStatus,
  toThreadMessage,
  toToolCallPart,
  toTransportThreadView,
} from "@/lib/assistant/converter";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import type { TransportMessage, TransportState, TransportToolCallPart } from "@/lib/assistant/contract";

const emptyState = (): TransportState => ({
  runs: [],
  current_run_id: null,
  approvals: {},
  context_usage_ratio: null,
  context_usage_used: null,
  context_window_total: null,
  error: null,
});

const completedRun = (runId: number, messages: TransportMessage[] = []): TransportState["runs"][number] => ({
  runId,
  status: "completed",
  endReason: null,
  messages,
  usage: null,
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
  it("rebuilds ordinary attachments from inline token ids for failure restore", () => {
    const file = registerLocalAttachment(new File([], "设计 说明.md", { type: "text/markdown" }), {
      id: "local-file-restore",
      path: "C:\\workspace\\设计 说明.md",
      name: "设计 说明.md",
      contentType: "text/markdown",
      kind: "file",
    });
    const attachments = extractUserAddMessageAttachments({
      type: "add-message",
      message: {
        role: "user",
        parts: [{ type: "text", text: "请查看 [[cosir-file:local-file-restore]]" }],
      },
    });
    expect(file.name).toBe("设计 说明.md");
    expect(attachments).toMatchObject([{
      id: "local-file-restore",
      type: "file",
      content: [{ data: "cosir-local-file:local-file-restore" }],
    }]);
  });

  it("keeps the local registry ID when restoring a sent file part", () => {
    const attachments = extractUserAddMessageAttachments({
      type: "add-message",
      message: {
        role: "user",
        parts: [{
          type: "file",
          data: "cosir-local-file:local-file-restore",
          filename: "设计 说明.md",
          mimeType: "text/markdown",
        }],
      },
    });

    expect(attachments).toMatchObject([{ id: "local-file-restore" }]);
  });

  it("does not use a file path or remote locator as an attachment ID", () => {
    const attachments = extractUserAddMessageAttachments({
      type: "add-message",
      message: {
        role: "user",
        parts: [{
          type: "file",
          data: "C:\\workspace\\leftHook.yml",
          filename: "leftHook.yml",
          mimeType: "text/yaml",
        }],
      },
    });

    expect(attachments).toEqual([]);
  });

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
      parts: [
        { type: "text", text: "partial **markdown", status: "running" },
        { type: "reasoning", text: "checking", status: "completed" },
      ],
    };
    const converted = toThreadMessage(message, { ...completedRun(1), status: "running" });
    expect(converted.content[0]).toMatchObject({ type: "text", text: "partial **markdown", status: { type: "running" } });
    expect(converted.content[1]).toMatchObject({ type: "reasoning", text: "checking", status: { type: "complete" } });
    expect(converted.status).toEqual({ type: "running" });
  });

  it("maps all backend tool lifecycle states and preserves artifact data", () => {
    const pending = toToolCallPart(tool("pending"));
    const running = toToolCallPart(tool("running"));
    const completed = toToolCallPart(tool("completed", { display_data: { kind: "terminal-result" } }));
    const failed = toToolCallPart(tool("failed", { error: "full diagnostic", display_data: { status_hint: "权限不足" }, errorCode: "DENIED" }));
    const cancelled = toToolCallPart(tool("cancelled"));

    expect(pending).toMatchObject({ type: "tool-call", argsText: '{\n  "command": "npm test"\n}', artifact: { backendStatus: "pending" } });
    expect(running).toMatchObject({ artifact: { backendStatus: "running" } });
    expect(completed).toMatchObject({ isError: false, artifact: { backendStatus: "completed", display_data: { kind: "terminal-result" } } });
    expect(failed).toMatchObject({ isError: true, artifact: { backendStatus: "failed", errorCode: "DENIED" } });
    expect(failed).toMatchObject({ artifact: { error: "权限不足" } });
    expect(failed).toMatchObject({ result: { kind: "tool-terminal", status: "failed" } });
    expect(cancelled).toMatchObject({ isError: false, artifact: { backendStatus: "cancelled", error: "已取消" } });
    expect(cancelled).toMatchObject({ result: { kind: "tool-terminal", status: "cancelled" } });
  });

  it("让后端终态覆盖 transport 的 sending 标记", () => {
    const state = emptyState();
    state.runs = [
      { ...completedRun(1), status: "failed", messages: [] },
      { ...completedRun(2), status: "cancelled", messages: [] },
    ];

    state.current_run_id = 1;
    expect(toTransportThreadView(state, { pendingCommands: [], isSending: true }).isRunning).toBe(false);

    state.current_run_id = 2;
    expect(toTransportThreadView(state, { pendingCommands: [], isSending: true }).isRunning).toBe(false);
  });

  it("没有当前 Run 时仍保留新命令的 sending 状态", () => {
    const state = emptyState();
    const pendingCommand = { type: "add-message" };

    expect(toTransportThreadView(state, { pendingCommands: [pendingCommand], isSending: false }).isRunning).toBe(true);
  });

  it("hides a tool result when the generic presentation disables it", () => {
    const converted = toToolCallPart(tool("completed", {
      toolName: "web_extract",
      presentation: { expand_layout: "list", show_result: false },
      display_data: { kind: "web-extract-urls", urls: [{ url: "https://example.com" }] },
    }));

    expect(converted).not.toHaveProperty("result");
    expect(converted).toMatchObject({
      artifact: {
        display_data: { kind: "web-extract-urls", urls: [{ url: "https://example.com" }] },
      },
    });
    expect(JSON.stringify(converted)).not.toContain("private document body");
  });

  it("does not treat unknown message status as success", () => {
    expect(toMessageStatus({ id: "m", role: "assistant", parts: [] }, { ...completedRun(1), status: "future-status" })).toEqual({ type: "incomplete", reason: "other" });
  });

  it("keeps unknown tool and run states explicitly non-success", () => {
    const unknownTool = toToolCallPart(tool("future-status" as TransportToolCallPart["status"]));
    expect(unknownTool).toMatchObject({
      isError: true,
      result: { kind: "tool-unknown" },
      artifact: { backendStatus: "unknown" },
    });
    const state = emptyState();
    state.runs = [{ ...completedRun(1), status: "future-run-status" }];
    state.current_run_id = 1;
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
    state.runs = [
      completedRun(1, [
        { id: "u1", role: "user", parts: [] },
        { id: "a1", role: "assistant", parts: [] },
        { id: "a1-tool-followup", role: "assistant", parts: [] },
      ]),
      completedRun(2, [{ id: "u2", role: "user", parts: [] }, { id: "a2", role: "assistant", parts: [] }]),
    ];

    const messages = toTransportThreadView(state, { pendingCommands: [], isSending: false }).messages;
    expect(messages[1].metadata?.custom).toMatchObject({ runId: 1, isLastRunMessage: false });
    expect(messages[2].metadata?.custom).toMatchObject({ runId: 1, isLastRunMessage: true });
    expect(messages[4].metadata?.custom).toMatchObject({ runId: 2, isLastRunMessage: true });
  });
});
