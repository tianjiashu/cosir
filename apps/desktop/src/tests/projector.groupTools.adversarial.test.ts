/**
 * projector 与 groupConsecutiveTools 对抗性 / 边界测试。
 *
 * 覆盖任务要求：
 * 1. projectTimelineIncrementally 对同一批 events 多次调用（幂等）结果一致。
 * 2. selectVisibleEntries 在 turn 终态（status 非 pending/running）下折叠 pending 块、
 *    不残留展开态；活动态 flush。
 * 3. groupConsecutiveTools：连续 ≥2 tool 聚合为 toolGroup 且 groupId 稳定；单条 tool 复用
 *    原 entry 引用（memo 友好）；被非 tool 条目打断的段分别处理；空/超大/重复边界。
 * 4. 边界/异常：空 events、单事件、重复 event_id、超大 delta。
 *
 * @module tests/projector.groupTools.adversarial
 */
import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";
import { groupConsecutiveTools } from "@/services/timeline/groupTools";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

let seq = 0;
function makeEvent(eventType: string, payload: Record<string, unknown>, turnId = "turn-1"): RuntimeEvent {
  seq += 1;
  return {
    event_id: `evt-${seq}`,
    task_id: "task-1",
    turn_id: turnId,
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as unknown as RuntimeEvent;
}

function toolItems(entries: TurnTimelineEntry[]) {
  return entries.filter((e): e is Extract<TurnTimelineEntry, { kind: "tool" }> => e.kind === "tool");
}
function assistantText(entries: TurnTimelineEntry[]): string {
  return entries
    .filter((e): e is Extract<TurnTimelineEntry, { kind: "assistant" }> => e.kind === "assistant")
    .map((e) => e.content)
    .join("");
}
function makeTool(index: number, callId?: string): TurnTimelineEntry {
  return {
    kind: "tool",
    item: {
      eventId: `evt-tool-${index}`,
      toolName: `tool-${index}`,
      status: "running",
      arguments: undefined,
      callId,
      display: undefined,
      requestSummary: `summary-${index}`,
    },
  } as TurnTimelineEntry;
}
function makeAssistant(id: string): TurnTimelineEntry {
  return { kind: "assistant", eventId: id, content: "hi" } as TurnTimelineEntry;
}

describe("projectTimelineIncrementally 幂等性", () => {
  // 测试目的：同一批 events 连续多次全量投影，结果稳定一致（引用可不同，但结构与内容一致）。
  // 可能发现的缺陷：每次投影都 concat 新 pending 块、或工具条目引用逐次漂移导致渲染风暴。
  it("同一批 events 重复全量投影，assistant 文本与工具数量一致", () => {
    const events = [
      makeEvent("model_output_delta", { text: "Hello " }),
      makeEvent("model_output_delta", { text: "world" }),
      makeEvent("tool_call_started", { tool_name: "read_file", tool_call_id: "c1", arguments: {} }),
      makeEvent("tool_call_finished", { tool_name: "read_file", tool_call_id: "c1", status: "completed", content: "x" }),
    ];
    const s1 = projectTimelineIncrementally(createTimelineProjectorState(), events);
    const e1 = selectVisibleEntries(s1);
    const s2 = projectTimelineIncrementally(createTimelineProjectorState(), events);
    const e2 = selectVisibleEntries(s2);
    expect(assistantText(e1)).toBe(assistantText(e2));
    expect(toolItems(e1).length).toBe(toolItems(e2).length);
    expect(e1.length).toBe(e2.length);
  });

  // 测试目的：增量分两批到达（先 delta 后半）与一次性全量到达的结果应一致（时序无关）。
  // 可能发现的缺陷：分批到达导致 pending 块重复定稿、或工具条目重复。
  it("分批到达与一次到达的累计结果一致", () => {
    const full = [
      makeEvent("model_output_delta", { text: "Hello " }),
      makeEvent("model_output_delta", { text: "world" }),
      makeEvent("tool_call_started", { tool_name: "read_file", tool_call_id: "c1", arguments: {} }),
    ];
    const sFull = projectTimelineIncrementally(createTimelineProjectorState(), full);
    const eFull = selectVisibleEntries(sFull);

    let sInc = createTimelineProjectorState();
    sInc = projectTimelineIncrementally(sInc, full.slice(0, 2));
    sInc = projectTimelineIncrementally(sInc, full.slice(2));
    const eInc = selectVisibleEntries(sInc);

    expect(assistantText(eFull)).toBe(assistantText(eInc));
    expect(toolItems(eFull).length).toBe(toolItems(eInc).length);
  });

  // 测试目的：重复 event_id 到达（回放/重连）应被幂等去重，不重复投影。
  // 可能发现的缺陷：重复 event_id 导致 assistant 文本加倍或工具条目重复。
  it("重复 event_id 重放不产生重复条目", () => {
    const e1 = makeEvent("model_output_delta", { text: "abc" });
    const e2 = makeEvent("tool_call_finished", { tool_name: "read_file", tool_call_id: "c1", status: "completed", content: "x" });
    let s = projectTimelineIncrementally(createTimelineProjectorState(), [e1, e2]);
    const first = selectVisibleEntries(s);
    // 重新投一遍完全相同的事件（相同 event_id）
    s = projectTimelineIncrementally(s, [e1, e2]);
    const second = selectVisibleEntries(s);
    expect(assistantText(second)).toBe(assistantText(first));
    expect(toolItems(second).length).toBe(toolItems(first).length);
  });
});

describe("selectVisibleEntries 终态折叠 pending 块", () => {
  // 测试目的：turn 活动态时，未 flush 的 pending thinking 块以 streaming 推入（可见）。
  it("活动态下 pending thinking 块可见且标记 streaming", () => {
    let s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_thinking_delta", { text: "我在思考" }),
    ]);
    const active = selectVisibleEntries(s, true);
    const thinking = active.find((e) => e.kind === "thinking");
    expect(thinking).toBeDefined();
    if (thinking && thinking.kind === "thinking") {
      expect(thinking.content).toBe("我在思考");
      expect(thinking.streaming).toBe(true);
    }
  });

  // 测试目的：turn 终态（如 completed）下，即便 stateRef 仍有 pending 残留（SSE 断开后
  // flushPending 永不触发），也按定稿态折叠，不再以 streaming 展开。
  // 可能发现的缺陷：终态下仍残留 streaming 标记的思考块（与后续 turn 重叠挤压空间）。
  it("终态下 pending thinking 不残留 streaming 标记", () => {
    let s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_thinking_delta", { text: "半截思考" }),
    ]);
    const terminal = selectVisibleEntries(s, false);
    const thinking = terminal.find((e) => e.kind === "thinking");
    expect(thinking).toBeDefined();
    if (thinking && thinking.kind === "thinking") {
      // 终态不应再标 streaming（折叠回定稿态）
      expect(thinking.streaming).toBeUndefined();
    }
  });

  // 测试目的：活动态下 pending assistant delta 可见；终态下同样可见但不标 streaming。
  it("终态下 pending assistant delta 可见且不标 streaming", () => {
    let s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_output_delta", { text: "正在生成" }),
    ]);
    const terminal = selectVisibleEntries(s, false);
    const assistant = terminal.find((e) => e.kind === "assistant");
    expect(assistant).toBeDefined();
    if (assistant && assistant.kind === "assistant") {
      expect(assistant.streaming).toBeUndefined();
      expect(assistant.content).toBe("正在生成");
    }
  });

  // 测试目的：无 pending 时 selectVisibleEntries 直接返回 entries 原引用（零分配，memo 友好）。
  it("无 pending 时返回 entries 原引用（引用稳定）", () => {
    let s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("tool_call_finished", { tool_name: "read_file", tool_call_id: "c1", status: "completed", content: "x" }),
    ]);
    const entries = selectVisibleEntries(s, true);
    expect(entries).toBe(s.entries);
  });
});

