import type { TransportState } from "@/lib/assistant/contract";

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

function requireKnownChildStatus(value: unknown, path: string): void {
  if (value !== "pending" && value !== "running" && value !== "completed" && value !== "failed" && value !== "cancelled") {
    throw new TransportSnapshotValidationError(path, "受支持的子 Agent 状态");
  }
}

function requireOptionalNonEmptyString(value: unknown, path: string): void {
  if (value !== undefined && (typeof value !== "string" || value.trim() === "")) {
    throw new TransportSnapshotValidationError(path, "非空字符串或 undefined");
  }
}

function validateDelegationDisplayData(data: Record<string, unknown>, path: string): void {
  const allowed = [
    "kind", "title", "child_agent_id", "delegation_id", "child_task_id",
    "status", "role",
  ];
  if (Object.keys(data).some((key) => !allowed.includes(key))) throw new TransportSnapshotValidationError(path, "已知字段");
  if (typeof data.title !== "string" || data.title.trim() === "") throw new TransportSnapshotValidationError(`${path}.title`, "非空字符串");
  requireOptionalNonEmptyString(data.child_agent_id, `${path}.child_agent_id`);
  requireOptionalNonEmptyString(data.role, `${path}.role`);
  if (data.delegation_id !== undefined) requirePositiveInteger(data.delegation_id, `${path}.delegation_id`);
  if (data.child_task_id !== undefined) requirePositiveInteger(data.child_task_id, `${path}.child_task_id`);
  if (data.status !== undefined) requireKnownChildStatus(data.status, `${path}.status`);
}

