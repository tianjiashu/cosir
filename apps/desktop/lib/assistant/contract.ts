/**
 * 后端 Assistant Transport 协议约定的 state 结构（传输契约类型）。
 *
 * 契约是 Task 视角：对话是一条扁平消息列表，Run 只作为消息上的 `runId`（诊断、日志、fork 定位）
 * 与 `activity`（最新 Run 摘要）出现，对话界面不按 runId 比对。后端先推 full 帧再按路径增量
 * 推送 mutation（`messages` / `activity` / `queued_messages` / `error`）。字段与后端
 * `apps/backend/app/assistant_transport/state/` 严格对齐，改动需前后端同步。
 *
 * 本文件为纯类型契约，不依赖 React，归位于 `lib/assistant/`，便于无 React 依赖的模块复用。
 */

/** 后端工具生命周期五态；非法输入由 Transport 边界拒绝。 */
export type TransportToolStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

/** 工具的通用展示数据；具体字段由 `kind` 决定，前端按 kind 选视图。 */
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
  sourceSequence?: number;
  finalReply?: boolean;
  /** 运行中为 "running"，结束为 "completed"。未知值保留给 converter 处理。 */
  status?: string;
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
  presentation: "activity" | "standalone" | "quiet";
  /**
   * 解析后的参数对象。参数不流式：排队（pending）时为空对象，进入 running 后才一次给全。
   * 后端不下发图标、动词或标题，前端按 `toolName` + `args` 查 `tool-catalog.ts`。
   */
  args?: Record<string, unknown> | null;
  /** 原始模型声明的目的，不用于判断执行状态。 */
  intent?: string | null;
  /** 工具执行错误文本；成功时为空。 */
  error?: string | null;
  /** 后端工具调用生命周期状态，已由传输入口校验。 */
  status: TransportToolStatus;
  /** 后端在失败时提供的稳定错误标识（可选）。 */
  errorCode?: string;
  /**
   * 是否在等用户决定（HITL 审批、交接）。后端在投影工具结果时一处计算（占位行 `pending` 且不是后台
   * 占位）、每次显式写；前端据此把调用提进「等待用户」区，不从 `status === "pending"` 推断——
   * pending 同时也表示「排队待执行」。
   */
  awaitingUser: boolean;
  /** 后端治理后的 UI 展示数据，前端不得从 args 推导展示内容。 */
  display_data?: TransportToolDisplayData | null;
  /** 持久化 TeamRun 的定位字段，工具失败后仍可查询流水线。 */
  team_run_id?: number;
  /**
   * 后台工作的占位（结论稍后以 `background_result` 回投）上的工作记录。投影自占位行 metadata，只在是后台
   * 占位时出现，结算后保留；卡片只看它在不在（所有工具共用的「在后台」事实），后端看板从这些记录投影。
   * 卡片状态仍看 `status`。
   */
  background?: TransportBackgroundWork;
  /** 后端显式标记的错误结果。 */
  isError?: boolean | null;
  /** 委派 / 子 Agent 调用指向的子 Task；委派行据此读任务状态推送、点击打开子任务，不渲染为文本。 */
  child_task_id?: number;
  /** Current AgentProfile.role resolved by the backend. */
  agent_role?: string;
  /** execute_terminal 流式输出的去重序号，后端投影内部使用；前端只接受、不消费。 */
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
/** 回合途中到达的 Agent 消息：当前回复过程里的一段，不开新回合。开启回合的 Agent 消息见 `TransportMessage.source`。 */
export type TransportAgentMessagePart = {
  type: "agent-message";
  id: string;
  text: string;
  source: TransportMessageSource;
};
/** 单条 Transport 消息的内容 part。 */
export type TransportMessagePart =
  | TransportTextPart
  | TransportToolCallPart
  | TransportImagePart
  | TransportFilePart
  | TransportAgentMessagePart;

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
type TransportError = {
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
  /** 冻结的是用户关掉的工具组名（不是工具名），composer 锁定期直接按组回显。 */
  disabledToolGroups: string[];
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
  /** Run 从创建到终态的耗时（毫秒）；终态必有耗时。运行中的耗时由前端按 `activity.startedAt` 本地计时。 */
  durationMs: number;
};

/** Agent 消息的发送方：`taskId` 用于跳到来源 Task，`name` 是发送时的显示名快照。 */
export type TransportMessageSource = {
  taskId: number;
  name: string;
};

/** 单条 Transport 消息：Task 级扁平消息列表中的 canonical 消息。 */
export type TransportMessage = {
  id: string;
  role: "user" | "assistant";
  parts: TransportMessagePart[];
  /** 所属 Run，只供诊断、日志与 fork 定位；对话界面不据此判断运行状态。 */
  runId: number | null;
  /** 仅「该 Run 最后一条助手消息」在 Run 终态后非空；user 消息恒为 null。 */
  outcome: TransportOutcome | null;
  /**
   * 仅 Agent 消息（另一个 Agent 交来、开启一轮的 user 消息：委派、追加消息、Team 节点输入）非空。
   * 它不是用户说的话：另行渲染、不可编辑，也不认领用户的乐观发送。
   */
  source: TransportMessageSource | null;
};

/**
 * 当前活动：Task 最新 Run 的摘要投影（事实源仍是后端 `ConversationRunModel.status`）。
 * `runId` 只供诊断与日志，对话界面按 `status` 判断「运行中 / 已停下」。
 */
