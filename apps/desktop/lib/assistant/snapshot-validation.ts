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
    "kind", "title", "child_agent_id", "delegation_id", "child_task_id", "child_run_id",
    "status", "role", "final_output", "status_hint", "end_reason",
  ];
  if (Object.keys(data).some((key) => !allowed.includes(key))) throw new TransportSnapshotValidationError(path, "已知字段");
  if (typeof data.title !== "string" || data.title.trim() === "") throw new TransportSnapshotValidationError(`${path}.title`, "非空字符串");
  requireOptionalNonEmptyString(data.child_agent_id, `${path}.child_agent_id`);
  requireOptionalNonEmptyString(data.role, `${path}.role`);
  if (data.delegation_id !== undefined) requirePositiveInteger(data.delegation_id, `${path}.delegation_id`);
  if (data.child_task_id !== undefined) requirePositiveInteger(data.child_task_id, `${path}.child_task_id`);
  if (data.child_run_id !== undefined) requirePositiveInteger(data.child_run_id, `${path}.child_run_id`);
  if (data.status !== undefined) requireKnownChildStatus(data.status, `${path}.status`);
  if (data.final_output !== undefined && typeof data.final_output !== "string") throw new TransportSnapshotValidationError(`${path}.final_output`, "字符串");
  requireOptionalNonEmptyString(data.status_hint, `${path}.status_hint`);
  requireOptionalNonEmptyString(data.end_reason, `${path}.end_reason`);
}

function validateChildWaitDisplayData(data: Record<string, unknown>, path: string): void {
  requireExactKeys(data, ["kind", "timed_out", "messages", "pending", "interrupted_by"], path);
  if (typeof data.timed_out !== "boolean") throw new TransportSnapshotValidationError(`${path}.timed_out`, "布尔值");
  if (!Array.isArray(data.messages)) throw new TransportSnapshotValidationError(`${path}.messages`, "数组");
  data.messages.forEach((value, index) => {
    const message = requireRecord(value, `${path}.messages[${index}]`);
    requireExactKeys(message, ["child_task_id", "child_run_id", "status", "final_output", "end_reason"], `${path}.messages[${index}]`);
    requirePositiveInteger(message.child_task_id, `${path}.messages[${index}].child_task_id`);
    requirePositiveInteger(message.child_run_id, `${path}.messages[${index}].child_run_id`);
    if (message.status !== "completed" && message.status !== "failed" && message.status !== "cancelled") {
      throw new TransportSnapshotValidationError(`${path}.messages[${index}].status`, "completed、failed 或 cancelled");
    }
    if (message.final_output !== null && typeof message.final_output !== "string") throw new TransportSnapshotValidationError(`${path}.messages[${index}].final_output`, "字符串或 null");
    if (message.end_reason !== null && typeof message.end_reason !== "string") throw new TransportSnapshotValidationError(`${path}.messages[${index}].end_reason`, "字符串或 null");
  });
  if (!Array.isArray(data.pending)) throw new TransportSnapshotValidationError(`${path}.pending`, "数组");
  data.pending.forEach((value, index) => {
    const pending = requireRecord(value, `${path}.pending[${index}]`);
    requireExactKeys(pending, ["child_task_id", "status"], `${path}.pending[${index}]`);
    requirePositiveInteger(pending.child_task_id, `${path}.pending[${index}].child_task_id`);
    if (pending.status !== null) {
      if (typeof pending.status !== "string") throw new TransportSnapshotValidationError(`${path}.pending[${index}].status`, "字符串或 null");
      requireKnownChildStatus(pending.status, `${path}.pending[${index}].status`);
    }
  });
  if (data.interrupted_by !== null && data.interrupted_by !== "parent_cancelled" && data.interrupted_by !== "session_closed" && data.interrupted_by !== "shutdown") {
    throw new TransportSnapshotValidationError(`${path}.interrupted_by`, "受支持的中断原因或 null");
  }
}

function validateChildAgentResultDisplayData(data: Record<string, unknown>, path: string): void {
  requireExactKeys(data, [
    "kind", "operation", "child_task_id", "child_run_id", "status", "agent_id", "agent_name", "final_output", "end_reason",
  ], path);
  if (data.operation !== "send" && data.operation !== "status") throw new TransportSnapshotValidationError(`${path}.operation`, "send 或 status");
  requirePositiveInteger(data.child_task_id, `${path}.child_task_id`);
  requirePositiveInteger(data.child_run_id, `${path}.child_run_id`);
  requireKnownChildStatus(data.status, `${path}.status`);
  if (data.agent_id !== null && (typeof data.agent_id !== "string" || data.agent_id.trim() === "")) throw new TransportSnapshotValidationError(`${path}.agent_id`, "非空字符串或 null");
  if (data.agent_name !== null && (typeof data.agent_name !== "string" || data.agent_name.trim() === "")) throw new TransportSnapshotValidationError(`${path}.agent_name`, "非空字符串或 null");
  if (data.final_output !== null && typeof data.final_output !== "string") throw new TransportSnapshotValidationError(`${path}.final_output`, "字符串或 null");
  if (data.end_reason !== null && typeof data.end_reason !== "string") throw new TransportSnapshotValidationError(`${path}.end_reason`, "字符串或 null");
}

function validateDisplayData(value: unknown, path: string): void {
  if (value === null || value === undefined) return;
  const data = requireRecord(value, path);
  if (data.kind !== undefined && typeof data.kind !== "string") throw new TransportSnapshotValidationError(`${path}.kind`, "字符串");
  if (data.kind === "delegation-result") validateDelegationDisplayData(data, path);
  if (data.kind === "child-agent-wait-result") validateChildWaitDisplayData(data, path);
  if (data.kind === "child-agent-result") validateChildAgentResultDisplayData(data, path);
}

