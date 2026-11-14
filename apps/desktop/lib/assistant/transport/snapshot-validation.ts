import { IMAGE_LOCATOR } from "@/lib/assistant/attachments/attachment-locator";
import type { TransportHistoryPage, TransportState } from "@/lib/assistant/contract";

export class TransportSnapshotValidationError extends Error {
  constructor(path: string, expectation: string) {
    super(`后端对话快照无效：${path} 应为${expectation}`);
    this.name = "TransportSnapshotValidationError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new TransportSnapshotValidationError(path, "对象");
  return value;
}

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== "string") throw new TransportSnapshotValidationError(path, "字符串");
  return value;
}

function requireNullableNonNegativeInteger(value: unknown, path: string): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || (value as number) < 0) {
    throw new TransportSnapshotValidationError(path, "非负整数或 null");
  }
  return value as number;
}

function requirePositiveInteger(value: unknown, path: string): number {
  if (!Number.isInteger(value) || (value as number) < 1) {
    throw new TransportSnapshotValidationError(path, "正整数");
  }
  return value as number;
}

/** 子 Agent 卡片的状态是子 Session 的 Run 状态（不是工具状态），与后端 `RunStatus` 同一组值。 */
function requireKnownChildStatus(value: unknown, path: string): void {
  if (!(RUN_STATUSES as readonly unknown[]).includes(value)) {
    throw new TransportSnapshotValidationError(path, "Run 状态");
  }
}

function requireOptionalNonEmptyString(value: unknown, path: string): void {
  if (value !== undefined && (typeof value !== "string" || value.trim() === "")) {
    throw new TransportSnapshotValidationError(path, "非空字符串或 undefined");
  }
}

function validateDelegationDisplayData(data: Record<string, unknown>, path: string): void {
  const allowed = [
    "kind", "title", "child_agent_id", "delegation_id", "child_session_id",
    "status", "role",
  ];
  if (Object.keys(data).some((key) => !allowed.includes(key))) throw new TransportSnapshotValidationError(path, "已知字段");
  if (typeof data.title !== "string" || data.title.trim() === "") throw new TransportSnapshotValidationError(`${path}.title`, "非空字符串");
  requireOptionalNonEmptyString(data.child_agent_id, `${path}.child_agent_id`);
  requireOptionalNonEmptyString(data.role, `${path}.role`);
  if (data.delegation_id !== undefined) requirePositiveInteger(data.delegation_id, `${path}.delegation_id`);
  if (data.child_session_id !== undefined) requirePositiveInteger(data.child_session_id, `${path}.child_session_id`);
  if (data.status !== undefined) requireKnownChildStatus(data.status, `${path}.status`);
}

function validateForkDisplayData(data: Record<string, unknown>, path: string): void {
  requireExactKeys(data, ["kind", "title", "child_session_id"], path);
  if (typeof data.title !== "string" || data.title.trim() === "") throw new TransportSnapshotValidationError(`${path}.title`, "非空字符串");
  requirePositiveInteger(data.child_session_id, `${path}.child_session_id`);
}

function validateAgentSessionResultDisplayData(data: Record<string, unknown>, path: string): void {
  requireExactKeys(data, [
    "kind", "operation", "session_id", "agent_name", ...(data.operation === "status" ? ["final_output", "end_reason"] : []),
  ], path);
  if (data.operation !== "send" && data.operation !== "status") throw new TransportSnapshotValidationError(`${path}.operation`, "send 或 status");
  requirePositiveInteger(data.session_id, `${path}.session_id`);
  if (data.agent_name !== null && (typeof data.agent_name !== "string" || data.agent_name.trim() === "")) throw new TransportSnapshotValidationError(`${path}.agent_name`, "非空字符串或 null");
  if (data.operation === "send") return;
  if (data.final_output !== null && typeof data.final_output !== "string") throw new TransportSnapshotValidationError(`${path}.final_output`, "字符串或 null");
  if (data.end_reason !== null && typeof data.end_reason !== "string") throw new TransportSnapshotValidationError(`${path}.end_reason`, "字符串或 null");
}

