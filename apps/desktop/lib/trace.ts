"use client";

export type TraceId = string;

/** 生成 128-bit、无分隔符的小写十六进制 trace id。 */
export function newTraceId(): TraceId {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}
