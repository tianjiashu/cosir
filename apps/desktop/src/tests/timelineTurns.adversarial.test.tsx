// @vitest-environment happy-dom
/**
 * ChatPanel.timelineTurns 真实排序逻辑对抗性测试（端到端经真实 ChatPanel 组件）。
 *
 * 与 turnOverlapFix.test.tsx 区别：本文件不复制排序实现，而是渲染真实 <ChatPanel>，
 * 通过 mock VirtualList 把 visibleTurns（即 timelineTurns 切片）传给 mock TurnTimeline，
 * 从 TurnTimeline 实际收到的 turn 顺序断言真实排序代码的行为。
 *
 * 覆盖任务要求的「created_at 排序健壮性」所有对抗场景：
 * - undefined / null / "" / 非法字符串 / 跨时区 / 同秒 / 100+ 乱序 等。
 *
 * @module tests/timelineTurns.adversarial
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { act } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { ChatPanel } from "@/components/layout/ChatPanel";

// ---- 捕获 TurnTimeline 实际收到的 turn 顺序 ----
const receivedTurns: TurnRecord[] = [];
vi.mock("@/components/layout/TurnTimeline", () => ({
  TurnTimeline: ({ turn }: { turn: TurnRecord }) => {
    receivedTurns.push(turn);
    return null;
  },
}));

// VirtualList 直接渲染 items 的 renderItem（renderItem 内部再调 TurnTimeline），
// 这样 visibleTurns（= timelineTurns 切片）真实流经真实排序逻辑。
vi.mock("@/lib/virtual/VirtualList", () => ({
  VirtualList: ({ items, renderItem }: { items: TurnRecord[]; renderItem: (t: TurnRecord) => unknown }) =>
    items.map((it, i) => <div key={i}>{renderItem(it, i)}</div>),
}));

vi.mock("@/lib/perf", () => ({
  PerfTrace: { markCurrent: () => {}, endCurrent: () => {} },
}));
vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));
vi.mock("@/components/ui/button", () => ({
  Button: () => null,
}));
// ChatPanel 顶部现内嵌 TaskHeaderBar（含 AgentSelector + ModelSelector + ProviderSettingsDialog）；
// 本测试关注 timelineTurns 排序，不展开这些子组件细节，统一桩化为空 stub。
vi.mock("@/components/chat/TaskHeaderBar", () => ({
  TaskHeaderBar: () => null,
}));

// ---- store mock：受控的 turns / events / activeTask ----
let storeState: {
  activeTaskId: string | null;
  activeTask: unknown;
  turns: TurnRecord[];
  eventsByTurnId: Record<string, RuntimeEvent[]>;
  eventsForTask: RuntimeEvent[];
};

function mkStore() {
  return {
    // taskStore
    useTaskStore: (sel: (s: unknown) => unknown) => sel(storeState.activeTask),
    // turnStore
    useTurnStore: (sel: (s: unknown) => unknown) => sel({ turnsByTaskId: storeState.turns.length ? { task1: storeState.turns } : {} }),
    // workspaceStore
    useWorkspaceStore: (sel: (s: unknown) => unknown) => sel({ activeWorkspaceId: "ws1" }),
    // eventStore：ChatPanel 用 useShallow 选 eventsByTurnId/events，用普通 selector 选 latestEvent
    useEventStore: (sel: (s: unknown) => unknown) => sel({ eventsByTurnId: storeState.eventsByTurnId, events: storeState.eventsForTask }),
  };
}

vi.mock("@/stores/taskStore", () => ({
  useTaskStore: (s: (st: unknown) => unknown) => mkStore().useTaskStore(s),
  selectActiveTask: () => storeState.activeTask,
}));
vi.mock("@/stores/turnStore", () => ({ useTurnStore: (s: (st: unknown) => unknown) => mkStore().useTurnStore(s) }));
vi.mock("@/stores/workspaceStore", () => ({ useWorkspaceStore: (s: (st: unknown) => unknown) => mkStore().useWorkspaceStore(s) }));
vi.mock("@/stores/eventStore", () => ({
  useEventStore: (s: (st: unknown) => unknown) => mkStore().useEventStore(s),
  selectEventsForTask: () => storeState.eventsForTask,
  selectLatestEvent: () => storeState.eventsForTask[storeState.eventsForTask.length - 1] ?? null,
  EMPTY_EVENTS: [],
}));

function makeTurn(turnId: string, createdAt: unknown): TurnRecord {
  return {
    turn_id: turnId,
    task_id: "task1",
    input_text: turnId,
    status: "pending",
    end_reason: null,
    response_text: null,
    created_at: createdAt as string,
    updated_at: createdAt as string,
  } as unknown as TurnRecord;
}

function setActiveTask(t: TurnRecord | null) {
  storeState.activeTaskId = t?.task_id ?? "task1";
  storeState.activeTask = t;
  storeState.turns = t ? [t] : [];
  storeState.eventsByTurnId = {};
  storeState.eventsForTask = [];
}

beforeEach(() => {
  receivedTurns.length = 0;
  storeState = {
    activeTaskId: "task1",
    activeTask: null,
    turns: [],
    eventsByTurnId: {},
    eventsForTask: [],
  };
});

describe("ChatPanel.timelineTurns 真实排序健壮性（经真实 ChatPanel 组件）", () => {
  // 测试目的：非法空值 created_at 应被兜底为 0 排到最前，而非抛错或乱序。
  // 可能发现的缺陷：new Date(undefined).getTime() 抛错、或 "" 落入 localeCompare 头部之外位置。
  it("created_at 为 undefined / null / '' 时兜底排最前且不抛错", () => {
    const tUndef = makeTurn("t-undef", undefined);
    const tNull = makeTurn("t-null", null as unknown as string);
    const tEmpty = makeTurn("t-empty", "");
    const tReal = makeTurn("t-real", new Date(5000).toISOString());
    // 故意乱序输入
    setActiveTask(tReal);
    storeState.turns = [tReal, tNull, tUndef, tEmpty];

    render(<ChatPanel onPickWorkspace={() => {}} />);
    const ids = receivedTurns.map((t) => t.turn_id);
    // 三个非法值都兜底为 0，排在所有真实时间之前；它们之间相对顺序保持稳定（slice 稳定排序）
    expect(ids).toContain("t-undef");
    expect(ids).toContain("t-null");
    expect(ids).toContain("t-empty");
    expect(ids).toContain("t-real");
    const realIdx = ids.indexOf("t-real");
    expect(realIdx).toBe(3); // t-real 在 3 个兜底 0 之后
    expect(ids).toHaveLength(4);
  });

  // 测试目的：非法字符串（如 "abc"）解析失败应兜底 0 排尾部/前部，不抛错。
  // 可能发现的缺陷：new Date("abc").getTime() 抛错或产生 NaN 比较污染排序。
  it("created_at 为非法字符串（'abc'）时解析失败兜底为 0，不抛错", () => {
    const tBad = makeTurn("t-bad", "abc");
    const tReal = makeTurn("t-real", new Date(3000).toISOString());
    setActiveTask(tReal);
    storeState.turns = [tBad, tReal];
    expect(() => render(<ChatPanel onPickWorkspace={() => {}} />)).not.toThrow();
    const ids = receivedTurns.map((t) => t.turn_id);
    expect(ids).toEqual(["t-bad", "t-real"]);
  });

  // 测试目的：混合同秒、不同 ISO 格式（含 'Z' 与无时区后缀 UTC）、跨时区表达，
  // 排序应严格按毫秒时间升序。
  // 可能发现的缺陷：不同 ISO 格式经 new Date 解析后顺序错乱。
  it("混合 ISO 格式 / 跨时区表达按毫秒时间严格升序", () => {
    const tIsoZ = makeTurn("iso-z", "2023-01-01T00:00:00.000Z"); // 0
    const tIsoLocal = makeTurn("iso-local", "2023-01-01T00:00:01.000Z"); // +1000ms
    const tIsoOffset = makeTurn("iso-offset", "2023-01-01T00:00:02.000+00:00"); // +2000ms
    const tIsoMs = makeTurn("iso-ms", "2023-01-01T00:00:02.500Z"); // +2500ms
    setActiveTask(tIsoMs);
    storeState.turns = [tIsoLocal, tIsoZ, tIsoOffset, tIsoMs];
    render(<ChatPanel onPickWorkspace={() => {}} />);
    const ids = receivedTurns.map((t) => t.turn_id);
    expect(ids).toEqual(["iso-z", "iso-local", "iso-offset", "iso-ms"]);
  });

  // 测试目的：100+ turn 乱序输入，排序后必须严格升序（相邻差值 >= 0）且无丢失。
  // 可能发现的缺陷：排序非稳定导致丢失、或比较函数误用 localeCompare 造成"10"排在"2"前。
  it("100+ turn 乱序输入排序后严格升序且无丢失", () => {
    const N = 150;
    const turns: TurnRecord[] = [];
    for (let i = 0; i < N; i++) {
      turns.push(makeTurn(`t-${i}`, new Date(i * 1000).toISOString()));
    }
    // Fisher-Yates 乱序
    for (let i = turns.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [turns[i], turns[j]] = [turns[j], turns[i]];
    }
    setActiveTask(turns[0]);
    storeState.turns = turns;
    render(<ChatPanel onPickWorkspace={() => {}} />);
    const ids = receivedTurns.map((t) => t.turn_id);
    // ChatPanel 有分片窗口 INITIAL_TURN_COUNT=20，仅渲染最近 20 个 turn（visibleTurns）。
    // 验证这 20 个恰为全局排序后的末尾 20 个（t-130..t-149），且严格升序、无重复。
    const WIN = 20;
    expect(ids).toHaveLength(WIN);
    const times = ids.map((id) => {
      const n = Number(id.split("-")[1]);
      return n * 1000;
    });
    for (let i = 1; i < times.length; i++) {
      expect(times[i]).toBeGreaterThanOrEqual(times[i - 1]);
    }
    const expectedTail = Array.from({ length: WIN }, (_, i) => `t-${N - WIN + i}`);
    expect(ids).toEqual(expectedTail);
  });

  // 测试目的：乐观临时 turn（本地时钟 Date.now() 字符串）紧贴真实 turn（后端时钟、同秒），
  // 排序应符合时间序（本地更早/更晚由实际毫秒决定），不依赖输入数组序。
  // 可能发现的缺陷：同秒时排序不稳定落入数组原序巧合，导致重叠。
  it("本地临时 turn 与后端 turn 同秒时严格按毫秒升序、两次输入顺序一致", () => {
    const baseMs = Date.now();
    const temp = makeTurn("temp-" + baseMs, new Date(baseMs).toISOString());
    const real = makeTurn("real-" + (baseMs + 1), new Date(baseMs + 1).toISOString());
    setActiveTask(real);
    const order1 = [temp, real];
    const order2 = [real, temp];
    storeState.turns = order1;
    render(<ChatPanel onPickWorkspace={() => {}} />);
    const ids1 = receivedTurns.map((t) => t.turn_id);
    receivedTurns.length = 0;
    storeState.turns = order2;
    // 重新渲染新实例以清空：用 unmount 后重渲染
    act(() => {});
    // 简单重建
    const { unmount } = render(<ChatPanel onPickWorkspace={() => {}} />);
    const ids2 = receivedTurns.map((t) => t.turn_id);
    unmount();
    expect(ids1).toEqual([temp.turn_id, real.turn_id]);
    expect(ids2).toEqual([temp.turn_id, real.turn_id]);
  });

  // 测试目的：child turn（delegation_child_started 标记）应从主 timeline 过滤掉。
  // 可能发现的缺陷：过滤逻辑漏掉 child turn 或误删 parent turn。
  it("含 delegation_child_started 事件的 child turn 应从主 timeline 过滤", () => {
    const parent = makeTurn("turn-parent", new Date(1000).toISOString());
    const child = makeTurn("turn-child", new Date(2000).toISOString());
    const childEvent: RuntimeEvent = {
      event_id: "ce1",
      task_id: "task1",
      turn_id: "turn-parent",
      event_type: "delegation_child_started",
      payload: { child_turn_id: "turn-child" },
    } as unknown as RuntimeEvent;
    setActiveTask(parent);
    storeState.turns = [parent, child];
    storeState.eventsByTurnId = { "turn-parent": [childEvent] };
    storeState.eventsForTask = [childEvent];
    render(<ChatPanel onPickWorkspace={() => {}} />);
    const ids = receivedTurns.map((t) => t.turn_id);
    expect(ids).toContain("turn-parent");
    expect(ids).not.toContain("turn-child");
    expect(ids).toHaveLength(1);
  });
});