function validateAgentConfigurationDraftDisplayData(data: Record<string, unknown>, path: string): void {
  requireExactKeys(data, ["kind", "status", "agent_id", "role", "description", "system_prompt"], path);
  if (data.status !== "draft") throw new TransportSnapshotValidationError(`${path}.status`, "draft");
  for (const field of ["agent_id", "role", "description", "system_prompt"]) {
    if (typeof data[field] !== "string" || data[field].trim() === "") {
      throw new TransportSnapshotValidationError(`${path}.${field}`, "非空字符串");
    }
  }
}

function validateDisplayData(value: unknown, path: string): void {
  if (value === null || value === undefined) return;
  const data = requireRecord(value, path);
  if (data.kind !== undefined && typeof data.kind !== "string") throw new TransportSnapshotValidationError(`${path}.kind`, "字符串");
  if (data.kind === "delegation-result") validateDelegationDisplayData(data, path);
  if (data.kind === "fork-session-result") validateForkDisplayData(data, path);
  if (data.kind === "agent-session-result") validateAgentSessionResultDisplayData(data, path);
  if (data.kind === "agent-configuration-draft") validateAgentConfigurationDraftDisplayData(data, path);
}

const USAGE_KEYS = [
  "input_tokens",
  "output_tokens",
  "total_tokens",
  "cache_hit_tokens",
  "cache_miss_tokens",
  "reasoning_tokens",
] as const;

function validateLaunch(value: unknown, path: string): void {
  const launch = requireRecord(value, path);
  requireExactKeys(launch, ["modelConfigId", "reasoningEffort"], path);
  if (launch.modelConfigId !== null && (typeof launch.modelConfigId !== "string" || !launch.modelConfigId)) throw new TransportSnapshotValidationError(`${path}.modelConfigId`, "非空字符串或 null");
  if (launch.reasoningEffort !== null && typeof launch.reasoningEffort !== "string") throw new TransportSnapshotValidationError(`${path}.reasoningEffort`, "字符串或 null");
}

function requireExactKeys(value: Record<string, unknown>, keys: readonly string[], path: string): void {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new TransportSnapshotValidationError(path, `固定字段 ${keys.join(", ")}`);
  }
}

function validateError(value: unknown, path: string): void {
  const error = requireRecord(value, path);
  requireExactKeys(error, ["code", "message"], path);
  // 与后端 ``_validate_error`` 同口径：code 与 message 都必须是非空白字符串，空白文案会让
  // 界面渲染出没有内容的错误提示。
  if (requireString(error.code, `${path}.code`).trim() === "") {
    throw new TransportSnapshotValidationError(`${path}.code`, "非空白字符串");
  }
  if (requireString(error.message, `${path}.message`).trim() === "") {
    throw new TransportSnapshotValidationError(`${path}.message`, "非空白字符串");
  }
}

function validateUsage(value: unknown, path: string): void {
  const usage = requireRecord(value, path);
  requireExactKeys(usage, USAGE_KEYS, path);
  for (const key of USAGE_KEYS) {
    const count = usage[key];
    if (count === null) continue;
    if (!Number.isInteger(count) || (count as number) < 0) {
      throw new TransportSnapshotValidationError(`${path}.${key}`, "非负整数");
    }
  }
}

