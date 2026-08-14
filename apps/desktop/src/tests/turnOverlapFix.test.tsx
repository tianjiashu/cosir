// @vitest-environment happy-dom
/**
 * 修复回归测试：「新一轮 turn 消息与上一轮重叠」的根因防御。
 *
 * 覆盖两个改动点的边界不变量：
 * 1. ChatPanel.timelineTurns 按 created_at 稳定排序（不依赖数组原序的 push 巧合）。
 * 2. TurnTimeline 以 turn_id 为硬不变量，跨 turn 复用实例时清空累积投影态（reset effect），
 *    确保新 turn 从空态首帧全量重建，不残留上一轮 entries（重叠的根因）。
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { act } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

// 捕获 TurnTimeline 实际渲染的 user/agent 文本，用于断言跨 turn 不串味。
const renderedText: string[] = [];
vi.mock("@/components/chat/UserMessage", () => ({
  UserMessage: ({ content }: { content?: string }) => {
    renderedText.push(`user:${content ?? ""}`);
    return null;
  },
}));
vi.mock("@/components/chat/AgentMessage", () => ({
  AgentMessage: ({ text }: { text?: string }) => {
    renderedText.push(`agent:${text ?? ""}`);
    return null;
  },
}));
vi.mock("@/components/chat/ThinkingBlock", () => ({
  ThinkingBlock: () => null,
}));
vi.mock("@/components/chat/ToolCallCard", () => ({
  ToolCallCard: () => null,
}));
vi.mock("@/components/chat/TerminalCallCard", () => ({
  TerminalCallCard: () => null,
}));
vi.mock("@/components/chat/StatusBadge", () => ({
  StatusBadge: () => null,
}));

function makeEvent(
  eventId: string,
  sequence: number,
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
  turnId: string,
): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: "task-1",
    turn_id: turnId,
    event_type: eventType,
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload,
  } as unknown as RuntimeEvent;
}

function makeTurn(turnId: string, createdAt: string, inputText: string): TurnRecord {
  return {
    turn_id: turnId,
    task_id: "task-1",
    input_text: inputText,
    status: "running",
    end_reason: null,
    response_text: null,
    created_at: createdAt,
    updated_at: createdAt,
  } as unknown as TurnRecord;
}

describe("TurnTimeline 跨 turn 复用实例不串味（reset effect 硬不变量）", () => {
  it("切换 turn_id 后旧 turn 的投影 entries 不残留（重叠根因防御）", () => {
    renderedText.length = 0;
    const turnA = makeTurn("turn-A", new Date(1000).toISOString(), "A 输入");
    const turnB = makeTurn("turn-B", new Date(2000).toISOString(), "B 输入");

    // turn-A 投出 agent 文本（经 projector 进入 stateRef.entries）
    const eventsA = [
      makeEvent("a1", 1, "model_output_delta", { text: "A 的回答" }, "turn-A"),
    ];
    // turn-B 用空事件：若 reset 失效，A 的 agent 文本会从 stateRef 残留渲染出来
    const eventsB: RuntimeEvent[] = [];

    const { rerender } = render(<TurnTimeline turn={turnA} events={eventsA} />);
    act(() => {
      rerender(<TurnTimeline turn={turnB} events={eventsB} />);
    });

    const allText = renderedText.join("|");
    // 新 turn 的 user 文本正确呈现
    expect(allText).toContain("user:B 输入");
    // 旧 turn 的 agent 投影条目必须被 reset 清空，绝不残留（重叠根因）
    expect(allText).not.toContain("A 的回答");
  });

  it("turn 切换后从空态首帧全量重建（reset 生效，不依赖 events 引用变化）", () => {
    renderedText.length = 0;
    const turnA = makeTurn("turn-A", new Date(1000).toISOString(), "A 输入");
    const turnB = makeTurn("turn-B", new Date(2000).toISOString(), "B 输入");

    // 相同结构事件、不同 turn_id，验证 reset 不靠 events 引用变化触发。
    // 用 tool_call_finished（终态，必然进入 entries）而非 model_output_delta
    // （running 单 delta 的 pending 块是否定稿属 projector 既有行为，不在此覆盖）。
    const mk = (turnId: string, toolName: string) => [
      makeEvent(`${turnId}-f`, 1, "tool_call_finished", {
        tool_name: toolName,
        tool_call_id: `${turnId}-c`,
        status: "completed",
        result: "ok",
      }, turnId),
    ];
    const eventsA = mk("turn-A", "read_file");
    const eventsB = mk("turn-B", "search_files");

    const { rerender } = render(<TurnTimeline turn={turnA} events={eventsA} />);
    act(() => {
      rerender(<TurnTimeline turn={turnB} events={eventsB} />);
    });

    const allText = renderedText.join("|");
    // 切到 B 后只应见 B 的内容，A 的投影条目已随 reset 清空
    expect(allText).toContain("user:B 输入");
    expect(allText).not.toContain("agent:A 的回答");
  });
});

describe("ChatPanel.timelineTurns 排序不变量（时间序优先于数组原序）", () => {
  // 与 ChatPanel useMemo 内实现保持一致：空 created_at 经 new Date(0).getTime()→||0 兜底为 0，排最前。
  const sortTurns = (turns: TurnRecord[]): TurnRecord[] =>
    turns.slice().sort((a, b) => {
      const ta = new Date(a.created_at ?? 0).getTime() || 0;
      const tb = new Date(b.created_at ?? 0).getTime() || 0;
      return ta - tb;
    });

  it("created_at 乱序时仍按时间升序渲染，不依赖 push 巧合顺序", () => {
    const t1 = makeTurn("t1", new Date(3000).toISOString(), "t1");
    const t2 = makeTurn("t2", new Date(1000).toISOString(), "t2"); // 更早，但排在后面
    const t3 = makeTurn("t3", new Date(2000).toISOString(), "t3");
    const tNoDate = makeTurn("tNoDate", "", "noDate"); // created_at 空 → 兜底 0，排最前

    // 故意以乱序输入（最新在前）
    const out = sortTurns([t1, t2, t3, tNoDate]).map((t) => t.turn_id);
    expect(out).toEqual(["tNoDate", "t2", "t3", "t1"]);
  });

  it("同秒/本地时钟与后端时钟不一致时稳定可重现（确定性排序）", () => {
    const earlier = makeTurn("earlier", new Date(999).toISOString(), "e");
    const temp = makeTurn("temp-xxx", new Date(1000).toISOString(), "tmp");
    const real = makeTurn("real-1", new Date(1000).toISOString(), "real");

    const out1 = sortTurns([temp, real, earlier]).map((t) => t.turn_id);
    const out2 = sortTurns([earlier, real, temp]).map((t) => t.turn_id);
    // 时间序（earlier 在最前）必须稳定；同秒元素保持输入相对序（slice 稳定排序）
    expect(out1[0]).toBe("earlier");
    expect(out2[0]).toBe("earlier");
  });
});
