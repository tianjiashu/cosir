import { describe, expect, it } from "vitest";

import {
  extractUserAddMessageAttachments,
  toEditableUserMessageDraft,
  toThreadMessageWithRenderContext,
  toPendingUserMessage,
  toToolCallPart,
  type TransportMessageRenderContext,
} from "@/lib/assistant/converter";
import { registerInlineFileMetadata, registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import type { TransportMessage, TransportToolCallPart } from "@/lib/assistant/contract";
import { activity, outcome, usage } from "./transport-fixtures";

/** 已到终态那一轮回复末尾的渲染上下文。 */
const completedRun = (runId: number): TransportMessageRenderContext => ({
  runId, outcome: outcome("completed"), isRunEnd: true, activity: null,
});
const runningRun = (runId: number): TransportMessageRenderContext => ({
  runId, outcome: null, isRunEnd: true, activity: activity(runId, "running"),
});
const toThreadMessage = (
  message: Pick<TransportMessage, "id" | "role" | "parts">,
  context: TransportMessageRenderContext,
) => toThreadMessageWithRenderContext(message, context);

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
  it("乐观用户消息复用渲染上下文，保持身份与附件顺序且不伪造 Run 事实", () => {
    const command = { type: "add-message", commandId: "pending-context", message: { role: "user", parts: [
      { type: "text", text: "查看文件和图片" },
      { type: "file", data: "cosir-local-file:one", filename: "notes.md", mimeType: "text/markdown" },
      { type: "image", image: "cosir-attachment://image-one" },
    ] } };
    const pending = toPendingUserMessage(command);
    expect(pending).toMatchObject({
      role: "user", attachments: [],
      content: [
        { type: "text", text: "查看文件和图片" },
        { type: "file", data: "cosir-local-file:one", filename: "notes.md", mimeType: "text/markdown" },
        { type: "image", image: "cosir-attachment://image-one" },
      ],
    });
    expect(pending?.metadata.custom).toEqual({ runId: null });
    expect(toPendingUserMessage(command)?.id).toBe(pending?.id);
  });

  // 目的：结局与当前活动随消息一并投影，使消息渲染只依赖消息自身事实。潜在缺陷：消息组件回查
  // thread external state 时，只读作用域（无 external state 的 runtime）会在渲染期抛错。
  it("projects the outcome and activity onto assistant message metadata (D5)", () => {
    const message: TransportMessage = { id: "assistant-1", role: "assistant", parts: [], runId: 7, outcome: null };
    const cancelled = outcome("cancelled", { usage: usage({ total_tokens: 42 }) });
    const ended = toThreadMessage(message, { runId: 7, outcome: cancelled, isRunEnd: true, activity: null });
    expect(ended.metadata.custom).toEqual({ runId: 7, isRunEnd: true, outcome: cancelled, activity: null });
    expect(ended.status).toEqual({ type: "incomplete", reason: "cancelled" });

    const live = toThreadMessage(message, runningRun(7));
    expect(live.metadata.custom).toMatchObject({ runId: 7, isRunEnd: true, outcome: null, activity: { status: "running" } });
    expect(live.status).toEqual({ type: "running" });
  });

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

  // 目的：乐观消息与后端 canonical 同形，气泡在原位渲染文件、Skill 胶囊，canonical 到达时附件不挪位。
  it("乐观消息把文件 token 换成原位 file part，并物化 Skill 标记", () => {
    const pending = toPendingUserMessage({ type: "add-message", commandId: "pending-tokens", message: { role: "user", parts: [
      { type: "text", text: "请用 [[cosir-skill:review-code]] 看 [[cosir-file:file-1]] 和 `[[cosir-file:file-2]]`" },
      { type: "file", data: "cosir-local-file:file-1", filename: "notes.md", mimeType: "text/markdown" },
      { type: "file", data: "cosir-local-file:file-3", filename: "extra.md", mimeType: "text/markdown" },
    ] } });

    expect(pending?.content).toMatchObject([
      { type: "text", text: "请用 【Skill: review-code】 看 " },
      { type: "file", data: "cosir-local-file:file-1", filename: "notes.md" },
      { type: "text", text: " 和 `[[cosir-file:file-2]]`" },
      { type: "file", data: "cosir-local-file:file-3", filename: "extra.md" },
    ]);
  });

  it("输入框里只有 token 的普通文件从本机登记取名称并占住原位", () => {
    registerInlineFileMetadata({ id: "token-only", name: "plan.md", contentType: "text/markdown" });
    const pending = toPendingUserMessage({ type: "add-message", commandId: "pending-token-only", message: { role: "user", parts: [
      { type: "text", text: "[[cosir-file:token-only]] 和 [[cosir-file:token-only]][[cosir-file:gone]]" },
    ] } });

    expect(pending?.content).toMatchObject([
      { type: "file", data: "cosir-local-file:token-only", filename: "plan.md", mimeType: "text/markdown" },
      { type: "text", text: " 和 " },
      { type: "file", data: "cosir-local-file:gone", filename: "附件" },
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
      runId: 1,
      outcome: null,
    };
    const converted = toThreadMessage(message, runningRun(1));
    expect(converted.content[0]).toMatchObject({ type: "text", text: "partial **markdown", status: { type: "running" } });
    expect(converted.content[1]).toMatchObject({ type: "reasoning", text: "checking", status: { type: "complete" } });
    expect(converted.status).toEqual({ type: "running" });
  });

  it("maps all backend tool lifecycle states and preserves artifact data", () => {
    const pending = toToolCallPart(tool("pending"));
    const running = toToolCallPart(tool("running"));
    const completed = toToolCallPart(tool("completed", { display_data: { kind: "terminal-result" } }));
    const failed = toToolCallPart(tool("failed", { error: "权限不足", display_data: { status_hint: "权限不足" }, errorCode: "DENIED" }));
    // 重启收尾的失败只带后端写入的 part.error，没有 status_hint，前端不得改写。
    const settled = toToolCallPart(tool("failed", { error: "执行异常" }));
    const cancelled = toToolCallPart(tool("cancelled"));
    const delegation = toToolCallPart(tool("running", {
      toolName: "delegate_task",
      child_task_id: 22,
    }));

    expect(pending).toMatchObject({ type: "tool-call", argsText: '{\n  "command": "npm test"\n}', artifact: { backendStatus: "pending" } });
    expect(running).toMatchObject({ artifact: { backendStatus: "running" } });
    expect(completed).toMatchObject({ isError: false, artifact: { backendStatus: "completed", display_data: { kind: "terminal-result" } } });
    expect(failed).toMatchObject({ isError: true, artifact: { backendStatus: "failed", errorCode: "DENIED" } });
    expect(failed).toMatchObject({ artifact: { error: "权限不足" } });
    expect(settled).toMatchObject({ artifact: { error: "执行异常" } });
    expect(failed).toMatchObject({ result: { kind: "tool-terminal", status: "failed" } });
    expect(cancelled).toMatchObject({ isError: false, artifact: { backendStatus: "cancelled", error: "已取消" } });
    expect(cancelled).toMatchObject({ result: { kind: "tool-terminal", status: "cancelled" } });
    expect(delegation).toMatchObject({ artifact: { child_task_id: 22 } });
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

  it("keeps unknown tool and run states explicitly non-success", () => {
    const unknownTool = toToolCallPart(tool("future-status" as TransportToolCallPart["status"]));
    expect(unknownTool).toMatchObject({
      isError: true,
      result: { kind: "tool-unknown" },
      artifact: { backendStatus: "unknown" },
    });
  });
});