function validatePart(value: unknown, path: string): void {
  const part = requireRecord(value, path);
  const type = requireString(part.type, `${path}.type`);
  if (type === "text") {
    if (Object.keys(part).some((key) => !["type", "text", "status", "sourceSequence", "finalReply"].includes(key))) {
      throw new TransportSnapshotValidationError(path, "已知字段");
    }
    requireString(part.text, `${path}.text`);
    if (hasOwn(part, "finalReply") && typeof part.finalReply !== "boolean") throw new TransportSnapshotValidationError(path, "finalReply 布尔值");
    if (hasOwn(part, "sourceSequence") && (!Number.isInteger(part.sourceSequence) || Number(part.sourceSequence) < 0)) throw new TransportSnapshotValidationError(path, "sourceSequence 非负整数");
    if (hasOwn(part, "status") && part.status !== undefined && part.status !== "running" && part.status !== "completed") {
      throw new TransportSnapshotValidationError(`${path}.status`, "running 或 completed");
    }
    return;
  }
  if (type === "agent-message") {
    requireExactKeys(part, ["type", "id", "text", "source"], path);
    if (!requireString(part.id, `${path}.id`)) throw new TransportSnapshotValidationError(`${path}.id`, "非空字符串");
    requireString(part.text, `${path}.text`);
    validateSource(part.source, `${path}.source`);
    return;
  }
  if (type === "image") {
    requireExactKeys(part, ["type", "image"], path);
    const image = requireString(part.image, `${path}.image`);
    if (!IMAGE_LOCATOR.test(image)) {
      throw new TransportSnapshotValidationError(`${path}.image`, "合法的附件 locator");
    }
    return;
  }
  if (type !== "tool-call") throw new TransportSnapshotValidationError(`${path}.type`, "已知消息 part 类型");
  const allowed = ["type", "toolCallId", "toolName", "presentation", "status", "args", "intent", "error", "errorCode", "awaitingUser", "display_data", "background", "isError", "child_session_id", "agent_role", "startedAt", "terminal_output_seq"];
  if (Object.keys(part).some((key) => !allowed.includes(key))) throw new TransportSnapshotValidationError(path, "已知字段");
  requireString(part.toolCallId, `${path}.toolCallId`);
  requireString(part.toolName, `${path}.toolName`);
  if (part.intent != null) requireString(part.intent, `${path}.intent`);
  if (!["activity", "standalone", "quiet"].includes(part.presentation as string)) throw new TransportSnapshotValidationError(path, "工具呈现类别");
  const status = requireString(part.status, `${path}.status`);
  if (!["pending", "running", "completed", "failed", "cancelled"].includes(status)) {
    throw new TransportSnapshotValidationError(`${path}.status`, "受支持的工具状态");
  }
  if (hasOwn(part, "args") && part.args !== null && !isRecord(part.args)) throw new TransportSnapshotValidationError(`${path}.args`, "对象或 null");
  if (hasOwn(part, "error") && part.error !== null && typeof part.error !== "string") throw new TransportSnapshotValidationError(`${path}.error`, "字符串或 null");
  if (hasOwn(part, "errorCode") && part.errorCode !== null && part.errorCode !== undefined && typeof part.errorCode !== "string") throw new TransportSnapshotValidationError(`${path}.errorCode`, "字符串或 null");
  // 后端每次显式写：缺省会让「排队待执行」与「等你操作」再次混在 pending 里。
  if (typeof part.awaitingUser !== "boolean") throw new TransportSnapshotValidationError(`${path}.awaitingUser`, "布尔值");
  if (hasOwn(part, "display_data")) validateDisplayData(part.display_data, `${path}.display_data`);
  if (hasOwn(part, "isError") && part.isError !== null && typeof part.isError !== "boolean") throw new TransportSnapshotValidationError(`${path}.isError`, "布尔值或 null");
  if (hasOwn(part, "child_session_id") && (!Number.isInteger(part.child_session_id) || (part.child_session_id as number) < 1)) throw new TransportSnapshotValidationError(`${path}.child_session_id`, "正整数");
  if (hasOwn(part, "background")) validateBackgroundWork(part.background, `${path}.background`);
  if (hasOwn(part, "agent_role") && (typeof part.agent_role !== "string" || !part.agent_role.trim())) throw new TransportSnapshotValidationError(`${path}.agent_role`, "非空字符串");
  if (hasOwn(part, "startedAt") && part.startedAt !== null && (typeof part.startedAt !== "string" || !part.startedAt)) throw new TransportSnapshotValidationError(`${path}.startedAt`, "非空字符串或 null");
  if (hasOwn(part, "terminal_output_seq") && (!Number.isInteger(part.terminal_output_seq) || (part.terminal_output_seq as number) < 0)) throw new TransportSnapshotValidationError(`${path}.terminal_output_seq`, "非负整数");
}

