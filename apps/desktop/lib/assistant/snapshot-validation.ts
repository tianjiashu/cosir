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
  if (type !== "tool-call") throw new TransportSnapshotValidationError(`${path}.type`, "已知消息 part 类型");
  const allowed = ["type", "toolCallId", "toolName", "status", "args", "error", "errorCode", "presentation", "display_data", "isError", "approvalRequestId"];
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
  if (hasOwn(part, "display_data") && part.display_data !== null && !isRecord(part.display_data)) throw new TransportSnapshotValidationError(`${path}.display_data`, "对象或 null");
  if (hasOwn(part, "isError") && part.isError !== null && typeof part.isError !== "boolean") throw new TransportSnapshotValidationError(`${path}.isError`, "布尔值或 null");
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
    requireExactKeys(run, ["runId", "status", "endReason", "messages", "usage"], `runs[${index}]`);
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
  if (state.error !== null) {
    const error = requireRecord(state.error, "error");
    requireExactKeys(error, ["code", "message", "retryable"], "error");
    requireString(error.code, "error.code");
    requireString(error.message, "error.message");
    if (typeof error.retryable !== "boolean") throw new TransportSnapshotValidationError("error.retryable", "布尔值");
  }
  return state as unknown as TransportState;
}
