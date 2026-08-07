// @vitest-environment happy-dom
/**
 * useSSE 快速重连时攒批缓冲事件丢失缺陷验证测试。
 *
 * 背景：`useSSE.connect` 开头做两件事：
 * 1. `connectionRef.current.disconnect()` —— 调用的是 **SSEConnection.disconnect**，
 *    它只负责 abort fetch，**不会 flush 攒批缓冲**（hook 自身的 disconnect 才会 flush）；
 * 2. `pendingEventsRef.current = []` —— 直接清空共享缓冲。
 *
 * 二者组合的后果：上一连接已通过 `onEvent` 推入缓冲、但尚未到下一动画帧 flush 的事件，
 * 在快速重连（切换 turn / 失败重试）时被静默丢弃，既不入 store 也不触发状态同步。
 *
 * 代码注释声称「disconnect 内部会兜底 flush 旧缓冲，确保残留事件落盘」，
 * 本测试验证该注释与实际行为是否相符。
 *
 * @module tests/useSSE.reconnectBuffer
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

/** 捕获每个 SSEConnection 实例，便于测试手动投递事件。 */
const instances: Array<{
  onEvent: (e: RuntimeEvent) => void;
  disconnect: ReturnType<typeof vi.fn>;
  connect: ReturnType<typeof vi.fn>;
}> = [];

vi.mock("@/services/sse", async () => {
  const actual = await vi.importActual<typeof import("@/services/sse")>("@/services/sse");
  return {
    ...actual,
    SSEConnection: class {
      onEvent: (e: RuntimeEvent) => void;
      disconnect = vi.fn();
      // connect 返回永不 resolve 的 Promise，模拟「连接仍在飞行中」
      connect = vi.fn(() => new Promise<void>(() => {}));
      constructor(options: { onEvent: (e: RuntimeEvent) => void }) {
        this.onEvent = options.onEvent;
        instances.push(this as never);
      }
    },
  };
});

import { useSSE } from "@/hooks/useSSE";
import { useEventStore } from "@/stores/eventStore";

/**
 * 构造运行时事件。
 *
 * @param id - event_id。
 * @param turnId - 所属轮次。
 * @returns RuntimeEvent。
 */
function ev(id: string, turnId: string): RuntimeEvent {
  return {
    event_id: id,
    task_id: "task-1",
    turn_id: turnId,
    event_type: "model_output_delta",
    payload: { text: id },
    sequence: 1,
    created_at: new Date().toISOString(),
  } as unknown as RuntimeEvent;
}

describe("useSSE 快速重连缓冲", () => {
  beforeEach(() => {
    instances.length = 0;
    useEventStore.getState().clearEvents();
  });

  it("重连前上一连接的未 flush 缓冲事件不应丢失", async () => {
    const { result } = renderHook(() => useSSE());

    // 建立第一个连接
    await act(async () => {
      await result.current.connect("task-1", "turn-1");
    });
    expect(instances).toHaveLength(1);

    // 第一个连接推入一个事件（进入攒批缓冲，尚未到下一动画帧）
    act(() => {
      instances[0].onEvent(ev("lost-1", "turn-1"));
    });

    // 立即重连到新 turn（快速重试 / 切换轮次）
    await act(async () => {
      await result.current.connect("task-1", "turn-2");
    });

    // 等待动画帧，让任何应有的 flush 完成
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    const ids = useEventStore.getState().events.map((e) => e.event_id);
    // 该事件已由后端投递并被 onEvent 接收，不应因重连而静默丢弃
    expect(ids).toContain("lost-1");
  });
});