const RUN_STATUSES = ["running", "suspended", "completed", "failed", "cancelled"] as const;
const TERMINAL_STATUSES = ["completed", "failed", "cancelled"] as const;

function validateMessageBody(message: Record<string, unknown>, path: string, seenIds: Set<string>): void {
  const id = requireString(message.id, `${path}.id`);
  if (id === "" || seenIds.has(id)) throw new TransportSnapshotValidationError(`${path}.id`, "非空且在 Session 内唯一");
  seenIds.add(id);
  if (message.role !== "user" && message.role !== "assistant") throw new TransportSnapshotValidationError(`${path}.role`, "user 或 assistant");
  if (!Array.isArray(message.parts)) throw new TransportSnapshotValidationError(`${path}.parts`, "数组");
  message.parts.forEach((part, index) => validatePart(part, `${path}.parts[${index}]`));
}

function validateOutcome(value: unknown, path: string): void {
  const outcome = requireRecord(value, path);
  requireExactKeys(outcome, ["status", "endReason", "error", "usage", "contextWindowTotal", "durationMs"], path);
  if (!(TERMINAL_STATUSES as readonly unknown[]).includes(outcome.status)) throw new TransportSnapshotValidationError(`${path}.status`, "Run 终态");
  if (outcome.endReason !== null && typeof outcome.endReason !== "string") throw new TransportSnapshotValidationError(`${path}.endReason`, "字符串或 null");
  if (outcome.error !== null) validateError(outcome.error, `${path}.error`);
  if (outcome.usage !== null) validateUsage(outcome.usage, `${path}.usage`);
  requireNullableNonNegativeInteger(outcome.contextWindowTotal, `${path}.contextWindowTotal`);
  if (requireNullableNonNegativeInteger(outcome.durationMs, `${path}.durationMs`) === null) {
    throw new TransportSnapshotValidationError(`${path}.durationMs`, "非负整数");
  }
}

function validateActivity(value: unknown, path: string): void {
  const activity = requireRecord(value, path);
  requireExactKeys(activity, ["status", "turn", "resumable", "endReason", "error", "usage", "contextWindowTotal", "launch", "startedAt", "modelActivity"], path);
  if (activity.modelActivity !== null) {
    const model = requireRecord(activity.modelActivity, `${path}.modelActivity`);
    requireExactKeys(model, ["stepId", "phase"], path);
    requireString(model.stepId, path);
    if (activity.status !== "running" || !["waiting", "reasoning", "responding", "tool_call"].includes(model.phase as string)) throw new TransportSnapshotValidationError(path, "模型活动阶段");
  }
  if (activity.turn !== null) requirePositiveInteger(activity.turn, `${path}.turn`);
  if (typeof activity.resumable !== "boolean") throw new TransportSnapshotValidationError(`${path}.resumable`, "布尔值");
  if (!(RUN_STATUSES as readonly unknown[]).includes(activity.status)) throw new TransportSnapshotValidationError(`${path}.status`, "受支持的运行状态");
  if (activity.endReason !== null && typeof activity.endReason !== "string") throw new TransportSnapshotValidationError(`${path}.endReason`, "字符串或 null");
  if (activity.error !== null) validateError(activity.error, `${path}.error`);
  if (activity.usage !== null) validateUsage(activity.usage, `${path}.usage`);
  requireNullableNonNegativeInteger(activity.contextWindowTotal, `${path}.contextWindowTotal`);
  validateLaunch(activity.launch, `${path}.launch`);
  if (Number.isNaN(Date.parse(requireString(activity.startedAt, `${path}.startedAt`)))) {
    throw new TransportSnapshotValidationError(`${path}.startedAt`, "ISO 时间字符串");
  }
}

const TERMINAL_SESSION_STATUSES = ["starting", "running", "exited", "closed", "failed"] as const;
const TERMINAL_INTEGRATION_STATES = ["starting", "ready", "unavailable", "off"] as const;