function validateChildAgentResultDisplayData(data: Record<string, unknown>, path: string): void {
  requireExactKeys(data, [
    "kind", "operation", "child_task_id", "status", "agent_id", "agent_name", "final_output", "end_reason",
  ], path);
  if (data.operation !== "send" && data.operation !== "status") throw new TransportSnapshotValidationError(`${path}.operation`, "send 或 status");
  requirePositiveInteger(data.child_task_id, `${path}.child_task_id`);
  requireKnownChildStatus(data.status, `${path}.status`);
  if (data.agent_id !== null && (typeof data.agent_id !== "string" || data.agent_id.trim() === "")) throw new TransportSnapshotValidationError(`${path}.agent_id`, "非空字符串或 null");
  if (data.agent_name !== null && (typeof data.agent_name !== "string" || data.agent_name.trim() === "")) throw new TransportSnapshotValidationError(`${path}.agent_name`, "非空字符串或 null");
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
  if (data.kind === "child-agent-result") validateChildAgentResultDisplayData(data, path);
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
  requireExactKeys(launch, ["modelConfigId", "reasoningEffort", "banTools"], path);
  requireNullableNonNegativeInteger(launch.modelConfigId, `${path}.modelConfigId`);
  if (launch.reasoningEffort !== null && typeof launch.reasoningEffort !== "string") throw new TransportSnapshotValidationError(`${path}.reasoningEffort`, "字符串或 null");
  if (!Array.isArray(launch.banTools) || !launch.banTools.every((name) => typeof name === "string")) throw new TransportSnapshotValidationError(`${path}.banTools`, "字符串数组");
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

function validateCommandError(value: unknown, path: string): void {
  const error = requireRecord(value, path);
  requireExactKeys(error, ["code", "message", "seq"], path);
  if (!Number.isInteger(error.seq) || (error.seq as number) <= 0) {
    throw new TransportSnapshotValidationError(`${path}.seq`, "正整数");
  }
  validateError({ code: error.code, message: error.message }, path);
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
  if (type === "text" || type === "reasoning") {
    if (Object.keys(part).some((key) => !["type", "text", "status", "unstable_summary"].includes(key))) {
      throw new TransportSnapshotValidationError(path, "已知字段");
    }
    requireString(part.text, `${path}.text`);
    if (hasOwn(part, "status") && part.status !== undefined && part.status !== "running" && part.status !== "completed") {
      throw new TransportSnapshotValidationError(`${path}.status`, "running 或 completed");
    }
    if (type === "reasoning" && hasOwn(part, "unstable_summary") && part.unstable_summary !== undefined && typeof part.unstable_summary !== "string") {
      throw new TransportSnapshotValidationError(`${path}.unstable_summary`, "字符串");
    }
    return;
  }
  if (type === "image") {
    requireExactKeys(part, ["type", "image"], path);
    const image = requireString(part.image, `${path}.image`);
    if (!/^cosir-attachment:\/\/[0-9a-f]{64}$/.test(image)) {
      throw new TransportSnapshotValidationError(`${path}.image`, "合法的附件 locator");
    }
    return;
  }
  if (type === "file") {
    requireExactKeys(part, ["type", "file", "name", "contentType"], path);
    const file = requireString(part.file, `${path}.file`);
    if (!/^cosir-local-file:[A-Za-z0-9._-]{1,128}$/.test(file)) {
      throw new TransportSnapshotValidationError(`${path}.file`, "合法的本地附件 locator");
    }
    if (!requireString(part.name, `${path}.name`)) {
      throw new TransportSnapshotValidationError(`${path}.name`, "非空字符串");
    }
    if (!requireString(part.contentType, `${path}.contentType`)) {
      throw new TransportSnapshotValidationError(`${path}.contentType`, "非空字符串");
    }
    return;
  }
  if (type !== "tool-call") throw new TransportSnapshotValidationError(`${path}.type`, "已知消息 part 类型");
  const allowed = ["type", "toolCallId", "toolName", "status", "args", "error", "errorCode", "presentation", "display_data", "team_run_id", "isError", "approvalRequestId", "child_task_id", "agent_role", "terminal_output_seq"];
  if (Object.keys(part).some((key) => !allowed.includes(key))) throw new TransportSnapshotValidationError(path, "已知字段");
  requireString(part.toolCallId, `${path}.toolCallId`);
  requireString(part.toolName, `${path}.toolName`);
  const status = requireString(part.status, `${path}.status`);
  if (!["pending", "running", "completed", "failed", "cancelled"].includes(status)) {
    throw new TransportSnapshotValidationError(`${path}.status`, "受支持的工具状态");
  }
  if (hasOwn(part, "args") && part.args !== null && !isRecord(part.args)) throw new TransportSnapshotValidationError(`${path}.args`, "对象或 null");
  if (hasOwn(part, "error") && part.error !== null && typeof part.error !== "string") throw new TransportSnapshotValidationError(`${path}.error`, "字符串或 null");
  if (hasOwn(part, "errorCode") && part.errorCode !== null && part.errorCode !== undefined && typeof part.errorCode !== "string") throw new TransportSnapshotValidationError(`${path}.errorCode`, "字符串或 null");
  if (hasOwn(part, "presentation") && !isRecord(part.presentation)) throw new TransportSnapshotValidationError(`${path}.presentation`, "对象");
  if (hasOwn(part, "display_data")) validateDisplayData(part.display_data, `${path}.display_data`);
  if (hasOwn(part, "isError") && part.isError !== null && typeof part.isError !== "boolean") throw new TransportSnapshotValidationError(`${path}.isError`, "布尔值或 null");
  if (hasOwn(part, "child_task_id") && (!Number.isInteger(part.child_task_id) || (part.child_task_id as number) < 1)) throw new TransportSnapshotValidationError(`${path}.child_task_id`, "正整数");
  if (hasOwn(part, "team_run_id") && (!Number.isInteger(part.team_run_id) || (part.team_run_id as number) < 1)) throw new TransportSnapshotValidationError(`${path}.team_run_id`, "正整数");
  if (hasOwn(part, "agent_role") && (typeof part.agent_role !== "string" || !part.agent_role.trim())) throw new TransportSnapshotValidationError(`${path}.agent_role`, "非空字符串");
  if (hasOwn(part, "terminal_output_seq") && (!Number.isInteger(part.terminal_output_seq) || (part.terminal_output_seq as number) < 0)) throw new TransportSnapshotValidationError(`${path}.terminal_output_seq`, "非负整数");
  if (part.approvalRequestId !== null && part.approvalRequestId !== undefined) throw new TransportSnapshotValidationError(`${path}.approvalRequestId`, "null 或 undefined");
}

const RUN_STATUSES = ["running", "suspended", "completed", "failed", "cancelled"] as const;
const TERMINAL_STATUSES = ["completed", "failed", "cancelled"] as const;

function validateMessageBody(message: Record<string, unknown>, path: string, seenIds: Set<string>): void {
  const id = requireString(message.id, `${path}.id`);
  if (id === "" || seenIds.has(id)) throw new TransportSnapshotValidationError(`${path}.id`, "非空且在 Task 内唯一");
  seenIds.add(id);
  if (message.role !== "user" && message.role !== "assistant") throw new TransportSnapshotValidationError(`${path}.role`, "user 或 assistant");
  if (!Array.isArray(message.parts)) throw new TransportSnapshotValidationError(`${path}.parts`, "数组");
  message.parts.forEach((part, index) => validatePart(part, `${path}.parts[${index}]`));
}

function validateOutcome(value: unknown, path: string): void {
  const outcome = requireRecord(value, path);
  requireExactKeys(outcome, ["status", "endReason", "error", "usage", "contextWindowTotal"], path);
  if (!(TERMINAL_STATUSES as readonly unknown[]).includes(outcome.status)) throw new TransportSnapshotValidationError(`${path}.status`, "Run 终态");
  if (outcome.endReason !== null && typeof outcome.endReason !== "string") throw new TransportSnapshotValidationError(`${path}.endReason`, "字符串或 null");
  if (outcome.error !== null) validateError(outcome.error, `${path}.error`);
  if (outcome.usage !== null) validateUsage(outcome.usage, `${path}.usage`);
}

function validateActivity(value: unknown, path: string): void {
  const activity = requireRecord(value, path);
  requireExactKeys(activity, ["runId", "status", "endReason", "error", "usage", "contextWindowTotal", "launch"], path);
  if (requireNullableNonNegativeInteger(activity.runId, `${path}.runId`) === null) {
    throw new TransportSnapshotValidationError(`${path}.runId`, "非负整数");
  }
  if (!(RUN_STATUSES as readonly unknown[]).includes(activity.status)) throw new TransportSnapshotValidationError(`${path}.status`, "受支持的运行状态");
  if (activity.endReason !== null && typeof activity.endReason !== "string") throw new TransportSnapshotValidationError(`${path}.endReason`, "字符串或 null");
  if (activity.error !== null) validateError(activity.error, `${path}.error`);
  if (activity.usage !== null) validateUsage(activity.usage, `${path}.usage`);
  requireNullableNonNegativeInteger(activity.contextWindowTotal, `${path}.contextWindowTotal`);
  validateLaunch(activity.launch, `${path}.launch`);
}

export function parseTransportState(value: unknown): TransportState {
  const state = requireRecord(value, "snapshot");
  requireExactKeys(state, ["messages", "activity", "error", ...(state.queued_messages === undefined ? [] : ["queued_messages"])], "snapshot");
  const ids = new Set<string>();
  if (!Array.isArray(state.messages)) throw new TransportSnapshotValidationError("messages", "数组");
  state.messages.forEach((rawMessage, index) => {
    const path = `messages[${index}]`;
    const message = requireRecord(rawMessage, path);
    requireExactKeys(message, ["id", "role", "parts", "runId", "outcome"], path);
    validateMessageBody(message, path, ids);
    if (requireNullableNonNegativeInteger(message.runId, `${path}.runId`) === null) {
      throw new TransportSnapshotValidationError(`${path}.runId`, "非负整数");
    }
    if (message.outcome !== null) {
      if (message.role !== "assistant") throw new TransportSnapshotValidationError(`${path}.outcome`, "user 消息上为 null");
      validateOutcome(message.outcome, `${path}.outcome`);
    }
  });
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
  if (state.error !== null) validateCommandError(state.error, "error");
  return state as unknown as TransportState;
}
