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

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new TransportSnapshotValidationError(path, "对象");
  return value;
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== "string") throw new TransportSnapshotValidationError(path, "字符串");
  return value;
}

function requireFiniteNonNegativeNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new TransportSnapshotValidationError(path, "非负有限数字");
  }
  return value;
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

function validateToolPart(value: Record<string, unknown>, path: string): void {
  requireString(value.toolCallId, `${path}.toolCallId`);
  requireString(value.toolName, `${path}.toolName`);
  const status = requireString(value.status, `${path}.status`);
  if (!["pending", "running", "completed", "failed", "cancelled"].includes(status)) {
    throw new TransportSnapshotValidationError(`${path}.status`, "受支持的工具状态");
  }
  if (hasOwn(value, "args") && !isRecord(value.args)) {
    throw new TransportSnapshotValidationError(`${path}.args`, "对象");
  }
  if (hasOwn(value, "presentation") && !isRecord(value.presentation)) {
    throw new TransportSnapshotValidationError(`${path}.presentation`, "对象");
  }
  if (hasOwn(value, "data") && value.data !== null && !isRecord(value.data)) {
    throw new TransportSnapshotValidationError(`${path}.data`, "对象或 null");
  }
  if (hasOwn(value, "errorCode") && value.errorCode !== undefined && typeof value.errorCode !== "string") {
    throw new TransportSnapshotValidationError(`${path}.errorCode`, "字符串");
  }
  if (hasOwn(value, "isError") && typeof value.isError !== "boolean") {
    throw new TransportSnapshotValidationError(`${path}.isError`, "布尔值");
  }
}

function validatePart(value: unknown, path: string): void {
  const part = requireRecord(value, path);
  const type = requireString(part.type, `${path}.type`);
  if (type === "text" || type === "reasoning") {
    requireString(part.text, `${path}.text`);
    if (hasOwn(part, "status") && part.status !== undefined && part.status !== "running" && part.status !== "completed") {
      throw new TransportSnapshotValidationError(`${path}.status`, "running 或 completed");
    }
    if (type === "reasoning" && hasOwn(part, "unstable_summary") && part.unstable_summary !== undefined && typeof part.unstable_summary !== "string") {
      throw new TransportSnapshotValidationError(`${path}.unstable_summary`, "字符串");
    }
    return;
  }
  if (type === "tool-call") {
    validateToolPart(part, path);
    return;
  }
  throw new TransportSnapshotValidationError(`${path}.type`, "已知消息 part 类型");
}

function validateMessage(value: unknown, index: number): void {
  const path = `messages[${index}]`;
  const message = requireRecord(value, path);
  requireString(message.id, `${path}.id`);
  if (message.role !== "user" && message.role !== "assistant") {
    throw new TransportSnapshotValidationError(`${path}.role`, "user 或 assistant");
  }
  requireString(message.status, `${path}.status`);
  if (!hasOwn(message, "endReason") || (message.endReason !== null && typeof message.endReason !== "string")) {
    throw new TransportSnapshotValidationError(`${path}.endReason`, "字符串或 null");
  }
  if (!Array.isArray(message.parts)) throw new TransportSnapshotValidationError(`${path}.parts`, "数组");
  message.parts.forEach((part, partIndex) => validatePart(part, `${path}.parts[${partIndex}]`));
}

export function parseTransportState(value: unknown): TransportState {
  const state = requireRecord(value, "snapshot");
  requireExactKeys(state, ["messages", "run", "approvals", "context_usage", "usage", "error"], "snapshot");

  if (!Array.isArray(state.messages)) throw new TransportSnapshotValidationError("messages", "数组");
  state.messages.forEach(validateMessage);

  const run = requireRecord(state.run, "run");
  requireExactKeys(run, ["runId", "status"], "run");
  if (run.runId !== null && (!Number.isInteger(run.runId) || (run.runId as number) < 0)) {
    throw new TransportSnapshotValidationError("run.runId", "非负整数或 null");
  }
  const runStatus = requireString(run.status, "run.status");
  if (![
    "idle",
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
  ].includes(runStatus)) {
    throw new TransportSnapshotValidationError("run.status", "受支持的运行状态");
  }

  const approvals = requireRecord(state.approvals, "approvals");
  if (Object.keys(approvals).length !== 0) {
    throw new TransportSnapshotValidationError("approvals", "空对象");
  }

  requireFiniteNonNegativeNumber(state.context_usage, "context_usage");
  const usage = requireRecord(state.usage, "usage");
  requireExactKeys(usage, USAGE_KEYS, "usage");
  USAGE_KEYS.forEach((key) => {
    const count = usage[key];
    if (!Number.isInteger(count) || (count as number) < 0) {
      throw new TransportSnapshotValidationError(`usage.${key}`, "非负整数");
    }
  });

  if (state.error !== null) {
    const error = requireRecord(state.error, "error");
    requireExactKeys(error, ["code", "message", "retryable"], "error");
    requireString(error.code, "error.code");
    requireString(error.message, "error.message");
    if (typeof error.retryable !== "boolean") throw new TransportSnapshotValidationError("error.retryable", "布尔值");
  }

  return state as unknown as TransportState;
}