function validateTerminalSession(value: unknown, path: string, seenIds: Set<string>): void {
  const session = requireRecord(value, path);
  requireExactKeys(session, [
    "terminalSessionId", "shellKind", "status", "endReason", "exitCode",
    "integration", "commandRunning", "createdAt",
  ], path);
  const terminalSessionId = requireString(session.terminalSessionId, `${path}.terminalSessionId`);
  if (terminalSessionId === "" || seenIds.has(terminalSessionId)) throw new TransportSnapshotValidationError(`${path}.terminalSessionId`, "非空且在 Session 内唯一");
  seenIds.add(terminalSessionId);
  if (requireString(session.shellKind, `${path}.shellKind`).trim() === "") throw new TransportSnapshotValidationError(`${path}.shellKind`, "非空字符串");
  if (!(TERMINAL_SESSION_STATUSES as readonly unknown[]).includes(session.status)) throw new TransportSnapshotValidationError(`${path}.status`, "受支持的终端会话状态");
  if (session.endReason !== null && typeof session.endReason !== "string") throw new TransportSnapshotValidationError(`${path}.endReason`, "字符串或 null");
  if (session.exitCode !== null && !Number.isInteger(session.exitCode)) throw new TransportSnapshotValidationError(`${path}.exitCode`, "整数或 null");
  if (!(TERMINAL_INTEGRATION_STATES as readonly unknown[]).includes(session.integration)) throw new TransportSnapshotValidationError(`${path}.integration`, "受支持的集成状态");
  if (typeof session.commandRunning !== "boolean") throw new TransportSnapshotValidationError(`${path}.commandRunning`, "布尔值");
  requireString(session.createdAt, `${path}.createdAt`);
}

const BACKGROUND_TASK_KINDS = ["terminal_command"] as const;
const BACKGROUND_TASK_STATUSES = ["running", "settling", "completed", "cancelled"] as const;

function validateBackgroundWork(value: unknown, path: string): void {
  const work = requireRecord(value, path);
  requireExactKeys(work, ["kind", "label", "startedAt", "endedAt"], path);
  if (!(BACKGROUND_TASK_KINDS as readonly unknown[]).includes(work.kind)) throw new TransportSnapshotValidationError(`${path}.kind`, "受支持的后台工作种类");
  requireString(work.label, `${path}.label`);
  requireString(work.startedAt, `${path}.startedAt`);
  if (work.endedAt !== null) requireString(work.endedAt, `${path}.endedAt`);
}

function validateBackgroundTask(value: unknown, path: string, seenIds: Set<string>): void {
  const entry = requireRecord(value, path);
  requireExactKeys(entry, ["toolCallId", "kind", "label", "status", "startedAt", "endedAt"], path);
  const toolCallId = requireString(entry.toolCallId, `${path}.toolCallId`);
  if (toolCallId === "" || seenIds.has(toolCallId)) throw new TransportSnapshotValidationError(`${path}.toolCallId`, "非空且在 Session 内唯一");
  seenIds.add(toolCallId);
  if (!(BACKGROUND_TASK_KINDS as readonly unknown[]).includes(entry.kind)) throw new TransportSnapshotValidationError(`${path}.kind`, "受支持的后台工作种类");
  if (!(BACKGROUND_TASK_STATUSES as readonly unknown[]).includes(entry.status)) throw new TransportSnapshotValidationError(`${path}.status`, "受支持的后台工作状态");
  requireString(entry.label, `${path}.label`);
  requireString(entry.startedAt, `${path}.startedAt`);
  if (entry.endedAt !== null) requireString(entry.endedAt, `${path}.endedAt`);
}

/**
 * 校验更早历史的一页。`loadedIds` 是已加载消息的 id：页里的消息必须与它们互不重复（页与窗口在完整
 * 历史里不重叠，重复说明位置已错乱）。
 */
