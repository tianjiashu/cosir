/**
 * 后端 Assistant Transport 协议约定的 state 结构（传输契约类型）。
 *
 * 后端以「增量构建（标准流式）」方式同步此 state：
 * 1. 先通过 `set` 一次性设置完整的 `runs` 数组；
 * 2. 随后在对应 Run 的消息 part 上增量填充内容。
 *
 * `content` 是消息的 part 数组，支持文本与工具调用两种 part。该类型必须与
 * 后端协议层（apps/backend）产出的 wire 帧严格对齐，改动需前后端同步。
 *
 * 本文件为纯类型契约，不依赖 React，归位于 `lib/assistant/`，便于无 React
 * 依赖的模块（如 converter）直接复用。
 */

import type { MessageStatus } from "@assistant-ui/core";

export type KnownTransportToolStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** 后端五态之外的值只用于安全显示，不得被解释为 pending 或 completed。 */
export type TransportToolStatus = KnownTransportToolStatus | "unknown";

export type TerminalSessionPresentationVariant =
  | "terminal-session-start"
  | "terminal-session-read"
  | "terminal-session-write"
  | "terminal-session-signal"
  | "terminal-session-close";

export type TransportToolPresentation = {
  verb?: string;
  icon?: string;
  /** Stable semantic renderer variant; never contains dynamic tool output. */
  variant?: TerminalSessionPresentationVariant;
  surface?: "trace" | "standalone";
  expandable?: boolean;
  expand_layout?: "none" | "details" | "list" | "diff" | "write" | "terminal";
  default_open?: boolean;
  show_result?: boolean;
  [key: string]: unknown;
};

/** 工具的通用展示数据；具体字段由 presentation.expand_layout 解释。 */
export type GenericTransportToolData = {
  kind?: string;
  [key: string]: unknown;
};

export type TransportToolDisplayData = GenericTransportToolData;

/**
 * 文本 part：Transport 协议中的纯文本片段。
 *
 * 字段必须与后端 snapshot 投影出的 text part
 * 严格对齐：`text` 为正文，`status` 为 part 级运行态（`running` / `completed`）。
 */
export type TransportTextPart = {
  type: "text";
  text: string;
  /** 运行中为 "running"，结束为 "completed"。未知值保留给 converter 处理。 */
  status?: string;
};

/**
 * 推理 part：Transport 协议中的模型思考过程片段。
 *
 * 后端经 conversation event projector 把推理文本写入 snapshot，再由 snapshot
 * 投影为 `{ type: "reasoning", status, text }`，与
 * assistant-ui 的 `ReasoningMessagePart` 字段同构（`unstable_summary` 可选，后端
 * 当前不产，故前端契约亦标可选）。
 */
export type TransportReasoningPart = {
  type: "reasoning";
  /** 模型推理文本。 */
  text: string;
  /** 推理通道运行中为 "running"，结束为 "completed"。未知值保留给 converter 处理。 */
  status?: string;
  /** 推理摘要（后端当前不产，保留以对齐 assistant-ui 契约）。 */
  unstable_summary?: string;
};

/**
 * 工具调用 part：Transport 协议中的工具调用片段。
 *
 * 字段对齐后端 snapshot tool-call part：`toolCallId` / `toolName` /
 * `status` / `args`（解析后的参数对象）/ `error`（失败时的错误文本，成功或未完成时为
 * undefined）/ `display_data`（展示数据）。后端不产 `argsText` 原始 JSON 流，故本
 * 契约以 `args` 为权威参数通道；converter 在需要时从 `args` 派生 `argsText` 以贴合
 * assistant-ui 的 `ToolCallMessagePart`。
 */
export type TransportToolCallPart = {
  type: "tool-call";
  toolCallId: string;
  toolName: string;
  /** 解析后的参数对象（可能因流式未完成而不完整）。 */
  args: Record<string, unknown>;
  /** 工具执行错误文本；成功时为空。 */
  error?: string | null;
  /** 后端工具调用生命周期状态；未知 wire 值由 converter 显式标为 unknown。 */
  status?: string;
  /** 后端在失败时提供的稳定错误标识（可选）。 */
  errorCode?: string;
  /** 后端声明的工具展示布局；它只影响 renderer，不改变工具生命周期。 */
  presentation?: TransportToolPresentation;
  /** 后端治理后的 UI 展示数据，前端不得从 args 推导展示内容。 */
  display_data?: TransportToolDisplayData | null;
  /** 后端显式标记的错误结果。 */
  isError?: boolean | null;
  /** Runtime-only child task locator for Workbench; never rendered as text. */
  child_task_id?: number;
  /** Runtime-only child Run locator for cancellation; never rendered as text. */
  child_run_id?: number;
  /** Current AgentProfile.role resolved by the backend. */
  agent_role?: string;
  delegation_ref_seq?: number;
  terminal_output_seq?: number;
};

/** 用户图片 part：只携带后端生成的稳定 locator。 */
export type TransportImagePart = {
  type: "image";
  image: string;
};

/** 用户普通文件 part：只携带稳定 locator 与展示元数据。 */
export type TransportFilePart = {
  type: "file";
  file: string;
  name: string;
  contentType: string;
};
/** 单条 Transport 消息：Run 内的 canonical conversation 消息。 */
export type TransportMessage = {
  id: string;
  role: "user" | "assistant";
  parts: Array<TransportTextPart | TransportReasoningPart | TransportToolCallPart | TransportImagePart | TransportFilePart>;
};

export type ConversationStateUsage = {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number | null;
  reasoning_tokens: number;
};

/** 一个 Run 的完整 Transport 事实，包括其消息、生命周期和 token usage。 */
export type TransportRun = {
  runId: number;
  status: string;
  endReason: string | null;
  messages: TransportMessage[];
  usage: ConversationStateUsage | null;
  /**
   * Run 进入失败/取消终态时后端给出的受控错误；运行中与正常结束为 null。
   *
   * `message` 是后端受控的 provider 无关文案，前端直接展示，不得自行拼接 provider 报文。
   */
  error: TransportError | null;
};

/**
 * 运行错误：与后端 `ConversationStateError` 对齐的稳定结构。
 *
 * 只有稳定 `code` 与面向用户的安全 `message`；`retryable` 属于工具观察（仅面向模型），
 * 不在 Transport 错误契约内。
 */
export type TransportError = {
  code: string;
  message: string;
};

/** Task 级 Transport state；Run 事实全部按 Run 保存在 `runs` 中。 */
export type TransportState = {
  runs: TransportRun[];
  current_run_id: number | null;
  /** 审批预留；当前固定为空对象。 */
  approvals: Record<string, never>;
  /** 当前 Task 上下文窗口占用比例；允许大于 1 表示超额。 */
  context_usage_ratio: number | null;
  /** 当前有效上下文已用 token；null 表示尚未完成有效测量。 */
  context_usage_used: number | null;
  /** 当前有效上下文窗口上限；null 表示后端暂时无法确定。 */
  context_window_total: number | null;
  /** 运行期错误；无错误时为 null。 */
  error: TransportError | null;
};

/** 把后端领域 status 翻译为 assistant-ui MessageStatus 的映射函数签名。 */
export type MessageStatusMapper = (
  message: TransportMessage,
  run: TransportRun,
) => MessageStatus;