describe("groupConsecutiveTools 边界", () => {
  it("空 entries 返回空数组", () => {
    expect(groupConsecutiveTools([])).toEqual([]);
  });

  it("单个 tool 条目直接复用原 entry 引用（不套壳）", () => {
    const entries = [makeTool(1, "c1")];
    const result = groupConsecutiveTools(entries);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(entries[0]);
    expect(result[0].kind).toBe("tool");
  });

  it("≥2 连续 tool 聚合为一个 toolGroup，groupId 由首项 callId 决定", () => {
    const entries = [makeTool(1, "c1"), makeTool(2, "c2")];
    const result = groupConsecutiveTools(entries);
    expect(result).toHaveLength(1);
    expect(result[0].kind).toBe("toolGroup");
    if (result[0].kind === "toolGroup") {
      expect(result[0].groupId).toBe("grp-c1");
      expect(result[0].items).toHaveLength(2);
    }
  });

  // 测试目的：callId 缺失时回退到「首项在原始 entries 中的全局索引」，保证流式期 key 稳定。
  it("callId 缺失时回退稳定索引生成 groupId", () => {
    const entries: TurnTimelineEntry[] = [makeTool(1), makeTool(2)];
    const result = groupConsecutiveTools(entries);
    expect(result[0].kind).toBe("toolGroup");
    if (result[0].kind === "toolGroup") {
      expect(result[0].groupId).toBe("grp-idx-0");
    }
  });

  // 测试目的：被非 tool 条目（assistant）打断的两段分别聚合，各自独立 groupId。
  it("被 assistant 打断的连续段分离为多个工具组", () => {
    const entries = [
      makeTool(1, "c1"),
      makeTool(2, "c2"),
      makeAssistant("a1"),
      makeTool(3, "c3"),
    ];
    const result = groupConsecutiveTools(entries);
    // 第 3 个 tool 单独出现（后无同伴），按规则作为单条 tool 不套壳；
    // 仅「≥2 连续 tool」才聚合。故中段分离为 [toolGroup, assistant, tool]。
    expect(result.map((e) => e.kind)).toEqual(["toolGroup", "assistant", "tool"]);
    if (result[0].kind === "toolGroup") expect(result[0].groupId).toBe("grp-c1");
    if (result[2].kind === "tool") expect(result[2]).toBe(entries[3]);
  });

  // 测试目的：流式期组从 2→3 个工具，groupId 保持稳定（key 不抖，不重置展开态）。
  it("流式期组从 2→3 增长时 groupId 稳定", () => {
    const two = [makeTool(1, "c1"), makeTool(2, "c2")];
    const three = [makeTool(1, "c1"), makeTool(2, "c2"), makeTool(3, "c3")];
    const g2 = groupConsecutiveTools(two)[0];
    const g3 = groupConsecutiveTools(three)[0];
    expect(g2.kind).toBe("toolGroup");
    expect(g3.kind).toBe("toolGroup");
    if (g2.kind === "toolGroup" && g3.kind === "toolGroup") {
      expect(g3.groupId).toBe(g2.groupId);
    }
  });
});

