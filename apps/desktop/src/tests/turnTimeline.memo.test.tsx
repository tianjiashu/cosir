// @vitest-environment happy-dom
/**
 * TurnTimeline 性能回归测试。
 *
 * 覆盖方案 A（叶子组件 memo）+ 方案 B（TimelineEntry memo 子组件）的正确性不变量：
 * 1. projector 增量投影时必须保留「未变化条目」的旧引用（方案 B 成立的根本前提）；
 * 2. 流式追加 delta 时，TurnTimeline 不应让未变化的工具/消息条目重新渲染内部组件
 *    （memo 命中，Markdown 不重复解析）——否则历史流每帧卡顿。
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { act } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
} from "@/services/timeline/projector";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

// openFileInEditor 来自 Tauri 宿主模块，测试环境无 Tauri，mock 掉以免导入崩溃。
vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

// 把渲染叶子组件替换为带调用计数的 spy，用于断言 memo 是否命中。
const renderCounts = {
  AgentMessage: 0,
  ThinkingBlock: 0,
  ToolCallCard: 0,
  TerminalCallCard: 0,
  StatusBadge: 0,
};
vi.mock("@/components/chat/AgentMessage", () => ({
  AgentMessage: () => {
    renderCounts.AgentMessage += 1;
    return null;
  },
}));
vi.mock("@/components/chat/ThinkingBlock", () => ({
  ThinkingBlock: () => {
    renderCounts.ThinkingBlock += 1;
    return null;
  },
}));
vi.mock("@/components/chat/ToolCallCard", () => ({
  ToolCallCard: () => {
    renderCounts.ToolCallCard += 1;
    return null;
  },
}));
vi.mock("@/components/chat/TerminalCallCard", () => ({
  TerminalCallCard: () => {
    renderCounts.TerminalCallCard += 1;
    return null;
  },
}));
vi.mock("@/components/chat/StatusBadge", () => ({
  StatusBadge: () => {
    renderCounts.StatusBadge += 1;
    return null;
  },
}));
// UserMessage 不参与断言，保留真实组件即可（纯文本，无重开销）。
vi.mock("@/components/chat/UserMessage", () => ({
  UserMessage: () => null,
}));

function makeEvent(
  eventId: string,
  sequence: number,
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload,
  } as unknown as RuntimeEvent;
}

function makeTurn(): TurnRecord {
  return {
    turn_id: "turn-1",
    task_id: "task-1",
    input_text: "user input",
    status: "running",
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TurnRecord;
}

describe("projector 增量投影的条目引用稳定性（方案 B 前提）", () => {
  it("未变化的工具条目在收到无关 delta 时保留旧引用", () => {
    let state = createTimelineProjectorState();

    // 帧1：工具调用 started
    const started = makeEvent("tool-1", 1, "tool_call_started", {
      tool_name: "search_files",
      tool_call_id: "call-1",
      arguments: { pattern: "*.ts" },
    });
    state = projectTimelineIncrementally(state, [started]);
    const entriesAfterStart = selectVisibleEntries(state);
    expect(entriesAfterStart).toHaveLength(1);
    const toolRef1 = entriesAfterStart[0];

    // 帧2：仅追加一段助手文本 delta，工具条目本身未变
    const delta = makeEvent("d-1", 2, "model_output_delta", { text: "hello " });
    state = projectTimelineIncrementally(state, [delta]);
    const entriesAfterDelta = selectVisibleEntries(state);
    // 工具条目 + 正在累积的 assistant（pending，不在 entries 定稿里）
    const toolRef2 = entriesAfterDelta.find((e) => e.kind === "tool");
    expect(toolRef2).toBe(toolRef1);
  });

  it("同一 callId 的 finished 事件只更新该工具条目、不影响其它条目引用", () => {
    let state = createTimelineProjectorState();
    const started = makeEvent("t1", 1, "tool_call_started", {
      tool_name: "search_files",
      tool_call_id: "c1",
      arguments: {},
    });
    const started2 = makeEvent("t2", 2, "tool_call_started", {
      tool_name: "read_file",
      tool_call_id: "c2",
      arguments: {},
    });
    state = projectTimelineIncrementally(state, [started, started2]);
    const before = selectVisibleEntries(state);
    expect(before).toHaveLength(2);
    const refTool1 = before[0];
    const refTool2 = before[1];

    // finished 仅针对 c1，c2 不应受影响
    const finished = makeEvent("f1", 3, "tool_call_finished", {
      tool_call_id: "c1",
      status: "completed",
      result: "ok",
    });
    state = projectTimelineIncrementally(state, [finished]);
    const after = selectVisibleEntries(state);
    expect(after[0]).not.toBe(refTool1); // c1 更新 → 新引用
    expect(after[1]).toBe(refTool2); // c2 未变 → 旧引用复用
  });
});

describe("TurnTimeline 流式期间未变化条目不重新渲染（方案 A+B 集成）", () => {
  it("追加助手 delta 时，已渲染的工具卡片计数不增加（memo 命中跳过）", () => {
    const turn = makeTurn();
    const toolStarted = makeEvent("tool-x", 1, "tool_call_started", {
      tool_name: "search_files",
      tool_call_id: "cx",
      arguments: { pattern: "*.ts" },
    });
    const delta1 = makeEvent("d1", 2, "model_output_delta", { text: "分析中" });

    const { rerender } = render(<TurnTimeline turn={turn} events={[toolStarted]} />);
    const toolCallsAfterFirstRender = renderCounts.ToolCallCard;
    const agentCallsAfterFirstRender = renderCounts.AgentMessage;
    expect(toolCallsAfterFirstRender).toBeGreaterThan(0);

    // 第二帧：保留工具事件引用不变 + 追加助手 delta（模拟流式追加）
    act(() => {
      rerender(<TurnTimeline turn={turn} events={[toolStarted, delta1]} />);
    });

    // 工具卡片条目未变化（同一 event 引用）→ 不应被重新渲染
    expect(renderCounts.ToolCallCard).toBe(toolCallsAfterFirstRender);
    // assistant 条目新增（或更新）→ AgentMessage 至少再渲染一次，证明只变化项重渲染
    expect(renderCounts.AgentMessage).toBeGreaterThan(agentCallsAfterFirstRender);
  });

  it("同一批事件引用重传时不触发任何子组件重渲染（引用稳定击穿 memo 防线）", () => {
    const turn = makeTurn();
    const toolStarted = makeEvent("tool-y", 1, "tool_call_started", {
      tool_name: "read_file",
      tool_call_id: "cy",
      arguments: {},
    });
    // 同一数组引用在两帧间复用，模拟「父组件 memo 命中前的兜底」场景：
    // 即便 TurnTimeline 因其它原因重渲染，只要 events 引用不变，下游子组件应被 memo 跳过。
    const sameEvents = [toolStarted];

    const { rerender } = render(<TurnTimeline turn={turn} events={sameEvents} />);
    const toolCalls1 = renderCounts.ToolCallCard;
    const agentCalls1 = renderCounts.AgentMessage;

    // 用完全相同的 events 数组引用再次渲染
    act(() => {
      rerender(<TurnTimeline turn={turn} events={sameEvents} />);
    });

    expect(renderCounts.ToolCallCard).toBe(toolCalls1);
    expect(renderCounts.AgentMessage).toBe(agentCalls1);
  });
});
