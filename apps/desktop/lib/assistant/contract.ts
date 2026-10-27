/**
 * 后端 Assistant Transport 协议约定的 state 结构（传输契约类型）。
 *
 * 契约是 Task 视角：对话是一条扁平消息列表，Run 只作为消息上的 `runId`（诊断、日志、fork 定位）
 * 与 `activity`（最新 Run 摘要）出现，对话界面不按 runId 比对。后端先推 full 帧再按路径增量
 * 推送 mutation（`messages` / `activity` / `queued_messages` / `error`）。字段与后端
 * `apps/backend/app/assistant_transport/state/` 严格对齐，改动需前后端同步；契约说明见
 * `docs/plan/task-command-fire-and-forget-design.md` 的「wire 契约（实施基准）」。
 *
 * 本文件为纯类型契约，不依赖 React，归位于 `lib/assistant/`，便于无 React 依赖的模块复用。
 */

type KnownTransportToolStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** 后端五态之外的值只用于安全显示，不得被解释为 pending 或 completed。 */
export type TransportToolStatus = KnownTransportToolStatus | "unknown";

type TerminalSessionPresentationVariant =
  | "terminal-session-list"
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
type GenericTransportToolData = {
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
  /** 持久化 TeamRun 的定位字段，工具失败后仍可查询流水线。 */
  team_run_id?: number;
  /** 后端显式标记的错误结果。 */
  isError?: boolean | null;
  /** Runtime-only child task locator for Workbench; never rendered as text. */
  child_task_id?: number;
  /** Current AgentProfile.role resolved by the backend. */
  agent_role?: string;
  terminal_output_seq?: number;
};

/** 用户图片 part：只携带后端生成的稳定 locator。 */
export type TransportImagePart = {
  type: "image";
  image: string;
};

/** 用户普通文件 part：只携带稳定 locator 与展示元数据。 */
type TransportFilePart = {
  type: "file";
  file: string;
  name: string;
  contentType: string;
};
/** 单条 Transport 消息的内容 part。 */
export type TransportMessagePart =
  | TransportTextPart
  | TransportReasoningPart
  | TransportToolCallPart
  | TransportImagePart
  | TransportFilePart;

/** 一次 Run 当前累计的模型 token 用量；字段缺少 provider 明细时为 null。 */
export type TransportUsage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cache_hit_tokens: number | null;
  cache_miss_tokens: number | null;
  reasoning_tokens: number | null;
};

/**
 * 运行错误：与后端 `ConversationStateError` 对齐的稳定结构。
 *
 * 只有稳定 `code` 与 UI 展示 `message`；`retryable` 属于工具观察（仅面向模型），不在
 * Transport 错误契约内。模型 HTTP 失败时 `message` 可能直接来自响应体的 message 字段。
 */
export type TransportError = {
  code: string;
  message: string;
};

/** Run 生命周期状态：与后端 `ConversationRunStatus` 的五个值一一对应。 */
export type TransportRunStatus = "running" | "suspended" | "completed" | "failed" | "cancelled";

/** Run 的终态（不会再产生运行中内容）。 */
export type TransportTerminalRunStatus = Extract<TransportRunStatus, "completed" | "failed" | "cancelled">;

/**
 * Run 冻结的发起配置（运行期锁定）：活动 Run 期间 composer 只读显示这份配置并以它发送。
 * `modelConfigId` 在模型配置被删除后为 null。
 */
export type TransportRunLaunch = {
  modelConfigId: number | null;
  reasoningEffort: string | null;
  banTools: string[];
};

/**
 * 一轮回复的结局：Run 到终态时由后端写在该 Run 最后一条助手消息上，Run 被恢复时清空。
 * 历史回复的「已取消 / 失败 / 用量」与 fork 入口都只读它，不回查 Run。
 */
export type TransportOutcome = {
  status: TransportTerminalRunStatus;
  endReason: string | null;
  error: TransportError | null;
  usage: TransportUsage | null;
  /** 该 Run 冻结的窗口上限，与 `usage` 同属一个 Run：上下文占用的「已用 / 上限」只能成对取。 */
  contextWindowTotal: number | null;
};

/** 单条 Transport 消息：Task 级扁平消息列表中的 canonical 消息。 */
export type TransportMessage = {
  id: string;
  role: "user" | "assistant";
  parts: TransportMessagePart[];
  /** 所属 Run，只供诊断、日志与 fork 定位；对话界面不据此判断运行状态。 */
  runId: number;
  /** 仅「该 Run 最后一条助手消息」在 Run 终态后非空；user 消息恒为 null。 */
  outcome: TransportOutcome | null;
};

/**
 * 当前活动：Task 最新 Run 的摘要投影（事实源仍是后端 `ConversationRunModel.status`）。
 * `runId` 只供诊断与日志，对话界面按 `status` 判断「运行中 / 已停下」。
 */
export type TransportActivity = {
  runId: number;
  status: TransportRunStatus;
  endReason: string | null;
  error: TransportError | null;
  usage: TransportUsage | null;
  /** 创建 Run 时冻结的模型上下文窗口上限。 */
  contextWindowTotal: number | null;
  launch: TransportRunLaunch;
};

const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set(["completed", "failed", "cancelled"]);
const ACTIVE_RUN_STATUSES: ReadonlySet<string> = new Set(["running", "suspended"]);

/** 判断 Run 是否进入后端定义的终态（与后端 `TERMINAL_RUN_STATUSES` 对齐）。 */
export function isTerminalRunStatus(status: string | null | undefined): boolean {
  return status !== null && status !== undefined && TERMINAL_RUN_STATUSES.has(status);
}

/**
 * 判断 Run 是否处于活动态（与后端 `ACTIVE_RUN_STATUSES` 对齐）：运行中与挂起都算，挂起时仍
 * 显示停止按钮、锁定配置，同一个 Run 会被唤醒继续输出。
 */
export function isActiveRunStatus(status: string | null | undefined): boolean {
  return status !== null && status !== undefined && ACTIVE_RUN_STATUSES.has(status);
}

/**
 * 活动 Run 期间发送消息的投递方式：`immediate` 在下个模型边界并入本轮，`next_run` 交给下一个
 * Run（本轮结束后）。无活动 Run 时后端忽略该字段直接新开 Run。
 */
export type InputDelivery = "immediate" | "next_run";

/** 尚未被模型边界消费的排队用户输入，事实仍在后端 inbox；尚无所属 Run。 */
export type TransportQueuedMessage = {
  id: string;
  role: "user";
  parts: TransportMessagePart[];
  delivery: InputDelivery;
};

/** Task 级 Transport state。 */
export type TransportState = {
  /** Task 标题，投影自后端 `tasks.title`；变化时由宿主刷新侧栏目录。 */
  title: string;
  /** 扁平消息列表，按 context 序号排列。 */
  messages: TransportMessage[];
  /** 最新 Run 的摘要；Task 尚无 Run 时为 null。 */
  activity: TransportActivity | null;
  queued_messages?: TransportQueuedMessage[];
};
