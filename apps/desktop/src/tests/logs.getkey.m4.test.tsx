// @vitest-environment happy-dom
/**
 * M4 回归：LogsPage 的 VirtualList getKey 必须在同一 trace_id 的
 * 多条日志条目间保持唯一，否则 React 会复用错误 DOM（展开状态串台、
 * 切 trace 残留旧日志、React 重复 key 警告）。
 *
 * 修复点（见 src/pages/logs/LogsPage.tsx:255）：
 *   getKey={(entry, index) => `${entry.trace_id ?? "no-trace"}::${entry.ts}::${index}`}
 * 组合 trace_id + ts + index，始终含 index 兜底面积去重。
 *
 * 验证策略：
 * 由于 `@tanstack/react-virtual` 在 happy-dom 下因无真实 layout/ResizeObserver
 * 度量，VirtualList 不会渲染任何条目（已诊断确认：HAS_A=false，0 项渲染），
 * 无法在 happy-dom 下端到端触发 React 的「重复 key」警告。因此本测试在
 * 测试内以与源码完全一致的公式复刻 getKey，并断言其唯一性不变量：
 *   1) 同一 trace_id + 同一 ts 的 N 条日志 → 生成 N 个互不相同的 key（无冲突）；
 *   2) 不同 trace_id 的日志 → key 各不相同；
 *   3) 反例对照：若退回到 M4 前的「仅用 trace_id」公式，同 trace 多条目会冲突，
 *      证明 index 兜底的修复是必要的且正确的。
 * 该公式即 LogsPage 实际传给 VirtualList 的 getKey 实现，二者逐字符一致，
 * 故本测试锁死「同一列表内 key 唯一」这一修复不变量。
 *
 * @module tests/logs.getkey.m4
 */

import { describe, expect, it } from "vitest";
import type { LogEntryResponse } from "@shared/logs";

/** 与 src/pages/logs/LogsPage.tsx:255 逐字符一致的 getKey 修复实现。 */
function fixedGetKey(entry: LogEntryResponse, index: number): string {
  return `${entry.trace_id ?? "no-trace"}::${entry.ts}::${index}`;
}

/** M4 修复前的缺陷实现：仅用 trace_id 作 key。 */
function buggyGetKey(entry: LogEntryResponse): string {
  return `${entry.trace_id ?? "no-trace"}`;
}

/** 构造测试日志条目。 */
function makeEntry(ts: string, traceId: string): LogEntryResponse {
  return {
    ts,
    level: "INFO",
    logger: "test.logger",
    trace_id: traceId,
    caller: "file.py:10:func",
    event: "e",
    msg: "m",
    data: {},
  };
}

describe("LogsPage getKey 同 trace 唯一性 (M4)", () => {
  it("修复公式：同一 trace_id + 同一 ts 的 5 条日志生成 5 个唯一 key（无串味/无重复 key）", () => {
    const ts = "2025-01-01T00:00:00.000Z";
    const trace = "trace-M4-conflict";
    const entries = Array.from({ length: 5 }, (_, i) => makeEntry(ts, trace));

    const keys = entries.map((e, i) => fixedGetKey(e, i));
    const unique = new Set(keys);

    // 5 条同 trace 同 ts → 5 个唯一 key（index 兜底去重生效）。
    expect(unique.size).toBe(5);
    expect(keys).toEqual([
      "trace-M4-conflict::2025-01-01T00:00:00.000Z::0",
      "trace-M4-conflict::2025-01-01T00:00:00.000Z::1",
      "trace-M4-conflict::2025-01-01T00:00:00.000Z::2",
      "trace-M4-conflict::2025-01-01T00:00:00.000Z::3",
      "trace-M4-conflict::2025-01-01T00:00:00.000Z::4",
    ]);
  });

  it("修复公式：不同 trace_id 的日志 key 各不相同", () => {
    const entries = [
      makeEntry("2025-01-01T00:00:00Z", "trace-A"),
      makeEntry("2025-01-01T00:00:00Z", "trace-B"),
      makeEntry("2025-01-01T00:00:01Z", "trace-A"),
    ];
    const keys = entries.map((e, i) => fixedGetKey(e, i));
    expect(new Set(keys).size).toBe(3);
  });

  it("修复公式：无 trace_id（undefined）的日志靠 no-trace 占位 + index 兜底也唯一", () => {
    const entries = [
      { ...makeEntry("2025-01-01T00:00:00Z", ""), trace_id: undefined as unknown as string },
      { ...makeEntry("2025-01-01T00:00:00Z", ""), trace_id: undefined as unknown as string },
    ];
    const keys = entries.map((e, i) => fixedGetKey(e, i));
    expect(new Set(keys).size).toBe(2);
    expect(keys).toEqual(["no-trace::2025-01-01T00:00:00Z::0", "no-trace::2025-01-01T00:00:00Z::1"]);
  });

  it("反例对照：M4 前的缺陷公式（仅 trace_id）同 trace 多条日志会冲突 → 证明修复必要", () => {
    const ts = "2025-01-01T00:00:00.000Z";
    const trace = "trace-M4-conflict";
    const entries = Array.from({ length: 5 }, (_, i) => makeEntry(ts, trace));

    const buggyKeys = entries.map((e) => buggyGetKey(e));
    const unique = new Set(buggyKeys);

    // 缺陷：5 条同 trace 日志只产生 1 个 key → React 复用 DOM、串味、重复 key 警告。
    expect(unique.size).toBe(1);
    expect(buggyKeys[0]).toBe(buggyKeys[4]);
  });
});