const USAGE_KEYS = [
  "input_tokens",
  "output_tokens",
  "total_tokens",
  "cache_hit_tokens",
  "cache_miss_tokens",
  "reasoning_tokens",
] as const;

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
    if (key === "cache_miss_tokens" && count === null) continue;
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
  const allowed = ["type", "toolCallId", "toolName", "status", "args", "error", "errorCode", "presentation", "display_data", "isError", "approvalRequestId", "child_task_id", "child_run_id", "agent_role", "terminal_output_seq"];
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
  if (hasOwn(part, "child_run_id") && (!Number.isInteger(part.child_run_id) || (part.child_run_id as number) < 1)) throw new TransportSnapshotValidationError(`${path}.child_run_id`, "正整数");
  if (hasOwn(part, "agent_role") && (typeof part.agent_role !== "string" || !part.agent_role.trim())) throw new TransportSnapshotValidationError(`${path}.agent_role`, "非空字符串");
  if (hasOwn(part, "terminal_output_seq") && (!Number.isInteger(part.terminal_output_seq) || (part.terminal_output_seq as number) < 0)) throw new TransportSnapshotValidationError(`${path}.terminal_output_seq`, "非负整数");
  if (part.approvalRequestId !== null && part.approvalRequestId !== undefined) throw new TransportSnapshotValidationError(`${path}.approvalRequestId`, "null 或 undefined");
}

function validateMessage(value: unknown, path: string, seenIds: Set<string>): void {
  const message = requireRecord(value, path);
  requireExactKeys(message, ["id", "role", "parts"], path);
  const id = requireString(message.id, `${path}.id`);
  if (id === "" || seenIds.has(id)) throw new TransportSnapshotValidationError(`${path}.id`, "非空且在 Run 内唯一");
  seenIds.add(id);
  if (message.role !== "user" && message.role !== "assistant") throw new TransportSnapshotValidationError(`${path}.role`, "user 或 assistant");
  if (!Array.isArray(message.parts)) throw new TransportSnapshotValidationError(`${path}.parts`, "数组");
  message.parts.forEach((part, index) => validatePart(part, `${path}.parts[${index}]`));
}

export function parseTransportState(value: unknown): TransportState {
  const state = requireRecord(value, "snapshot");
  requireExactKeys(state, ["runs", "current_run_id", "approvals", "context_usage_ratio", "context_usage_used", "context_window_total", "error"], "snapshot");
  if (!Array.isArray(state.runs)) throw new TransportSnapshotValidationError("runs", "数组");
  const runIds = new Set<number>();
  const activeRunIds: number[] = [];
  for (const [index, rawRun] of state.runs.entries()) {
    const run = requireRecord(rawRun, `runs[${index}]`);
    requireExactKeys(run, ["runId", "status", "endReason", "messages", "usage", "error"], `runs[${index}]`);
    const runId = requireNullableNonNegativeInteger(run.runId, `runs[${index}].runId`);
    if (runId === null || runIds.has(runId)) throw new TransportSnapshotValidationError(`runs[${index}].runId`, "唯一的非负整数");
    runIds.add(runId);
    const status = requireString(run.status, `runs[${index}].status`);
    if (!["idle", "pending", "running", "completed", "failed", "cancelled", "interrupted"].includes(status)) throw new TransportSnapshotValidationError(`runs[${index}].status`, "受支持的运行状态");
    if (status === "pending" || status === "running") activeRunIds.push(runId);
    if (run.endReason !== null && typeof run.endReason !== "string") throw new TransportSnapshotValidationError(`runs[${index}].endReason`, "字符串或 null");
    if (!Array.isArray(run.messages)) throw new TransportSnapshotValidationError(`runs[${index}].messages`, "数组");
    const messageIds = new Set<string>();
    run.messages.forEach((message, messageIndex) => validateMessage(message, `runs[${index}].messages[${messageIndex}]`, messageIds));
    if (run.usage !== null) validateUsage(run.usage, `runs[${index}].usage`);
    if (run.error !== null) validateError(run.error, `runs[${index}].error`);
  }
  const currentRunId = requireNullableNonNegativeInteger(state.current_run_id, "current_run_id");
  if (currentRunId !== null && !runIds.has(currentRunId)) throw new TransportSnapshotValidationError("current_run_id", "已存在的 Run ID 或 null");
  if (activeRunIds.length > 1) throw new TransportSnapshotValidationError("runs", "最多一个 active Run");
  if (activeRunIds.length === 1 && currentRunId !== activeRunIds[0]) {
    throw new TransportSnapshotValidationError("current_run_id", "必须指向唯一的 active Run");
  }
  if (!isRecord(state.approvals) || Object.keys(state.approvals).length !== 0) throw new TransportSnapshotValidationError("approvals", "空对象");
  const ratio = state.context_usage_ratio;
  if (ratio !== null && (typeof ratio !== "number" || !Number.isFinite(ratio) || ratio < 0)) throw new TransportSnapshotValidationError("context_usage_ratio", "非负有限数字或 null");
  requireNullableNonNegativeInteger(state.context_usage_used, "context_usage_used");
  requireNullableNonNegativeInteger(state.context_window_total, "context_window_total");
  if (state.error !== null) validateError(state.error, "error");
  return state as unknown as TransportState;
}
