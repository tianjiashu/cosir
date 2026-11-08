/** 工具展示数据的读取小工具：只做形状收口，不解释语义。 */

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

export function readArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function readRecords(value: unknown): Record<string, unknown>[] {
  return readArray(value).map(asRecord);
}

export function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

/** 有实际内容的字符串；空串与纯空白按没有。 */
export function readText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}