describe("projector 边界 / 异常", () => {
  // 测试目的：空 events 调用不应抛错，且返回原 state 引用（零分配）。
  it("空 events 返回原 state 引用且不抛错", () => {
    const s0 = createTimelineProjectorState();
    const s1 = projectTimelineIncrementally(s0, []);
    expect(s1).toBe(s0);
  });

  // 测试目的：单条事件（final_response）能产出 assistant 条目。
  it("单条 final_response 产出定稿 assistant", () => {
    const s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("final_response", { text: "最终回复" }),
    ]);
    const entries = selectVisibleEntries(s);
    const assistant = entries.find((e) => e.kind === "assistant");
    expect(assistant).toBeDefined();
    if (assistant && assistant.kind === "assistant") {
      expect(assistant.content).toBe("最终回复");
    }
  });

  // 测试目的：超大单段 delta（模拟一次性超长响应）正确累积为整段文本，不截断。
  it("超大单段 delta 完整累积", () => {
    const big = "x".repeat(50000);
    const s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_output_delta", { text: big }),
    ]);
    expect(assistantText(selectVisibleEntries(s))).toBe(big);
  });

  // 测试目的：final_response 在有 delta 流过时不重复追加正文（避免两段重复展示）。
  it("final_response 在 delta 之后不重复追加正文", () => {
    const s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_output_delta", { text: "hello" }),
      makeEvent("final_response", { text: "hello" }),
    ]);
    const assistants = s.entries.filter((e) => e.kind === "assistant");
    expect(assistants).toHaveLength(1);
  });

  // 测试目的：final_response 在 delta 已流过后到达（文本不同）时，不覆盖/重复追加——
  // 设计语义是「final_response 仅作无 delta 时的兜底」，已有流式 delta 时以 delta 文本为准，
  // 且迟到 delta（final 之后）被丢弃，最终 assistant 只出现一次。
  it("final_response 在 delta 已流过后到达：以 delta 文本为准且只出现一次", () => {
    let s = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_output_delta", { text: "streamed" }),
    ]);
    s = projectTimelineIncrementally(s, [
      makeEvent("final_response", { text: "authoritative" }),
    ]);
    // 已有 delta 流过，final_response 不追加兜底文本；最终文本为已流的 delta。
    const text = assistantText(selectVisibleEntries(s));
    expect(text).toBe("streamed");
    const assistants = s.entries.filter((e) => e.kind === "assistant");
    expect(assistants).toHaveLength(1);
  });
});
