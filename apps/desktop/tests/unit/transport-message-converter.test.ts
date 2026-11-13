import { describe, expect, it } from "vitest";

import {
  extractUserAddMessageAttachments,
  toEditableUserMessageDraft,
  toThreadMessageWithRenderContext,
  toPendingUserMessage,
  toToolCallPart,
  type TransportMessageRenderContext,
} from "@/lib/assistant/transport-message-converter";
import type { TransportMessage, TransportToolCallPart } from "@/lib/assistant/contract";
import { activity, agentMessagePart, outcome, usage } from "./transport-fixtures";
import { readToolArtifact } from "@/lib/assistant/tools/tool-artifact";

/** 已到终态那一轮回复末尾的渲染上下文。 */
const completedTurn = (turn: number): TransportMessageRenderContext => ({
  turn, inherited: false, source: null, outcome: outcome("completed"), activity: null,
});
const runningTurn = (turn: number): TransportMessageRenderContext => ({
  turn, inherited: false, source: null, outcome: null, activity: activity(turn, "running"),
});
const toThreadMessage = (
  message: Pick<TransportMessage, "id" | "role" | "parts">,
  context: TransportMessageRenderContext,
) => toThreadMessageWithRenderContext(message, context);

const tool = (status: TransportToolCallPart["status"], extra: Partial<TransportToolCallPart> = {}): TransportToolCallPart => ({presentation: "activity",
  type: "tool-call",
  toolCallId: `tool-${status}`,
  toolName: "execute_terminal",
  status,
  args: { command: "npm test" },
  awaitingUser: false,
  ...extra,
});