export function parseHistoryPage(value: unknown, loadedIds: ReadonlySet<string>): TransportHistoryPage {
  const page = requireRecord(value, "history");
  requireExactKeys(page, ["messages", "messageOffset"], "history");
  validateMessages(page.messages, "history.messages", new Set(loadedIds));
  if (requireNullableNonNegativeInteger(page.messageOffset, "history.messageOffset") === null) throw new TransportSnapshotValidationError("history.messageOffset", "非负整数");
  return page as unknown as TransportHistoryPage;
}

function validateMessages(value: unknown, path: string, ids: Set<string>): void {
  if (!Array.isArray(value)) throw new TransportSnapshotValidationError(path, "数组");
  value.forEach((message, index) => validateMessage(message, `${path}[${index}]`, ids));
}

function validateMessage(rawMessage: unknown, messagePath: string, ids: Set<string>): void {
  const message = requireRecord(rawMessage, messagePath);
  requireExactKeys(message, ["id", "role", "parts", "turn", "inherited", "outcome", "source"], messagePath);
  validateMessageBody(message, messagePath, ids);
  if (requireNullableNonNegativeInteger(message.turn, `${messagePath}.turn`) === null) {
    throw new TransportSnapshotValidationError(`${messagePath}.turn`, "非负整数");
  }
  if (typeof message.inherited !== "boolean") throw new TransportSnapshotValidationError(`${messagePath}.inherited`, "布尔值");
  if (message.outcome !== null) {
    if (message.role !== "assistant") throw new TransportSnapshotValidationError(`${messagePath}.outcome`, "user 消息上为 null");
    validateOutcome(message.outcome, `${messagePath}.outcome`);
  }
  if (message.source !== null) {
    if (message.role !== "user") throw new TransportSnapshotValidationError(`${messagePath}.source`, "assistant 消息上为 null");
    validateSource(message.source, `${messagePath}.source`);
  }
}

function validateSource(value: unknown, path: string): void {
  const source = requireRecord(value, path);
  requireExactKeys(source, ["sessionId", "name", "report"], path);
  if (requireNullableNonNegativeInteger(source.sessionId, `${path}.sessionId`) === null) throw new TransportSnapshotValidationError(`${path}.sessionId`, "非负整数");
  requireString(source.name, `${path}.name`);
  if (source.report !== null) {
    const report = requireRecord(source.report, `${path}.report`);
    requireExactKeys(report, ["status", "intermediate"], `${path}.report`);
    if (report.status !== "completed" && report.status !== "failed") throw new TransportSnapshotValidationError(`${path}.report.status`, "completed 或 failed");
    if (typeof report.intermediate !== "boolean") throw new TransportSnapshotValidationError(`${path}.report.intermediate`, "布尔值");
  }
}

/**
 * 整帧增量应用后复用快照契约校验，成功后才能提交。普通消息增量只检查受影响的消息；
 * 消息身份变化时才核对其与其余消息的唯一性，逐 token 更新不扫描整段历史。
 * 根对象、消息列表或其他顶层字段被替换时走完整校验。
 */
export function validateTransportUpdate(previous: TransportState, next: TransportState, paths: readonly (readonly (string | number)[])[]): void {
  if (paths.some((path) => path[0] !== "messages" || typeof path[1] !== "number")) {
    parseTransportState(next);
    return;
  }
  const indexes = new Set(paths.map((path) => path[1] as number));
  const identityChanged = [...indexes].some((index) => next.messages[index]?.id !== previous.messages[index]?.id);
  const ids = identityChanged
    ? new Set([
      ...previous.messages.filter((_, index) => !indexes.has(index)).map((message) => message.id),
      ...(next.queued_messages ?? []).map((message) => message.id),
    ])
    : new Set<string>();
  for (const index of indexes) validateMessage(next.messages[index], `messages[${index}]`, ids);
}

