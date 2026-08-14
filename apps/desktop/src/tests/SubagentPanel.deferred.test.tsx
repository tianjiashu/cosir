// @vitest-environment happy-dom
/**
 * SubagentPanel 派生降频（修复 2）测试。
 *
 * 验证：
 *  1. 功能正确性：并发 sibling tab、选中 child 状态徽章、child timeline 分片渲染。
 *  2. 派生降频（核心契约）：高频更新 eventStore.events 时，sibling/status 两个 O(N) 派生
 *     函数（deriveSiblingDelegations / deriveChildDelegationStatus）的调用次数显著少于
 *     store 更新次数——proof 是 useDeferredValue 把全量事件降为低优先级副本，
 *     使这两个 useMemo 不会在每次 store 更新都同步重算。
 *  3. 最终一致性：高频更新全部 settle 后，派生结果（sibling 数量 / 状态）与「一次性 seed
 *     全量事件」的基准结果一致（无丢派生）。
 *  4. childEvents 实时性：仅更新选中 child 分片事件时，timeline 立即反映，与 allEvents 的
 *     deferred 解耦（不延迟）。
 *
 * 注意：只用 vi.spyOn 计数，不 mock 整个 projector（保证真实派生被验证）。
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useDelegationStore } from "@/stores/delegationStore";
import { useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";
import * as projector from "@/services/timeline/projector";

/** 构造扁平 RuntimeEvent（不依赖真实后端）。 */
function runtimeEvent(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  turnId: string,
  payload: Record<string, unknown>,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: "task-delegation",
    turn_id: turnId,
    sequence,
    created_at: `2026-08-11T00:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
  } as RuntimeEvent;
}

/** 构造最小 TurnRecord（用于 turnStore 落库，避免触发兜底 synthetic record）。 */
function turnRecord(turnId: string, status: "pending" | "running" | "completed" = "running") {
  return {
    turn_id: turnId,
    task_id: "task-delegation",
    input_text: `input-${turnId}`,
    status,
    end_reason: null,
    response_text: null,
    created_at: "",
    updated_at: "",
  };
}

const TASK_ID = "task-delegation";
const PARENT_TURN = "turn-parent";

/** 构造一棵 N 个并发 child delegation 的事件树（同 parent，同 delegation_type）。 */
function buildConcurrentEvents(childCount: number, perChildEventCount: number): RuntimeEvent[] {
  const events: RuntimeEvent[] = [];
  let seq = 0;
  for (let c = 0; c < childCount; c++) {
    const childTurnId = `turn-child-${c + 1}`;
    const delegationId = `delegation-${c + 1}`;
    for (let e = 0; e < perChildEventCount; e++) {
      seq += 1;
      events.push(
        runtimeEvent(
          `ev-c${c}-${e}`,
          "delegation_child_started",
          PARENT_TURN,
          {
            delegation_id: delegationId,
            parent_turn_id: PARENT_TURN,
            child_turn_id: childTurnId,
            child_agent_id: `agent-${c + 1}`,
            delegation_type: "review",
            status: "running",
          },
          seq,
        ),
      );
    }
  }
  return events;
}

/** 选中 child 自己的 timeline 分片事件（model_output_delta，turn_id = childTurnId）。
 *  prefix 用于让不同批次的 event_id 互不重叠（eventStore 按 event_id 全局去重）。 */
function buildChildShard(childTurnId: string, count: number, prefix = "shard"): RuntimeEvent[] {
  const events: RuntimeEvent[] = [];
  for (let i = 0; i < count; i++) {
    events.push(
      runtimeEvent(
        `${prefix}-${childTurnId}-${i}`,
        "model_output_delta",
        childTurnId,
        { text: `chunk-${i}` },
        1000 + i,
      ),
    );
  }
  return events;
}

// 为 TurnTimeline 注册 stub：避免渲染整棵真实 timeline（含大量子组件副作用），
// 但保留 SubagentPanel 自身的真实 projector 派生逻辑（spy 计数的是 projector 派生函数，
// 与 TurnTimeline 内部投影无关）。TurnTimeline 渲染 childEvents.length，用于断言实时性。
vi.mock("@/components/layout/TurnTimeline", () => ({
  TurnTimeline: ({ events }: { turn: unknown; events: RuntimeEvent[] }) =>
    events.length === 0 ? null : (
      <div data-testid="turn-timeline" data-len={String(events.length)}>
        timeline:{events.length}
      </div>
    ),
}));

describe("SubagentPanel 派生降频（修复 2）", () => {
  beforeEach(() => {
    useDelegationStore.getState().clearSelection();
    useEventStore.getState().clearEvents();
    useTurnStore.getState().setTurnsForTask(TASK_ID, []);
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 测试目的：验证并发 sibling tab 渲染 + 选中 child 状态徽章 + child timeline 分片。
  // 可能发现的缺陷：siblings 派生口径错误（长度 < 2 不渲染 tab）、状态徽章映射缺失、
  // childEvents 分片未传入 TurnTimeline。
  it("功能正确性：并发 child 选中后渲染 sibling tab / 状态徽章 / child timeline", async () => {
    // 一次性灌入全部事件（含选中 child 的分片），分片按 turn_id 自动归位。
    const events = [...buildConcurrentEvents(2, 1), ...buildChildShard("turn-child-1", 3)];

    await act(async () => {
      useEventStore.getState().setEvents(events, TASK_ID);
      useTurnStore.getState().setTurnsForTask(TASK_ID, [
        turnRecord("turn-child-1"),
        turnRecord("turn-child-2"),
      ]);
      useDelegationStore.getState().selectChildTurn("turn-child-1");
    });

    render(<SubagentPanel />);

    // 并发组 2 个 child → sibling tab 出现（role=tablist 存在，内含 2 个 tab）。
    const tablist = screen.getByRole("tablist", { name: "并发子 Agent 切换" });
    expect(tablist).toBeTruthy();
    expect(tablist.querySelectorAll('[role="tab"]').length).toBe(2);

    // 选中 child 状态徽章：running → "运行中"（头部 + 当前 tab 各一个，>=1 即可）。
    expect(screen.getAllByText("运行中").length).toBeGreaterThanOrEqual(1);

    // timeline 区域渲染了 childEvents 分片（3 条 turn-child-1 事件）。
    const timeline = screen.getByTestId("turn-timeline");
    expect(timeline.getAttribute("data-len")).toBe("3");
  });

  // 测试目的（核心契约）：高频多次更新 eventStore.events 时，两个派生函数调用次数
  // 显著少于更新次数，证明 useDeferredValue 将全量事件降频为低优先级副本。
  // 可能发现的缺陷：忘记使用 useDeferredValue，导致每次 store 更新都同步重算派生（调用次数 == 更新次数）。
  it("派生降频：高频连续更新 events，派生函数调用次数显著少于更新次数", async () => {
    const spySiblings = vi.spyOn(projector, "deriveSiblingDelegations");
    const spyStatus = vi.spyOn(projector, "deriveChildDelegationStatus");

    // 初始 seed：3 个并发 child，建立选中态与初始派生。
    await act(async () => {
      useEventStore.getState().setEvents(buildConcurrentEvents(3, 1), TASK_ID);
      useTurnStore.getState().setTurnsForTask(TASK_ID, [
        turnRecord("turn-child-1"),
        turnRecord("turn-child-2"),
        turnRecord("turn-child-3"),
      ]);
      useDelegationStore.getState().selectChildTurn("turn-child-1");
    });

    const { unmount } = render(<SubagentPanel />);

    // 模拟高频事件流：连续 10 次更新（每次追加一批新事件）且全部包在同一 act 帧内。
    const UPDATE_COUNT = 10;
    await act(async () => {
      for (let i = 0; i < UPDATE_COUNT; i++) {
        // 每帧再追加 3 个并发 child 各 1 条事件（共 9 条），模拟每帧一次 set。
        const batch = buildConcurrentEvents(3, 1).map((ev, idx) => ({
          ...ev,
          event_id: `hf-${i}-${ev.event_id}-${idx}`,
          sequence: 10000 + i * 10 + idx,
        }));
        useEventStore.getState().setEvents(batch, TASK_ID);
      }
    });

    // 在「单次同步渲染帧」内，派生调用次数应远小于更新次数：
    // deferred 值尚未 flush，sibling/status 派生不应逐次重算。
    // 理想情况下调用次数 == 1（仅初次渲染一次），最多允许 < UPDATE_COUNT。
    expect(spySiblings.mock.calls.length).toBeLessThan(UPDATE_COUNT);
    expect(spyStatus.mock.calls.length).toBeLessThan(UPDATE_COUNT);

    // 关键契约：在同步帧内，派生调用次数不应等于「更新次数」级别；
    // 这里断言最多只重算了极少数几次（降频生效）。
    expect(spySiblings.mock.calls.length).toBeLessThanOrEqual(2);
    expect(spyStatus.mock.calls.length).toBeLessThanOrEqual(2);

    unmount();
  });

  // 测试目的：高频更新全部 settle 后，断言派生结果（sibling 数量 / 状态）最终与
  // 「一次性 seed 全部事件」的基准结果一致，证明降频未丢派生（无丢数据）。
  // 可能发现的缺陷：deferred 值未 flush 或被提前裁剪，导致最终 sibling 数量少于基准。
  it("最终一致性：高频更新 settle 后派生结果与一次性 seed 基准一致", async () => {
    // 基准：一次性把所有高频事件 + 初始事件灌入，派生应得到 3 个 sibling（全并发）。
    const baselineEvents = [...buildConcurrentEvents(3, 1)];
    for (let i = 0; i < 10; i++) {
      baselineEvents.push(
        ...buildConcurrentEvents(3, 1).map((ev, idx) => ({
          ...ev,
          event_id: `hf-${i}-${ev.event_id}-${idx}`,
          sequence: 10000 + i * 10 + idx,
        })),
      );
    }

    const baselineSiblings = projector.deriveSiblingDelegations(baselineEvents, "turn-child-1");
    const baselineStatus = projector.deriveChildDelegationStatus("turn-child-1", baselineEvents);
    expect(baselineSiblings.length).toBe(3);
    expect(baselineStatus).toBe("running");

    // 高频路径：初始 seed + 10 帧更新，再等待 deferred 值 flush。
    await act(async () => {
      useEventStore.getState().setEvents(buildConcurrentEvents(3, 1), TASK_ID);
      useTurnStore.getState().setTurnsForTask(TASK_ID, [
        turnRecord("turn-child-1"),
        turnRecord("turn-child-2"),
        turnRecord("turn-child-3"),
      ]);
      useDelegationStore.getState().selectChildTurn("turn-child-1");
    });

    const { unmount } = render(<SubagentPanel />);

    await act(async () => {
      for (let i = 0; i < 10; i++) {
        const batch = buildConcurrentEvents(3, 1).map((ev, idx) => ({
          ...ev,
          event_id: `hf-${i}-${ev.event_id}-${idx}`,
          sequence: 10000 + i * 10 + idx,
        }));
        useEventStore.getState().setEvents(batch, TASK_ID);
      }
    });

    // 等待 deferred 值 flush（useDeferredValue 低优先级更新在下一个 tick 生效）。
    // 断言最终在 store 全量事件上直接派生，结果与基准一致（无丢派生）。
    await waitFor(
      () => {
        const finalSiblings = projector.deriveSiblingDelegations(
          useEventStore.getState().events,
          "turn-child-1",
        );
        expect(finalSiblings.length).toBe(baselineSiblings.length);
      },
      { timeout: 3000 },
    );

    // 断言面板最终确实渲染了 3 个 sibling tab（deferred flush 后）。
    await waitFor(
      () => {
        expect(screen.getAllByRole("tab").length).toBe(3);
      },
      { timeout: 3000 },
    );

    // 断言最终状态徽章仍为 running（与基准一致）。
    expect(screen.getAllByText("运行中").length).toBeGreaterThanOrEqual(1);

    unmount();
  });

  // 测试目的：childEvents 分片实时性——仅更新选中 child 的分片事件时，timeline 立即反映，
  // 与 allEvents 的 deferred 解耦（不延迟）。
  // 可能发现的缺陷：误把 childEvents 也用 deferred，导致分片更新被延迟、timeline 滞后。
  it("childEvents 实时性：仅更新选中 child 分片，timeline 立即反映（不延迟）", async () => {
    // 初始：选中 child-1，注入少量分片事件（1 条）。
    await act(async () => {
      useEventStore.getState().setEvents(
        [...buildConcurrentEvents(2, 1), ...buildChildShard("turn-child-1", 1)],
        TASK_ID,
      );
      useTurnStore.getState().setTurnsForTask(TASK_ID, [
        turnRecord("turn-child-1"),
        turnRecord("turn-child-2"),
      ]);
      useDelegationStore.getState().selectChildTurn("turn-child-1");
    });

    const { unmount } = render(<SubagentPanel />);
    expect(screen.getByTestId("turn-timeline").getAttribute("data-len")).toBe("1");

    // 仅追加选中 child 的分片事件（appendEvent 走 eventsByTurnId 分片通道），
    // 不碰全量 allEvents 之外的任何东西。timeline 应同步帧内立即反映（实时，无需 deferred flush）。
    // 注意：extra 用独立 prefix（"shard-x"），避免与初始 setEvents 灌入的 event_id 全局去重重迭。
    await act(async () => {
      const extra = buildChildShard("turn-child-1", 2, "shard-x");
      for (const e of extra) useEventStore.getState().appendEvent(e);
    });

    // 同步帧内即应反映（实时），无需等待 deferred flush。
    expect(screen.getByTestId("turn-timeline").getAttribute("data-len")).toBe("3");

    unmount();
  });

  // 测试目的：确认 deferred 路径下，未 flush 时面板仍显示「上一次」的派生结果（不崩溃、不白屏），
  // 侧面证明 deferred 确实延迟了重算（而非同步全量重算）。
  // 可能发现的缺陷：高频更新期间若派生同步执行，渲染会随每次更新抖动；此处验证稳定。
  it("派生降频：高频更新期间未 flush 时面板不抛错且保持可渲染", async () => {
    await act(async () => {
      useEventStore.getState().setEvents(buildConcurrentEvents(3, 1), TASK_ID);
      useTurnStore.getState().setTurnsForTask(TASK_ID, [
        turnRecord("turn-child-1"),
        turnRecord("turn-child-2"),
        turnRecord("turn-child-3"),
      ]);
      useDelegationStore.getState().selectChildTurn("turn-child-1");
    });

    const { unmount } = render(<SubagentPanel />);
    expect(screen.getAllByText("运行中").length).toBeGreaterThanOrEqual(1);

    // 连续高频更新（同步帧内不 await 完成 flush）。
    await act(async () => {
      for (let i = 0; i < 5; i++) {
        const batch = buildConcurrentEvents(3, 1).map((ev, idx) => ({
          ...ev,
          event_id: `hf2-${i}-${ev.event_id}-${idx}`,
          sequence: 20000 + i * 10 + idx,
        }));
        useEventStore.getState().setEvents(batch, TASK_ID);
      }
    });

    // 面板不应因高频更新崩溃，且仍渲染（派生结果在 flush 前沿用旧值）。
    expect(screen.getByRole("tablist", { name: "并发子 Agent 切换" })).toBeTruthy();
    expect(screen.getAllByText("运行中").length).toBeGreaterThanOrEqual(1);

    unmount();
  });
});
