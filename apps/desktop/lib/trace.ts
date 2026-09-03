"use client";

export type TraceId = string;

let activeTraceId: TraceId | null = null;

/** 生成 128-bit、无分隔符的小写十六进制 trace id。 */
export function newTraceId(): TraceId {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export function isTraceId(value: string | null | undefined): value is TraceId {
  return value !== undefined && value !== null && /^[0-9a-f]{32}$/.test(value);
}

export function setActiveTraceId(traceId: TraceId | null): void {
  activeTraceId = traceId;
}

export function getActiveTraceId(): TraceId | null {
  return activeTraceId;
}
