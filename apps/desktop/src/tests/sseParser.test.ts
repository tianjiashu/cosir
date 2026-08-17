/**
 * createSSEFrameParser（基于 eventsource-parser 的 SSE 帧分发器）行为测试。
 *
 * 验证替换手写 parseSSEFrame 后保持相同业务语义：
 * - 有 event 名 + 至少一条 data 行 → 分发 { eventType, data }
 * - 多条 data 行按 \n 拼接
 * - data 为空串是合法帧（心跳 / ping），不得丢弃
 * - 缺 event 名 → 不分发（库对无 event 名的帧也会 dispatch，工厂内过滤）
 * - 缺 data 行（只有 event 行）→ 不分发
 * - 跨分片喂入：一帧拆成多段 feed 仍解析出完整帧（库原生缓冲）
 * - CRLF（\r\n）行结束
 * - 注释行（: 开头）被忽略
 * - EOF flush（feed("\n\n")）促使缓冲区内已完整帧被分发；reset 后同一 parser 可复用
 *
 * @module tests/sseParser
 */

import { describe, expect, it } from "vitest";
import { createSSEFrameParser, type ParsedSSEFrame } from "@/services/sseParser";

/**
 * 构造一个收集分发帧的测试用 parser。
 *
 * @returns 包含 parser 与已分发帧列表的对象。
 *
 * @sideeffect 无。
 */
function collect(): { parser: ReturnType<typeof createSSEFrameParser>; frames: ParsedSSEFrame[] } {
  const frames: ParsedSSEFrame[] = [];
  const parser = createSSEFrameParser((frame) => {
    frames.push(frame);
  });
  return { parser, frames };
}

describe("createSSEFrameParser", () => {
  it("单帧 event + data 解析为 { eventType, data }", () => {
    const { parser, frames } = collect();
    parser.feed('event: run_started\ndata: {"a":1}\n\n');
    expect(frames).toEqual([{ eventType: "run_started", data: '{"a":1}' }]);
  });

  it("同一帧内多条 data 行按换行拼接，而非只保留最后一行", () => {
    // SSE 规范：多条 data 行以 "\n" 连接构成完整 data 字段。
    // 后端若把长 JSON（如包含换行的 content）按行拆成多条 data，将命中此路径。
    const { parser, frames } = collect();
    parser.feed('event: tool_call_finished\ndata: {"content":"line1\ndata: line2"}\n\n');
    expect(frames).toEqual([
      { eventType: "tool_call_finished", data: '{"content":"line1\nline2"}' },
    ]);
  });

  it("多条 data 行时不得丢失前面的分片导致 JSON 截断", () => {
    const { parser, frames } = collect();
    parser.feed('event: x\ndata: {\ndata:   "k": 1\ndata: }\n\n');
    expect(frames).toHaveLength(1);
    expect(() => JSON.parse(frames[0].data)).not.toThrow();
  });

  it("data 值为空串是合法帧，不应被丢弃（心跳 / ping）", () => {
    const { parser, frames } = collect();
    parser.feed("event: ping\ndata:\n\n");
    expect(frames).toEqual([{ eventType: "ping", data: "" }]);
  });

  it("缺 event 名的帧被丢弃（库会 dispatch event 为空的帧，工厂内过滤）", () => {
    const { parser, frames } = collect();
    parser.feed('data: {"a":1}\n\n');
    expect(frames).toEqual([]);
  });

  it("缺 data 行（只有 event 行）的帧被丢弃", () => {
    const { parser, frames } = collect();
    parser.feed("event: foo\n\n");
    expect(frames).toEqual([]);
  });

  it("跨分片喂入：把一帧拆成两段 feed 仍解析出完整帧", () => {
    const { parser, frames } = collect();
    parser.feed('event: run_started\nda');
    parser.feed('ta: {"a":1}\n\n');
    expect(frames).toEqual([{ eventType: "run_started", data: '{"a":1}' }]);
  });

  it("CRLF（\\r\\n）行结束同样解析", () => {
    const { parser, frames } = collect();
    parser.feed('event: run_started\r\ndata: {"a":1}\r\n\r\n');
    expect(frames).toEqual([{ eventType: "run_started", data: '{"a":1}' }]);
  });

  it("注释行（: 开头）被忽略", () => {
    const { parser, frames } = collect();
    parser.feed(": heartbeat comment\nevent: run_started\ndata: {\"a\":1}\n\n");
    expect(frames).toEqual([{ eventType: "run_started", data: '{"a":1}' }]);
  });

  it("EOF flush：feed(\"\\n\\n\") 促使缓冲区内已完整帧被分发，reset 后 parser 可复用", () => {
    const { parser, frames } = collect();
    // 流在完整帧后结束但没有尾随空行：帧仍残留在解析器缓冲区中
    parser.feed('event: run_started\ndata: {"a":1}');
    // EOF：模拟标准流终止空行，促使缓冲区内已完整帧被分发
    parser.feed("\n\n");
    parser.reset();
    expect(frames).toEqual([{ eventType: "run_started", data: '{"a":1}' }]);

    // reset 后同一 parser 可复用于解析新流
    parser.feed('event: run_finished\ndata: {"ok":true}\n\n');
    expect(frames).toEqual([
      { eventType: "run_started", data: '{"a":1}' },
      { eventType: "run_finished", data: '{"ok":true}' },
    ]);
  });
});
