// @vitest-environment happy-dom
/**
 * 缺陷验证 #2：TurnTimeline 增量续算丢失「中段插入」的乱序归位事件。
 *
 * 背景：TurnTimeline 的投影 effect 假设 events append-only，仅当「长度变短」或
 * 「首事件 event_id 变化」时才整体重建，否则 delta = events.slice(lastLen)。
 * 当 eventStore.appendOrderedShard 把迟到事件插入数组中段（[e0, e2] → [e0, e1, e2]，
 * 长度不减、首事件不变）时，delta = [e2]——而 e2 已被投影过（幂等跳过），
 * e1 永远不被投影，其文本直到切换任务前都不渲染。
 */
import { describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { TurnTimeline } from "@/components/layout/TurnTimeline";

// openFileInEditor 来自 Tauri 宿主模块，测试环境无 Tauri，mock 掉以免导入崩溃。
vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));
// 叶子组件替换为轻量桩：AgentMessage 直接渲染文本以供断言，其余不参与本测试。
vi.mock("@/components/chat/AgentMessage", () => ({
  AgentMessage: ({ content }: { content: string }) => (
    <div data-testid="agent-message">{content}</div>
  ),
}));
vi.mock("@/components/chat/UserMessage", () => ({
  UserMessage: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("@/components/chat/ThinkingBlock", () => ({ ThinkingBlock: () => null }));
vi.mock("@/components/chat/ToolCallCard", () => ({ ToolCallCard: () => null }));
vi.mock("@/components/chat/TerminalCallCard", () => ({ TerminalCallCard: () => null }));
vi.mock("@/components/chat/StatusBadge", () => ({ StatusBadge: () => null }));

function makeDeltaEvent(eventId: string, sequence: number, text: string): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: "model_output_delta",
    sequence,
    created_at: new Date((sequence + 1) * 1000).toISOString(),
    payload: { text },
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

describe("TurnTimeline 中段乱序事件归位", () => {
  // 测试目的：模拟「乱序分片先落地 e0/e2，随后 e1 中段插入归位」，断言 e1 文本渲染。
  // 可能发现的缺陷：中段插入不触发重建，delta 切片把已处理的 e2 当新增、漏掉 e1，
  //   导致 e1 文本永久丢失（直到切换任务强制重建）。
  it("events 由 [e0, e2] 变为 [e0, e1, e2]（e1 中段归位）后，e1 的文本应出现在文档中", () => {
    const turn = makeTurn();
    const e0 = makeDeltaEvent("e0", 0, "第一段-");
    const e1 = makeDeltaEvent("e1", 1, "第二段-");
    const e2 = makeDeltaEvent("e2", 2, "第三段");

    const { container, rerender } = render(<TurnTimeline turn={turn} events={[e0, e2]} />);
    // 前置确认：乱序先到的两段已渲染（证明首帧投影正常）。
    expect(container.textContent).toContain("第一段-");
    expect(container.textContent).toContain("第三段");

    // e1 乱序归位：中段插入，长度不减、首事件身份不变（模拟 appendOrderedShard 归位）。
    act(() => {
      rerender(<TurnTimeline turn={turn} events={[e0, e1, e2]} />);
    });

    // 正确行为：e1 归位后其文本应渲染（完整内容为 第一段-第二段-第三段）。
    expect(container.textContent).toContain("第二段-");
  });

  // 测试目的：正向对照——正常的尾部追加（append-only）增量续算应渲染新文本。
  // 可能发现的缺陷：无（此用例应 PASS，证明 harness 与增量路径本身可用）。
  it("对照：尾部追加 [e0, e1] → [e0, e1, e2] 时 e2 文本正常渲染", () => {
    const turn = makeTurn();
    const e0 = makeDeltaEvent("e0", 0, "第一段-");
    const e1 = makeDeltaEvent("e1", 1, "第二段-");
    const e2 = makeDeltaEvent("e2", 2, "第三段");

    const { container, rerender } = render(<TurnTimeline turn={turn} events={[e0, e1]} />);
    expect(container.textContent).toContain("第一段-第二段-");

    act(() => {
      rerender(<TurnTimeline turn={turn} events={[e0, e1, e2]} />);
    });
    expect(container.textContent).toContain("第一段-第二段-第三段");
  });
});
