/**
 * SSE 帧解析器缺陷验证测试。
 *
 * 目标：验证 `parseSSEFrame` 在以下场景下的行为是否符合 SSE 规范（W3C EventSource）：
 * - 多行 `data:`（同一帧内多条 data 行应按换行拼接，而非后者覆盖前者）
 * - data 值本身为空串（合法帧，但当前实现会整帧丢弃）
 * - `data: ` 后带前导空格（规范只吃掉一个前导空格，不应 trim 尾随有效空白）
 *
 * @module tests/sseParser.multiline
 */

import { describe, expect, it } from "vitest";
import { parseSSEFrame } from "@/services/sseParser";

describe("parseSSEFrame 多行 data", () => {
  it("单行 data 正常解析（基线）", () => {
    const frame = parseSSEFrame('event: run_started\ndata: {"a":1}');
    expect(frame).toEqual({ eventType: "run_started", data: '{"a":1}' });
  });

  it("同一帧内多条 data 行应按换行拼接，而非只保留最后一行", () => {
    // SSE 规范：多条 data 行以 "\n" 连接构成完整 data 字段。
    // 后端若把长 JSON（如包含换行的 content）按行拆成多条 data，将命中此路径。
    const text = 'event: tool_call_finished\ndata: {"content":"line1\ndata: line2"}';
    const frame = parseSSEFrame(text);
    expect(frame?.data).toBe('{"content":"line1\nline2"}');
  });

  it("多条 data 行时不得丢失前面的分片导致 JSON 截断", () => {
    const text = "event: x\ndata: {\ndata:   \"k\": 1\ndata: }";
    const frame = parseSSEFrame(text);
    expect(frame).not.toBeNull();
    expect(() => JSON.parse(frame!.data)).not.toThrow();
  });

  it("data 值为空串是合法帧，不应整帧返回 null", () => {
    const frame = parseSSEFrame("event: ping\ndata:");
    expect(frame).toEqual({ eventType: "ping", data: "" });
  });
});
