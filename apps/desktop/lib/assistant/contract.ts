/**
 * 后端 Assistant Transport 协议约定的 state 结构（传输契约类型）。
 *
 * 后端以「增量构建（标准流式）」方式同步此 state：
 * 1. 先通过 `set` 一次性设置完整的 `messages` 数组（含历史 + 本轮
 *    用户消息 + 空的 assistant 占位消息）；
 * 2. 随后用 `append-text` 增量填充 assistant 消息的内容。
 *
 * `content` 是消息的 part 数组，支持文本与工具调用两种 part。该类型必须与
 * 后端协议层（apps/backend）产出的 wire 帧严格对齐，改动需前后端同步。
 *
 * 本文件为纯类型契约，不依赖 React，归位于 `lib/assistant/`，便于无 React
 * 依赖的模块（如 converter）直接复用。
 */

import type { MessageStatus } from "@assistant-ui/core";

/**
 * 文本 part：Transport 协议中的纯文本片段。
 *
 * 字段必须与后端 snapshot 投影出的 text part
 * 严格对齐：`text` 为正文，`status` 为 part 级运行态（`running` / `complete`）。
 */
export type TransportTextPart = {
  type: "text";
  text: string;
  /** 运行中为 "running"，结束为 "complete"。 */
  status?: "running" | "complete";
};

/**
 * 推理 part：Transport 协议中的模型思考过程片段。
 *
 * 后端经 `ConversationMutationWriter.append_assistant_reasoning` 把推理文本写入
 * snapshot，再由 snapshot 投影为 `{ type: "reasoning", status, text }`，与
 * assistant-ui 的 `ReasoningMessagePart` 字段同构（`unstable_summary` 可选，后端
 * 当前不产，故前端契约亦标可选）。
 */
export type TransportReasoningPart = {
  type: "reasoning";
  /** 模型推理文本。 */
  text: string;
  /** 推理通道运行中为 "running"，结束为 "complete"。 */
  status?: "running" | "complete";
  /** 推理摘要（后端当前不产，保留以对齐 assistant-ui 契约）。 */
  unstable_summary?: string;
};

/**
 * 工具调用 part：Transport 协议中的工具调用片段。
 *
 * 字段对齐后端 snapshot tool-call part：`toolCallId` / `toolName` /
 * `status` / `args`（解析后的参数对象）/ `result`（执行结果）/ `error`（失败时的
 * 错误文本，成功或未完成时为 undefined）。后端不产 `argsText` 原始 JSON 流，故本
 * 契约以 `args` 为权威参数通道；converter 在需要时从 `args` 派生 `argsText` 以贴合
 * assistant-ui 的 `ToolCallMessagePart`。
 */
export type TransportToolCallPart = {
  type: "tool-call";
  toolCallId: string;
  toolName: string;
  /** 解析后的参数对象（可能因流式未完成而不完整）。 */
  args: Record<string, unknown>;
  /** 工具执行结果；未完成或失败时为空。 */
  result?: unknown;
  /** 工具执行错误文本；成功时为空。 */
  error?: string;
  /** 后端工具调用生命周期状态。 */
  status?:
    | "pending"
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    ;
  /** 后端在失败时提供的稳定错误标识（可选）。 */
  errorCode?: string;
};

/** 单条 Transport 消息：对应后端 canonical conversation 投影出的一条 UI 消息。 */
export type TransportMessage = {
  id: string;
  role: "user" | "assistant";
  parts: Array<TransportTextPart | TransportReasoningPart | TransportToolCallPart>;
  /** 后端领域状态，由前端 converter 翻译为 assistant-ui 的 MessageStatus。 */
  status?: string;
  /**
   * 中性领域终态原因，取自后端 turns.end_reason（可能为 null）。
   * 仅作事实透传，不含 assistant-ui 语义；converter 据此区分 failed 的不同终止原因。
   */
  endReason?: string | null;
  createdAt?: string;
};

/** 当前运行元信息：供前端在「停止」时取出 turn_id 调用真实取消端点。 */
export type TransportRun = {
  /** 当前运行切片标识，等价于后端 turns.id；首屏无运行时为 null。 */
  runId: number | null;
  /** 运行态领域状态字符串（pending/running/idle 等）。 */
  status: string;
};

/** 运行错误：与后端 `ConversationStateError` 对齐的稳定结构。 */
export type TransportError = {
  code: string;
  message: string;
  retryable: boolean;
};

/** Transport 整体 state：message 数组的容器 + 当前运行元信息。 */
export type TransportState = {
  messages: TransportMessage[];
  /** 运行时元信息；首屏历史场景下 runId 为 null、status 为 idle。 */
  run: TransportRun;
  /** 审批预留；当前固定为空对象。 */
  approvals: Record<string, never>;
  /** 运行期错误；无错误时为 null。 */
  error: TransportError | null;
};

/** 把后端领域 status 翻译为 assistant-ui MessageStatus 的映射函数签名。 */
export type MessageStatusMapper = (
  message: TransportMessage,
) => MessageStatus;