describe("assistant transport converter", () => {
  it("正文追加复用未变工具的 part/artifact；工具状态或展示数据改变时创建新版本", () => {
    const source = { ...tool("running"), intent: "核对工具结果" };
    const context = runningTurn(1);
    const before = toThreadMessage({ id: "a", role: "assistant", parts: [source, { type: "text", text: "开始" }] }, context);
    const after = toThreadMessage({ id: "a", role: "assistant", parts: [source, { type: "text", text: "开始继续" }] }, context);
    expect(after.content[0]).toBe(before.content[0]);
    const first = before.content[0];
    if (first.type !== "tool-call") throw new Error("expected tool call");
    expect(readToolArtifact(first.artifact)).toBe(first.artifact);
    const settled = toToolCallPart({ ...source, status: "completed", display_data: { kind: "terminal-result", output: "done" } });
    expect(settled).not.toBe(first);
    if (settled.type !== "tool-call") throw new Error("expected tool call");
    expect(readToolArtifact(settled.artifact)).toMatchObject({ backendStatus: "completed", display_data: { output: "done" } });
    expect(readToolArtifact(first.artifact).backendStatus).toBe("running");
    expect(readToolArtifact(first.artifact).intent).toBe("核对工具结果");
    expect(readToolArtifact(settled.artifact).intent).toBe("核对工具结果");
  });
  it("乐观用户消息复用渲染上下文，正文胶囊与图片原样投影", () => {
    const command = { type: "add-message", message: { role: "user", parts: [
      { type: "text", text: '查看 <file path="/tmp/notes.md" /> 和 ' },
      { type: "image", image: "cosir-attachment://image-one" },
    ] } };
    const pending = toPendingUserMessage(command);
    expect(pending).toMatchObject({
      role: "user", attachments: [],
      content: [
        { type: "text", text: '查看 <file path="/tmp/notes.md" /> 和 ' },
        { type: "image", image: "cosir-attachment://image-one" },
      ],
    });
    expect(pending?.metadata.custom).toEqual({ source: null, pending: true });
    expect(toPendingUserMessage(command)?.id).toBe(pending?.id);
  });

  // 目的：结局与当前活动随消息一并投影，使消息渲染只依赖消息自身事实。潜在缺陷：消息组件回查
  // thread external state 时，只读作用域（无 external state 的 runtime）会在渲染期抛错。
  it("途中来信在 content 里占位，回合才能按 canonical 下标读后面的 part", () => {
    const parts = [agentMessagePart("5"), { type: "text" as const, text: "结论", status: "completed" as const }];
    const message = toThreadMessageWithRenderContext(
      { id: "assistant-1", role: "assistant", parts },
      { turn: 1, inherited: false, source: null, outcome: null, activity: null },
    );
    expect(message.content.map((part) => part.type)).toEqual(["data", "text"]);
  });

  it("Agent 消息的发送方随 user 消息的 custom 交给渲染层", () => {
    const message: TransportMessage = {
      id: "11", role: "user", parts: [{ type: "text", text: "做这个", status: "completed" }], turn: 3, inherited: false, outcome: null,
      source: { taskId: 4, teamRunId: null, name: "主任务", report: null },
    };
    const converted = toThreadMessage(message, { turn: 3, inherited: false, source: message.source, outcome: null, activity: null });
    expect(converted.metadata.custom).toEqual({ source: { taskId: 4, teamRunId: null, name: "主任务", report: null } });
  });

  it("projects the outcome and activity onto assistant message metadata (D5)", () => {
    const message: TransportMessage = { id: "assistant-turn-7", role: "assistant", parts: [], turn: 7, inherited: false, source: null, outcome: null };
    const cancelled = outcome("cancelled", { usage: usage({ total_tokens: 42 }) });
    const ended = toThreadMessage(message, { turn: 7, inherited: false, source: null, outcome: cancelled, activity: null });
    expect(ended.metadata.custom).toMatchObject({ turn: 7, inherited: false, outcome: cancelled, activity: null });
    expect(ended.status).toEqual({ type: "incomplete", reason: "cancelled" });

    const live = toThreadMessage(message, runningTurn(7));
    expect(live.metadata.custom).toMatchObject({ turn: 7, inherited: false, outcome: null, activity: { status: "running" } });
    expect(live.status).toEqual({ type: "running" });
  });

  it("rebuilds only images for failure restore；正文胶囊随文本恢复", () => {
    const locator = `cosir-attachment://${"a".repeat(64)}`;
    const attachments = extractUserAddMessageAttachments({
      type: "add-message",
      message: {
        role: "user",
        parts: [
          { type: "text", text: '请查看 <file path="/tmp/a.md" />' },
          { type: "image", image: locator },
        ],
      },
    });
    expect(attachments).toMatchObject([{
      id: locator,
      type: "image",
      content: [{ type: "image", image: locator }],
    }]);
  });

  it("keeps text and image parts in canonical order", () => {
    const imageLocator = `cosir-attachment://${"a".repeat(64)}`;
    const converted = toThreadMessage(
      {
        id: "user-ordered",
        role: "user",
        parts: [
          { type: "text", text: '请看 <file path="/tmp/notes.md" />', status: "completed" },
          { type: "image", image: imageLocator },
        ],
      },
      completedTurn(1),
    );

    expect(converted.content.map((part) => part.type)).toEqual(["text", "image"]);
  });

  it("builds one edit draft from canonical text and images, keeping capsule tags in place", () => {
    const imageLocator = `cosir-attachment://${"b".repeat(64)}`;
    const converted = toThreadMessage(
      {
        id: "user-edit-order",
        role: "user",
        parts: [
          { type: "text", text: '前文<file path="/tmp/a.md" />中间', status: "completed" },
          { type: "image", image: imageLocator },
          { type: "text", text: '后文 <ref path="src/b.py" lines="3" />', status: "completed" },
        ],
      },
      completedTurn(1),
    );

    if (converted.role !== "user") throw new Error("expected user message");
    const draft = toEditableUserMessageDraft(converted);

    expect(draft.text).toBe('前文<file path="/tmp/a.md" />中间后文 <ref path="src/b.py" lines="3" />');
    expect(draft.attachments.map((attachment) => attachment.id)).toEqual([imageLocator]);
    expect(draft.document.images.map((attachment) => attachment.id)).toEqual([imageLocator]);
  });

  it("deduplicates edit image attachments even when the message model contains both sources", () => {
    const imageLocator = `cosir-attachment://${"c".repeat(64)}`;
    const converted = toThreadMessage(
      {
        id: "user-edit-dedup",
        role: "user",
        parts: [{ type: "image", image: imageLocator }],
      },
      completedTurn(1),
    );

    if (converted.role !== "user") throw new Error("expected user message");
    const draft = toEditableUserMessageDraft({
      ...converted,
      attachments: [...converted.attachments, ...converted.attachments],
    });

    expect(draft.attachments).toHaveLength(1);
    expect(draft.attachments[0]?.id).toBe(imageLocator);
  });

  it("maps streaming text and reasoning without losing their running status", () => {
    const message: TransportMessage = {
      id: "assistant-1",
      role: "assistant",
      parts: [
        { type: "text", text: "partial **markdown", status: "running" },

      ],
      turn: 1,
      inherited: false,
      outcome: null,
      source: null,
    };
    const converted = toThreadMessage(message, runningTurn(1));
    expect(converted.content[0]).toMatchObject({ type: "text", text: "partial **markdown", status: { type: "running" } });

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

    expect(pending).toMatchObject({ type: "tool-call", argsText: '{"command":"npm test"}', artifact: {presentation: "activity",  backendStatus: "pending" } });
    expect(running).toMatchObject({ artifact: {presentation: "activity",  backendStatus: "running" } });
    expect(completed).toMatchObject({ isError: false, artifact: {presentation: "activity",  backendStatus: "completed", display_data: { kind: "terminal-result" } } });
    expect(failed).toMatchObject({ isError: true, artifact: {presentation: "activity",  backendStatus: "failed", errorCode: "DENIED" } });
    expect(failed).toMatchObject({ artifact: { error: "权限不足" } });
    expect(settled).toMatchObject({ artifact: { error: "执行异常" } });
    expect(failed).toMatchObject({ result: { kind: "tool-terminal", status: "failed" } });
    expect(cancelled).toMatchObject({ isError: false, artifact: {presentation: "activity",  backendStatus: "cancelled", error: "已取消" } });
    expect(cancelled).toMatchObject({ result: { kind: "tool-terminal", status: "cancelled" } });
    expect(delegation).toMatchObject({ artifact: { childTaskId: 22 } });
  });

  // 目的：等待用户与展示数据原样进 artifact，调用行只读这里；参数留在 assistant-ui 的 args 上，标题由前端目录算。
  it("把等待用户与展示数据原样带进 artifact，参数留在 args", () => {
    const awaiting = toToolCallPart(tool("pending", {
      toolName: "browser_handoff",
      args: { reason: "交给你登录" },
      awaitingUser: true,
    }));
    expect(awaiting).toMatchObject({
      args: { reason: "交给你登录" },
      artifact: {presentation: "activity",  backendStatus: "pending", awaitingUser: true },
    });
    expect(awaiting).not.toHaveProperty("result");

    const completed = toToolCallPart(tool("completed", {
      toolName: "web_extract",
      display_data: { kind: "web-extract-urls", urls: [{ url: "https://example.com" }] },
    }));
    expect(completed).not.toHaveProperty("result");
    expect(completed).toMatchObject({
      artifact: {
        awaitingUser: false,
        display_data: { kind: "web-extract-urls", urls: [{ url: "https://example.com" }] },
      },
    });
  });


});