export function parseTransportState(value: unknown): TransportState {
  const state = requireRecord(value, "snapshot");
  requireExactKeys(state, ["title", "messages", "messageOffset", "activity", "terminal_sessions", "background_tasks", "foreground_waits", "handoffs", ...(state.fork === undefined ? [] : ["fork"]), ...(state.queued_messages === undefined ? [] : ["queued_messages"])], "snapshot");
  if (typeof state.title !== "string" || state.title.trim() === "") throw new TransportSnapshotValidationError("title", "非空字符串");
  if (state.fork !== undefined) {
    const fork = requireRecord(state.fork, "fork");
    requireExactKeys(fork, ["sourceSessionId", "sourceTitle", "sessionType", "initialLaunch"], "fork");
    if (requireNullableNonNegativeInteger(fork.sourceSessionId, "fork.sourceSessionId") === null || typeof fork.sourceTitle !== "string" || !["agent_fork", "user_fork"].includes(fork.sessionType as string)) throw new TransportSnapshotValidationError("fork", "有效来源");
    validateLaunch(fork.initialLaunch, "fork.initialLaunch");
  }
  const ids = new Set<string>();
  validateMessages(state.messages, "messages", ids);
  if (requireNullableNonNegativeInteger(state.messageOffset, "messageOffset") === null) throw new TransportSnapshotValidationError("messageOffset", "非负整数");
  if (state.queued_messages !== undefined) {
    if (!Array.isArray(state.queued_messages)) throw new TransportSnapshotValidationError("queued_messages", "数组");
    state.queued_messages.forEach((rawMessage, index) => {
      const path = `queued_messages[${index}]`;
      const message = requireRecord(rawMessage, path);
      requireExactKeys(message, ["id", "role", "parts", "delivery"], path);
      validateMessageBody(message, path, ids);
      if (message.delivery !== "immediate" && message.delivery !== "next_run") throw new TransportSnapshotValidationError(`${path}.delivery`, "immediate 或 next_run");
      if (message.role !== "user") throw new TransportSnapshotValidationError(`${path}.role`, "user");
    });
  }
  if (state.activity !== null) validateActivity(state.activity, "activity");
  if (!Array.isArray(state.terminal_sessions)) throw new TransportSnapshotValidationError("terminal_sessions", "数组");
  const sessionIds = new Set<string>();
  state.terminal_sessions.forEach((session, index) => validateTerminalSession(session, `terminal_sessions[${index}]`, sessionIds));
  if (!Array.isArray(state.background_tasks)) throw new TransportSnapshotValidationError("background_tasks", "数组");
  const backgroundTaskIds = new Set<string>();
  state.background_tasks.forEach((entry, index) => validateBackgroundTask(entry, `background_tasks[${index}]`, backgroundTaskIds));
  if (!Array.isArray(state.foreground_waits)) throw new TransportSnapshotValidationError("foreground_waits", "数组");
  const foregroundIds = new Set<string>();
  state.foreground_waits.forEach((value, index) => {
    const toolCallId = requireString(value, `foreground_waits[${index}]`);
    if (toolCallId === "" || foregroundIds.has(toolCallId)) throw new TransportSnapshotValidationError(`foreground_waits[${index}]`, "非空且在 Session 内唯一");
    foregroundIds.add(toolCallId);
  });
  if (!Array.isArray(state.handoffs)) throw new TransportSnapshotValidationError("handoffs", "数组");
  const handoffCalls = new Set<string>();
  const handoffResources = new Set<string>();
  state.handoffs.forEach((raw, index) => {
    const path = `handoffs[${index}]`;
    const entry = requireRecord(raw, path);
    requireExactKeys(entry, ["toolCallId", "toolName", "resourceId", "reason"], path);
    const toolCallId = requireString(entry.toolCallId, `${path}.toolCallId`);
    const resourceId = requireString(entry.resourceId, `${path}.resourceId`);
    requireString(entry.toolName, `${path}.toolName`);
    requireString(entry.reason, `${path}.reason`);
    if (toolCallId === "" || resourceId === "" || handoffCalls.has(toolCallId) || handoffResources.has(resourceId)) {
      throw new TransportSnapshotValidationError(path, "调用与资源非空且唯一");
    }
    handoffCalls.add(toolCallId);
    handoffResources.add(resourceId);
  });
  return state as unknown as TransportState;
}