export type TransportActivity = {
  modelActivity: { stepId: string; phase: "waiting" | "reasoning" | "responding" | "tool_call" } | null;
  runId: number;
  status: TransportRunStatus;
  endReason: string | null;
  error: TransportError | null;
  usage: TransportUsage | null;
  /** 创建 Run 时冻结的模型上下文窗口上限。 */
  contextWindowTotal: number | null;
  launch: TransportRunLaunch;
  /** Run 创建时刻（ISO 8601）；运行中回合脚注据此本地计时，终态后以 `outcome.durationMs` 为准。 */
  startedAt: string;
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
 * 排队用户输入的并入时机，由后端决定：`next_run` 是活动 Run 期间发送的排队消息（Run 结束后一轮一条
 * 处理）；`immediate` 是空闲时发送或被「立即发送」的消息，在下个模型边界并入或马上开新 Run。
 */
export type InputDelivery = "immediate" | "next_run";

/** 尚未被模型边界消费的排队用户输入，事实仍在后端 message_queue；尚无所属 Run。 */
export type TransportQueuedMessage = {
  id: string;
  role: "user";
  parts: TransportMessagePart[];
  delivery: InputDelivery;
};

/** 终端会话生命周期：`starting` / `running` 为活跃，其余为已结束。 */
export type TerminalSessionStatus = "starting" | "running" | "exited" | "closed" | "failed";

/**
 * Task 级交互终端会话条目（snapshot 顶层 `terminal_sessions`）。
 *
 * 事实源在后端 `TerminalSessionService`，这里只是投影：活跃会话在前、按创建时间排序，已结束
 * 会话最多保留 10 个。前端标签栏、工具卡片状态都只读它，不自行推断会话存活；后端重启后列表清空。
 * `label` 为 Agent 指定的用途名，未指定时为空串（标签按「终端 N」编号）。
 */
export type TerminalSessionEntry = {
  sessionId: string;
  label: string;
  shellKind: string;
  createdBy: "agent" | "user";
  status: TerminalSessionStatus;
  endReason: string | null;
  exitCode: number | null;
  integration: "starting" | "ready" | "unavailable" | "off";
  /** 集成跟踪到有命令正在执行；无集成（integration 非 ready）时恒为 false，不代表空闲。 */
  commandRunning: boolean;
  createdAt: string;
};

/** 工具 part 上的后台工作记录（后端 `BackgroundWork`）。 */
export type TransportBackgroundWork = {
  kind: "terminal_command" | "child_agent" | "agent_team";
  label: string;
  startedAt: string;
  /** 结论到达、被撤销或被重启中断的时刻；还在等结论时为 null。 */
  endedAt: string | null;
};

/**
 * Task 后台工作看板条目（snapshot 顶层 `background_tasks`）。
 *
 * 后端从工作副本里带后台记录的工具 part 与登记表活条目投影：只含后台工作（一开始就在后台或已转到后台），
 * 进行中的在前（按开始先后），之后是最近结束的若干条；重启后保留历史。前台等待见 `foreground_waits`。
 * `settling`：工作已结束、对话里的占位还没落定（结论待并入或撤销待结算），马上会变成完成或取消。
 * 工具卡片的状态不读它。
 */
export type BackgroundTaskEntry = {
  toolCallId: string;
  kind: TransportBackgroundWork["kind"];
  label: string;
  status: "running" | "settling" | "completed" | "cancelled";
  startedAt: string;
  /** 进行中（运行、收尾）时为 null。 */
  endedAt: string | null;
};

/**
 * 一次进行中的交接（snapshot 顶层 `handoffs`）：Agent 把一个资源交给用户、等用户交还。
 *
 * 事实源是后端进程内的交接登记表，按登记先后排列，同一资源至多一条；后端重启后清空（工具卡片上的交还入口
 * 仍在）。`surface` + `resourceId` 指向交出去的资源（终端会话 id、浏览器标签 id），「交还」即对 `toolCallId`
 * 作出 HITL 决定。
 */
export type HandoffEntry = {
  toolCallId: string;
  toolName: string;
  surface: "terminal" | "browser";
  resourceId: string;
  reason: string;
};

/** Task 级 Transport state。 */
/** 更早历史的一页：完整历史中 `[messageOffset, messageOffset + messages.length)` 这一段。 */
export type TransportHistoryPage = {
  messages: TransportMessage[];
  messageOffset: number;
};

export type TransportState = {
  fork?: { sourceTaskId: number; sourceTitle: string; taskType: "user_fork" | "agent_fork"; initialLaunch: TransportRunLaunch };
  /** Task 标题，投影自后端 `tasks.title`；变化时由宿主刷新侧栏目录。 */
  title: string;
  /** 扁平消息列表，按 context 序号排列；只含已加载的一段（最近窗口加上往上翻已取到的页）。 */
  messages: TransportMessage[];
  /**
   * `messages` 之前还有多少条更早的消息没加载。后端只发最近一段，往上翻时按页取
   * （`/assistant/messages?end=`）；增量路径里的消息下标是完整历史里的位置，减去它才是本地位置。
   */
  messageOffset: number;
  /** 最新 Run 的摘要；Task 尚无 Run 时为 null。 */
  activity: TransportActivity | null;
  queued_messages?: TransportQueuedMessage[];
  /** 本 Task 的交互终端会话列表（必填，可为空数组）。 */
  terminal_sessions: TerminalSessionEntry[];
  /** 本 Task 的后台工作看板条目（必填，可为空数组）。 */
  background_tasks: BackgroundTaskEntry[];
  /** 正在前台等待、可转到后台的工具调用 id（必填，可为空数组）；只服务工具卡片的「转到后台」。 */
  foreground_waits: string[];
  /** 进行中的交接（必填，可为空数组）；面板的交接提示与自动弹出读它。 */
  handoffs: HandoffEntry[];
};
