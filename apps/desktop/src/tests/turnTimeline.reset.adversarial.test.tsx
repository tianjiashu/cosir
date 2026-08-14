// @vitest-environment happy-dom
/**
 * TurnTimeline 投影态 reset 与增量投影对抗性测试。
 *
 * 覆盖任务要求：
 * 1. 同一组件实例（memo 复用）下切 turn_id 后旧 entries 完全不残留（不串味）。
 * 2. 切回 A 后从空态全量重建而非显示脏数据。
 * 3. reset effect 与 events effect 竞态：turn_id 变化且 events 同帧变化，首帧即正确全量重建。
 * 4. forceRender 确实触发重渲染（用渲染计数断言）。
 * 5. 中段插入 / 头部插入探测不漏投；events 长度变短不脏累积。
 * 6. events 引用频繁变化但内容相同（memo/投影稳定不重复 append）。
 *
 * 注意：AgentMessage mock 渲染真实文本到 DOM，断言基于 container.textContent（反映最新渲染，
 * 不累积），以精确区分「切 turn 后残留旧 DOM」与「首次渲染的旧记录」。
 *
 * @module tests/turnTimeline.reset.adversarial
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { act } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/lib/perf", () => ({
  PerfTrace: { markCurrent: () => {}, endCurrent: () => {} },
}));

// 工具卡片渲染计数（验证 memo 命中 / 重复 append）。
let toolCount = 0;
vi.mock("@/components/chat/AgentMessage", () => ({
  // 渲染真实文本到 DOM，便于基于 container.textContent 断言当前内容（不累积）。
  AgentMessage: ({ content }: { content: string }) => <span data-testid="agent">{content}</span>,
}));
vi.mock("@/components/chat/UserMessage", () => ({
  UserMessage: ({ content }: { content?: string }) => <span data-testid="user">{content ?? ""}</span>,
}));
vi.mock("@/components/chat/ThinkingBlock", () => ({ ThinkingBlock: () => null }));
vi.mock("@/components/chat/ToolCallCard", () => ({
  ToolCallCard: ({ toolName }: { toolName?: string }) => {
    toolCount += 1;
    return <span data-testid="tool">{toolName ?? "tool"}</span>;
  },
}));
vi.mock("@/components/chat/ToolCallGroup", () => ({
  // 连续 ≥2 个 tool 会被 groupConsecutiveTools 聚合成 toolGroup 走此组件。
  ToolCallGroup: () => <span data-testid="toolgroup">group</span>,
}));
vi.mock("@/components/chat/TerminalCallCard", () => ({ TerminalCallCard: () => null }));
vi.mock("@/components/chat/StatusBadge", () => ({ StatusBadge: () => null }));

function makeDelta(eventId: string, sequence: number, text: string, turnId: string): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: "task-1",
    turn_id: turnId,
    event_type: "model_output_delta",
    sequence,
    created_at: new Date((sequence + 1) * 1000).toISOString(),
    payload: { text },
  } as unknown as RuntimeEvent;
}
function makeTool(turnId: string, idx: number): RuntimeEvent {
  return {
    event_id: `${turnId}-tool-${idx}`,
    task_id: "task-1",
    turn_id: turnId,
    event_type: "tool_call_finished",
    sequence: idx,
    created_at: new Date((idx + 1) * 1000).toISOString(),
    payload: { tool_name: "read_file", tool_call_id: `${turnId}-c${idx}`, status: "completed", content: `R${idx}` },
  } as unknown as RuntimeEvent;
}
function makeTurn(turnId: string, inputText: string): TurnRecord {
  return {
    turn_id: turnId,
    task_id: "task-1",
    input_text: inputText,
    status: "running",
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TurnRecord;
}

beforeEach(() => {
  toolCount = 0;
});

describe("TurnTimeline 跨 turn 复用实例不串味（reset 硬不变量）", () => {
  // 测试目的：先喂 turn A 的若干 events 投影出 entries，再把 turn_id 换成 B（events 为空），
  //   验证 A 的 entries 完全不残留（不串味）。断言基于切 turn 后的 container.textContent。
  it("切到空 events 的 turn B 后，旧 turn A 的 agent 文本不再残留于 DOM", () => {
    const turnA = makeTurn("turn-A", "A 输入");
    const turnB = makeTurn("turn-B", "B 输入");
    const eventsA = [makeDelta("a1", 1, "A 的回答", "turn-A")];
    const eventsB: RuntimeEvent[] = [];

    const { rerender, container } = render(<TurnTimeline turn={turnA} events={eventsA} />);
    expect(container.textContent).toContain("A 的回答");

    act(() => {
      rerender(<TurnTimeline turn={turnB} events={eventsB} />);
    });
    // B 没有任何 events（无 agent 文本），不应残留 A 的 agent 文本。
    // 但 UserMessage 会渲染 B 输入，故断言不应再含 A 的 agent 文本，且只含 B 输入。
    expect(container.textContent).not.toContain("A 的回答");
    expect(container.textContent).toContain("B 输入");
  });

  // 测试目的：切回 A 后从空态全量重建（reset 清空旧 entries）。
  it("切回 A（turn_id 再换回）后，A 的 agent 文本重新全量重建出现", () => {
    const turnA = makeTurn("turn-A", "A 输入");
    const turnB = makeTurn("turn-B", "B 输入");
    const eventsA = [makeDelta("a1", 1, "A 的回答", "turn-A")];
    const eventsB: RuntimeEvent[] = [];

    const { rerender, container } = render(<TurnTimeline turn={turnA} events={eventsA} />);
    act(() => {
      rerender(<TurnTimeline turn={turnB} events={eventsB} />);
    });
    expect(container.textContent).not.toContain("A 的回答");
    act(() => {
      rerender(<TurnTimeline turn={turnA} events={eventsA} />);
    });
    expect(container.textContent).toContain("A 的回答");
  });

  // 测试目的：turn_id 变化且 events 同帧变化（新 turn 首帧就带来 events）时，首帧即正确全量重建、
  //   不残留旧 entries。使用不同 turn_id + 不同内容的 delta。
  it("turn_id 与 events 同帧切换：新 turn 首帧即全量投影，不残留旧 entries", () => {
    const turnA = makeTurn("turn-A", "A 输入");
    const turnB = makeTurn("turn-B", "B 输入");
    const eventsA = [makeDelta("a1", 1, "AAAA", "turn-A")];
    const eventsB = [makeDelta("b1", 1, "BBBB", "turn-B")];

    const { rerender, container } = render(<TurnTimeline turn={turnA} events={eventsA} />);
    act(() => {
      rerender(<TurnTimeline turn={turnB} events={eventsB} />);
    });
    const all = container.textContent ?? "";
    expect(all).toContain("BBBB");
    expect(all).not.toContain("AAAA");
  });
});

describe("TurnTimeline forceRender 确实触发重渲染", () => {
  // 测试目的：reset effect 内 forceRender 必须在切 turn 时让组件重渲染，且新 turn 的首帧
  //   events 被全量重建。构造：A 用空 events，B 用 tool 事件；切到 B 后 B 的工具卡片应出现，
  //   证明切 turn 当帧即走 reset→全量重建路径（forceRender 生效）。
  it("切到带 tool 事件的新 turn 后，新 turn 的工具卡片当帧即被渲染", () => {
    const turnA = makeTurn("turn-A", "A 输入");
    const turnB = makeTurn("turn-B", "B 输入");
    const eventsA: RuntimeEvent[] = [];
    const eventsB = [makeTool("turn-B", 1)];

    const { rerender } = render(<TurnTimeline turn={turnA} events={eventsA} />);
    expect(toolCount).toBe(0);
    act(() => {
      rerender(<TurnTimeline turn={turnB} events={eventsB} />);
    });
    expect(toolCount).toBeGreaterThan(0);
  });
});

describe("TurnTimeline 中段 / 头部插入探测", () => {
  // 测试目的：已投影区间内部插入更早事件（中段乱序归位），验证该事件不漏投。
  it("events 由 [e0, e2] 变 [e0, e1, e2]（中段插入）后 e1 文本出现在 DOM", () => {
    const turn = makeTurn("turn-1", "in");
    const e0 = makeDelta("e0", 0, "S0-", "turn-1");
    const e1 = makeDelta("e1", 1, "S1-", "turn-1");
    const e2 = makeDelta("e2", 2, "S2", "turn-1");
    const { rerender, container } = render(<TurnTimeline turn={turn} events={[e0, e2]} />);
    expect(container.textContent).toContain("S0-");
    expect(container.textContent).toContain("S2");
    act(() => {
      rerender(<TurnTimeline turn={turn} events={[e0, e1, e2]} />);
    });
    expect(container.textContent).toContain("S1-");
  });

  // 测试目的：头部插入更早历史事件（首 event_id 变化），验证整体重建、新旧文本均出现。
  it("events 头部插入更早事件（首 event_id 变化）后，新旧文本均出现", () => {
    const turn = makeTurn("turn-1", "in");
    const e1 = makeDelta("e1", 1, "后到-", "turn-1");
    const e0 = makeDelta("e0", 0, "先到-", "turn-1");
    const { rerender, container } = render(<TurnTimeline turn={turn} events={[e1]} />);
    act(() => {
      rerender(<TurnTimeline turn={turn} events={[e0, e1]} />);
    });
    const all = container.textContent ?? "";
    expect(all).toContain("先到-");
    expect(all).toContain("后到-");
  });

  // 测试目的：events 长度变短（被裁剪/替换），验证不脏累积（旧长尾文本消失）。
  it("events 由 [e0,e1,e2] 变 [e0,e1]（长度变短）后，e2 文本不再出现", () => {
    const turn = makeTurn("turn-1", "in");
    const e0 = makeDelta("e0", 0, "S0-", "turn-1");
    const e1 = makeDelta("e1", 1, "S1-", "turn-1");
    const e2 = makeDelta("e2", 2, "S2", "turn-1");
    const { rerender, container } = render(<TurnTimeline turn={turn} events={[e0, e1, e2]} />);
    expect(container.textContent).toContain("S2");
    act(() => {
      rerender(<TurnTimeline turn={turn} events={[e0, e1]} />);
    });
    const all = container.textContent ?? "";
    expect(all).not.toContain("S2");
    expect(all).toContain("S0-");
    expect(all).toContain("S1-");
  });
});

describe("TurnTimeline events 引用频繁变化但内容相同（memo/投影稳定不重复 append）", () => {
  // 测试目的：每帧用新数组引用（内容相同）传入，验证投影不重复 append（agent 文本只出现一次定稿）。
  it("同一批 tool events 用新引用重复传入，工具卡片不被重复追加渲染", () => {
    const turn = makeTurn("turn-1", "in");
    const base = [makeTool("turn-1", 1), makeTool("turn-1", 2)];
    const { rerender, container } = render(<TurnTimeline turn={turn} events={base} />);
    // 2 个连续 tool 被聚合成 1 个 toolGroup（走 ToolCallGroup，而非 2 个 ToolCallCard）。
    const groupsFirst = container.querySelectorAll('[data-testid="toolgroup"]');
    expect(groupsFirst.length).toBe(1);
    const textFirst = container.textContent ?? "";
    for (let i = 0; i < 5; i++) {
      act(() => {
        rerender(<TurnTimeline turn={turn} events={base.map((e) => ({ ...e }))} />);
      });
    }
    // 重传相同内容后 DOM 中 toolGroup 数量仍应为 1（不重复 append）。
    const groupsAfter = container.querySelectorAll('[data-testid="toolgroup"]');
    expect(groupsAfter.length).toBe(1);
    const textLast = container.textContent ?? "";
    expect(textLast).toBe(textFirst);
  });

  // 测试目的：相同 callId 的 finished 重复到达（幂等），不应重复生成工具条目。
  it("相同 callId 的 finished 重复到达不重复追加工具条目", () => {
    const turn = makeTurn("turn-1", "in");
    const f1 = makeTool("turn-1", 1);
    const { rerender, container } = render(<TurnTimeline turn={turn} events={[f1]} />);
    expect(container.querySelectorAll('[data-testid="tool"]').length).toBe(1);
    const f1Again = makeTool("turn-1", 1);
    act(() => {
      rerender(<TurnTimeline turn={turn} events={[f1Again]} />);
    });
    expect(container.querySelectorAll('[data-testid="tool"]').length).toBe(1);
  });
});
