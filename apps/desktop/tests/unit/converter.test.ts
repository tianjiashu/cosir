import { describe, expect, it } from "vitest";

import {
  extractUserAddMessageAttachments,
  toMessageStatus,
  toEditableUserMessageDraft,
  toThreadMessage,
  toToolCallPart,
} from "@/lib/assistant/converter";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import type { TransportMessage, TransportState, TransportToolCallPart } from "@/lib/assistant/contract";

const completedRun = (runId: number, messages: TransportMessage[] = []): TransportState["runs"][number] => ({
  runId,
  status: "completed",
  endReason: null,
  messages,
  usage: null,
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

  it("keeps canonical attachments in ordered content instead of a second top-level source", () => {
    const converted = toThreadMessage(
      {
        id: "user-1",
        role: "user",
        parts: [{
          type: "file",
          file: "cosir-local-file:file-1",
          name: "notes.md",
          contentType: "text/markdown",
        }],
      },
      completedRun(1),
    );

    expect(converted).toMatchObject({
      role: "user",
      attachments: [],
      content: [{
        type: "file",
        data: "cosir-local-file:file-1",
        filename: "notes.md",
      }],
    });
  });

  it("keeps ordinary files as ordered message parts while hiding internal tokens", () => {
    const converted = toThreadMessage(
      {
        id: "user-2",
        role: "user",
        parts: [
          { type: "text", text: "请查看 [[cosir-file:file-1]]" },
          {
            type: "file",
            file: "cosir-local-file:file-1",
            name: "notes.md",
            contentType: "text/markdown",
          },
        ],
      },
      completedRun(1),
    );

    expect(converted.content).toMatchObject([
      { type: "text", text: "请查看 <!-- [[cosir-file:file-1]] -->" },
      {
        type: "file",
        data: "cosir-local-file:file-1",
        filename: "notes.md",
        mimeType: "text/markdown",
        sourceType: "id",
      },
    ]);
  });

  it("keeps text, file, and image parts in canonical order", () => {
    const converted = toThreadMessage(
      {
        id: "user-ordered",
        role: "user",
        parts: [
          { type: "text", text: "请看", status: "completed" },
          { type: "file", file: "cosir-local-file:file-1", name: "notes.md", contentType: "text/markdown" },
          { type: "text", text: "和图片", status: "completed" },
          { type: "image", image: "cosir-attachment://" + "a".repeat(64) },
        ],
      },
      completedRun(1),
    );

    expect(converted.content.map((part) => part.type)).toEqual(["text", "file", "text", "image"]);
  });

  it("builds one edit attachment list and preserves file token positions", () => {
    const converted = toThreadMessage(
      {
        id: "user-edit-order",
        role: "user",
        parts: [
          { type: "text", text: "前文", status: "completed" },
          { type: "file", file: "cosir-local-file:file-a", name: "a.md", contentType: "text/markdown" },
          { type: "text", text: "中间", status: "completed" },
          { type: "file", file: "cosir-local-file:file-b", name: "b.md", contentType: "text/markdown" },
          { type: "text", text: "后文", status: "completed" },
        ],
      },
      completedRun(1),
    );

    if (converted.role !== "user") throw new Error("expected user message");
    const draft = toEditableUserMessageDraft(converted);

    expect(draft.text).toBe(
      "前文[[cosir-file:file-a]]中间[[cosir-file:file-b]]后文",
    );
    expect(draft.attachments.map((attachment) => attachment.id)).toEqual(["file-a", "file-b"]);
  });

  it("preserves mixed image and file positions in the edit draft", () => {
    const imageLocator = `cosir-attachment://${"a".repeat(64)}`;
    const converted = toThreadMessage(
      {
        id: "user-edit-mixed",
        role: "user",
        parts: [
          { type: "text", text: "文字 A", status: "completed" },
          { type: "image", image: imageLocator },
          { type: "text", text: "文字 B", status: "completed" },
          { type: "file", file: "cosir-local-file:file-a", name: "a.md", contentType: "text/markdown" },
          { type: "text", text: "文字 C", status: "completed" },
        ],
      },
      completedRun(1),
    );

    if (converted.role !== "user") throw new Error("expected user message");
    const draft = toEditableUserMessageDraft(converted);

    expect(draft.text).toBe("文字 A文字 B[[cosir-file:file-a]]文字 C");
    expect(draft.document.inlineFiles.map((attachment) => attachment.id)).toEqual(["file-a"]);
    expect(draft.document.previewImages.map((attachment) => attachment.id)).toEqual([imageLocator]);
  });

  it("deduplicates edit attachments even when the message model contains both sources", () => {
    const converted = toThreadMessage(
      {
        id: "user-edit-dedup",
        role: "user",
        parts: [
          { type: "text", text: "查看", status: "completed" },
          { type: "file", file: "cosir-local-file:file-a", name: "a.md", contentType: "text/markdown" },
        ],
      },
      completedRun(1),
    );

    if (converted.role !== "user") throw new Error("expected user message");
    const draft = toEditableUserMessageDraft({
      ...converted,
      attachments: [...converted.attachments, ...converted.attachments],
    });

    expect(draft.text).toBe("查看[[cosir-file:file-a]]");
    expect(draft.attachments).toHaveLength(1);
    expect(draft.attachments[0]?.id).toBe("file-a");
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
    const delegation = toToolCallPart(tool("running", {
      toolName: "delegate_task",
      child_task_id: 22,
      child_run_id: 220,
    }));

    expect(pending).toMatchObject({ type: "tool-call", argsText: '{\n  "command": "npm test"\n}', artifact: { backendStatus: "pending" } });
    expect(running).toMatchObject({ artifact: { backendStatus: "running" } });
    expect(completed).toMatchObject({ isError: false, artifact: { backendStatus: "completed", display_data: { kind: "terminal-result" } } });
    expect(failed).toMatchObject({ isError: true, artifact: { backendStatus: "failed", errorCode: "DENIED" } });
    expect(failed).toMatchObject({ artifact: { error: "权限不足" } });
    expect(failed).toMatchObject({ result: { kind: "tool-terminal", status: "failed" } });
    expect(cancelled).toMatchObject({ isError: false, artifact: { backendStatus: "cancelled", error: "已取消" } });
    expect(cancelled).toMatchObject({ result: { kind: "tool-terminal", status: "cancelled" } });
    expect(delegation).toMatchObject({ artifact: { child_task_id: 22, child_run_id: 220 } });
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

  it("prefers the backend controlled failure message over the local fallback", () => {
    const failedRun: TransportState["runs"][number] = {
      ...completedRun(1),
      status: "failed",
      error: { code: "model_insufficient_quota", message: "模型服务配额或余额不足，请充值或更换模型" },
    };

    expect(toMessageStatus({ id: "m", role: "assistant", parts: [] }, failedRun)).toEqual({
      type: "incomplete",
      reason: "error",
      error: "模型服务配额或余额不足，请充值或更换模型",
    });
  });

  it("falls back to the local failure message when the run carries no error contract", () => {
    const failedRun: TransportState["runs"][number] = { ...completedRun(1), status: "failed" };

    expect(toMessageStatus({ id: "m", role: "assistant", parts: [] }, failedRun)).toEqual({
      type: "incomplete",
      reason: "error",
      error: "对话运行失败，请检查模型配置或后端状态。",
    });
  });

  it("keeps disconnected failures rendered as cancelled instead of an error", () => {
    const disconnectedRun: TransportState["runs"][number] = {
      ...completedRun(1),
      status: "failed",
      endReason: "client_disconnected",
      error: { code: "client_disconnected", message: "连接已断开，本轮对话被中断，请重新发送" },
    };

    expect(toMessageStatus({ id: "m", role: "assistant", parts: [] }, disconnectedRun)).toEqual({
      type: "incomplete",
      reason: "cancelled",
    });
  });

  it("keeps unknown tool and run states explicitly non-success", () => {
    const unknownTool = toToolCallPart(tool("future-status" as TransportToolCallPart["status"]));
    expect(unknownTool).toMatchObject({
      isError: true,
      result: { kind: "tool-unknown" },
      artifact: { backendStatus: "unknown" },
    });
  });
});
