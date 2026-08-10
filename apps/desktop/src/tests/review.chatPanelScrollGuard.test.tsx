// @vitest-environment happy-dom
/**
 * 缺陷验证 #3：ChatPanel 自动滚底缺少「用户正在底部」守卫。
 *
 * 背景：ChatPanel 的滚动 effect（约 182-197 行）在 events.length / latestEvent
 * 变化时无条件 scrollTo 到底部，不判断用户是否已上滚阅读历史。正确行为应为：
 * 仅当用户处于底部（或接近底部）时才跟随滚底；用户上滚阅读时新事件不应打断阅读位置。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";
import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { useEventStore } from "@/stores/eventStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

// TurnTimeline 投影链路与本测试无关，替换为轻量桩以聚焦滚动行为。
vi.mock("@/components/layout/TurnTimeline", () => ({
  TurnTimeline: () => <div data-testid="turn-timeline-stub" />,
}));
// 性能埋点走 logger→console，mock 掉保持输出干净。
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));

const TASK_ID = "task-scroll";

function makeEvent(eventId: string, sequence: number): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: TASK_ID,
    turn_id: "turn-1",
    event_type: "model_output_delta",
    sequence,
    created_at: new Date((sequence + 1) * 1000).toISOString(),
    payload: { text: `text-${eventId}` },
  } as unknown as RuntimeEvent;
}

function seedStores(events: RuntimeEvent[]) {
  useWorkspaceStore.setState({ activeWorkspaceId: "ws-1" } as never);
  useTaskStore.setState({
    activeTaskId: TASK_ID,
    activeTurnId: null,
    tasksById: {
      [TASK_ID]: {
        task_id: TASK_ID,
        workspace_id: "ws-1",
        agent_id: "dev",
        input_text: "hi",
        title: "hi",
        last_message_preview: "",
        latest_turn_id: "turn-1",
        status: "running",
        execution_status: "running",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      } satisfies TaskRecord,
    },
    tasksByWorkspaceId: {
      "ws-1": [
        {
          task_id: TASK_ID,
          workspace_id: "ws-1",
          agent_id: "dev",
          input_text: "hi",
          title: "hi",
          last_message_preview: "",
          latest_turn_id: "turn-1",
          status: "running",
          execution_status: "running",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        } satisfies TaskRecord,
      ],
    },
    loadedWorkspaceIds: new Set(["ws-1"]),
  } as never);
  useTurnStore.setState({
    turnsByTaskId: {
      [TASK_ID]: [
        {
          turn_id: "turn-1",
          task_id: TASK_ID,
          input_text: "hi",
          status: "running",
          end_reason: null,
          response_text: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        } satisfies TurnRecord,
      ],
    },
  } as never);
  useEventStore.setState({
    events,
    eventsByTaskId: { [TASK_ID]: events },
    eventsByTurnId: { "turn-1": events },
  } as never);
}

describe("ChatPanel 新事件到达时的滚底守卫", () => {
  let scrollToSpy: ReturnType<typeof vi.fn>;
  let nowValue: number;

  beforeEach(() => {
    // happy-dom 未实现元素 scrollTo，替换为 spy 以观测调用。
    scrollToSpy = vi.fn();
    Object.defineProperty(window.HTMLElement.prototype, "scrollTo", {
      value: scrollToSpy,
      writable: true,
      configurable: true,
    });
    // 可控时钟：规避组件内 200ms 节流对断言时序的干扰。
    nowValue = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => nowValue);
    seedStores([makeEvent("e0", 0)]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  /**
   * stub 滚动容器几何并模拟用户滚动到指定位置。
   *
   * happy-dom 的 scrollHeight/clientHeight 恒为 0，无法表达「上滚/在底」，
   * 必须显式 stub 度量并派发 scroll 事件，组件的近底守卫才能感知位置变化。
   */
  function stubScrollGeometry(scrollTop: number) {
    const scrollContainer = document.querySelector("[data-testid='chat-turn-scroll-container']") as HTMLElement;
    expect(scrollContainer).not.toBeNull();
    Object.defineProperty(scrollContainer, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scrollContainer, "clientHeight", { value: 500, configurable: true });
    scrollContainer.scrollTop = scrollTop;
    act(() => {
      fireEvent.scroll(scrollContainer);
    });
    return scrollContainer;
  }

  // 测试目的：用户已上滚（scrollTop 远离底部）时，新事件到达不应强制滚底。
  // 可能发现的缺陷：滚动 effect 无「用户在底部」判断，新事件无条件 scrollTo 到底，
  //   打断用户阅读历史（每次流式 delta 都把视口拽回底部）。
  it("用户上滚阅读历史时，新事件到达不应调用 scrollTo 强制滚底", () => {
    render(<ChatPanel onPickWorkspace={vi.fn()} />);
    // 挂载首帧允许滚底一次（初始定位），此后清零统计。
    scrollToSpy.mockClear();

    // 模拟用户上滚阅读历史：距底 500px（远超 80px 近底阈值）。
    stubScrollGeometry(0);

    // 推进时钟越过 200ms 节流窗口，再向 eventStore 追加一条新事件（模拟流式到达）。
    nowValue += 1000;
    const events = [makeEvent("e0", 0), makeEvent("e1", 1)];
    act(() => {
      useEventStore.setState({
        events,
        eventsByTaskId: { [TASK_ID]: events },
        eventsByTurnId: { "turn-1": events },
      } as never);
    });

    // 正确行为：用户不在底部 → 跳过本次自动滚底。
    expect(scrollToSpy).not.toHaveBeenCalled();
  });

  // 测试目的：正向对照——用户正处于底部时，新事件到达仍应自动滚底跟随最新内容，
  //   证明守卫只拦截「上滚阅读」场景而非禁用自动滚底。
  // 可能发现的缺陷：无（此用例应 PASS；若失败说明守卫过度拦截，破坏流式跟随）。
  it("对照：用户正在底部时，新事件到达应继续自动滚底", () => {
    render(<ChatPanel onPickWorkspace={vi.fn()} />);
    scrollToSpy.mockClear();

    // 模拟用户位于底部：scrollTop = scrollHeight - clientHeight。
    stubScrollGeometry(500);

    nowValue += 1000;
    const events = [makeEvent("e0", 0), makeEvent("e1", 1)];
    act(() => {
      useEventStore.setState({
        events,
        eventsByTaskId: { [TASK_ID]: events },
        eventsByTurnId: { "turn-1": events },
      } as never);
    });

    expect(scrollToSpy).toHaveBeenCalled();
  });

  // 测试目的：正向对照——事件追加确实触发了滚动 effect 的求值路径（harness 有效性），
  //   即「挂载时」滚动 effect 会正常执行一次滚底。
  // 可能发现的缺陷：无（此用例应 PASS；若失败说明 scrollTo 从未接通，主用例结论无效）。
  it("对照：挂载完成且 totalSize>0 时，滚动 effect 至少执行过一次滚底", () => {
    render(<ChatPanel onPickWorkspace={vi.fn()} />);
    expect(scrollToSpy).toHaveBeenCalled();
  });

  it("切换任务时即使处于 200ms 节流窗口内也应滚底", () => {
    render(<ChatPanel onPickWorkspace={vi.fn()} />);
    expect(scrollToSpy).toHaveBeenCalled();
    scrollToSpy.mockClear();

    act(() => {
      useTaskStore.setState({
        activeTaskId: "task-scroll-2",
        activeTurnId: "turn-2",
        tasksById: {
          "task-scroll-2": {
            task_id: "task-scroll-2",
            workspace_id: "ws-1",
            agent_id: "dev",
            input_text: "second task",
            title: "second task",
            last_message_preview: "",
            latest_turn_id: "turn-2",
            status: "running",
            execution_status: "running",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          } satisfies TaskRecord,
        },
        tasksByWorkspaceId: {
          "ws-1": [
            {
              task_id: "task-scroll-2",
              workspace_id: "ws-1",
              agent_id: "dev",
              input_text: "second task",
              title: "second task",
              last_message_preview: "",
              latest_turn_id: "turn-2",
              status: "running",
              execution_status: "running",
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            } satisfies TaskRecord,
          ],
        },
      } as never);
      useTurnStore.setState({
        turnsByTaskId: {
          "task-scroll-2": [
            {
              turn_id: "turn-2",
              task_id: "task-scroll-2",
              input_text: "second task",
              status: "running",
              end_reason: null,
              response_text: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            } satisfies TurnRecord,
          ],
        },
      } as never);
      useEventStore.setState({
        events: [],
        eventsByTaskId: { "task-scroll-2": [] },
        eventsByTurnId: { "turn-2": [] },
      } as never);
    });

    expect(scrollToSpy).toHaveBeenCalled();
  });
});
