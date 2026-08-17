// @vitest-environment happy-dom
/**
 * H2 回归护栏：`useSSE` 连接池按 turnId 隔离。
 *
 * 原缺陷：hook 只持有单个 `connectionRef`。多 task 并发流式时：
 * - 新 turn 建连会无条件 disconnect 上一个连接 → 后台 task 的事件流被掐断，
 *   其 turn 永远收不到终态（停在 running）；
 * - `disconnect()` 是全局的，无法「只断某个 turn」。
 *
 * 修复形态：`connectionsRef = useRef<Map<turnId, SSEConnection>>`，
 * `connect(taskId, turnId)` 只断开**同一 turnId** 的旧连接，
 * `disconnectTurn(turnId)` 只断指定 turn，`disconnectAll()` 断全部。
 *
 * 测试策略：mock `@/services/sse` 的 SSEConnection，捕获每个实例并暴露其
 * `disconnect` spy 与构造参数，从而在不发真实请求的前提下断言连接池隔离性。
 * `connect` 返回永不 resolve 的 Promise，模拟「连接仍在飞行中」，避免 finally
 * 提前把连接从池中摘除而干扰隔离断言。
 *
 * @module tests/useSSE.connectionPoolIsolation
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

/** 被捕获的 SSEConnection 实例形态。 */
interface CapturedConn {
  taskId: string;
  turnId: string;
  onEvent: (e: RuntimeEvent) => void;
  disconnect: ReturnType<typeof vi.fn>;
  connect: ReturnType<typeof vi.fn>;
}

/** 按创建顺序捕获全部 SSEConnection 实例。 */
const instances: CapturedConn[] = [];

vi.mock("@/services/sse", async () => {
  const actual = await vi.importActual<typeof import("@/services/sse")>("@/services/sse");
  return {
    ...actual,
    SSEConnection: class {
      taskId: string;
      turnId: string;
      onEvent: (e: RuntimeEvent) => void;
      disconnect = vi.fn();
      // 永不 resolve：模拟连接仍在飞行中，连接保留在池内。
      connect = vi.fn(() => new Promise<void>(() => {}));
      constructor(options: { taskId: string; turnId: string; onEvent: (e: RuntimeEvent) => void }) {
        this.taskId = options.taskId;
        this.turnId = options.turnId;
        this.onEvent = options.onEvent;
        instances.push(this as unknown as CapturedConn);
      }
    },
  };
});

import { useSSE } from "@/hooks/useSSE";
import { useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";

/** 取出指定 turnId 对应的被捕获连接（同 turnId 多次建连时取最后一个）。 */
function connOf(turnId: string): CapturedConn {
  const found = instances.filter((c) => c.turnId === turnId);
  if (found.length === 0) throw new Error(`未找到 turnId=${turnId} 的连接实例`);
  return found[found.length - 1]!;
}

beforeEach(() => {
  instances.length = 0;
  useEventStore.getState().clearEvents();
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
});

describe("useSSE - H2：返回契约与 disconnectTurn 签名", () => {
  it("hook 返回 connect / disconnectTurn / disconnectAll / connectionState", () => {
    // 测试目的：锁死 H2 修复后的公开 API 形态（调用方 useTask 依赖 disconnectTurn）。
    // 可能发现缺陷：仍导出旧的无参 disconnect()，则「只断某个 turn」无法表达，
    //   调用方只能全局断连，并发流式必然互相掐断。
    const { result } = renderHook(() => useSSE());
    expect(result.current.connect).toBeTypeOf("function");
    expect(result.current.disconnectTurn).toBeTypeOf("function");
    expect(result.current.disconnectAll).toBeTypeOf("function");
    // disconnectTurn 必须接收 turnId 参数（而非无参全局断连）。
    expect(result.current.disconnectTurn.length).toBe(1);
    // 旧的无参 disconnect 不应再作为公开 API 存在（避免两套语义并存被误用）。
    expect((result.current as unknown as Record<string, unknown>).disconnect).toBeUndefined();
  });

  it("disconnectTurn 对不存在的 turn 是 no-op：不抛异常、不误断已有连接", () => {
    // 测试目的：useTask 会对「可能尚未建连的临时 turnId」调用 disconnectTurn，
    //   必须安全。可能发现缺陷：未做 Map.get 存在性判断而对 undefined 调用
    //   .disconnect() 抛 TypeError，或退化为遍历全池断连（误伤并发 turn）。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-1", "turn-1");
    });
    expect(instances).toHaveLength(1);

    expect(() => {
      act(() => {
        result.current.disconnectTurn("turn-does-not-exist");
      });
    }).not.toThrow();

    // 已存在的连接未被牵连
    expect(connOf("turn-1").disconnect).not.toHaveBeenCalled();
  });

  it("对空字符串 turnId 调用 disconnectTurn：同样是安全 no-op", () => {
    // 测试目的：边界输入（空串）不得被当作「通配」而清空整个连接池。
    // 可能发现缺陷：falsy 判断写成 `if (!turnId) disconnectAll()` 之类的危险兜底。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-1", "turn-1");
    });
    act(() => {
      result.current.disconnectTurn("");
    });
    expect(connOf("turn-1").disconnect).not.toHaveBeenCalled();
  });
});

describe("useSSE - H2：并发两 turn 互不断开（原缺陷核心场景）", () => {
  it("connect 两个不同 turnId：两个连接都被建立，且旧连接未被断开", () => {
    // 测试目的：H2 的核心断言之一——新建连接不得掐断其它 turn 的在途连接。
    // 可能发现缺陷：若回退为单 connectionRef，第二次 connect 会 disconnect 第一个连接，
    //   后台 task 的事件流被掐断，其 turn 永远停在 running。
    const { result } = renderHook(() => useSSE());

    act(() => {
      void result.current.connect("task-A", "turn-A");
    });
    act(() => {
      void result.current.connect("task-B", "turn-B");
    });

    expect(instances).toHaveLength(2);
    expect(instances.map((c) => c.turnId)).toEqual(["turn-A", "turn-B"]);
    // 关键断言：turn-A 的连接未因 turn-B 建连而被断开。
    expect(connOf("turn-A").disconnect).not.toHaveBeenCalled();
    expect(connOf("turn-B").disconnect).not.toHaveBeenCalled();
    // 两个连接都真的发起了（connect 被调用）。
    expect(connOf("turn-A").connect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-B").connect).toHaveBeenCalledTimes(1);
  });

  it("disconnectTurn(turn-A)：仅 turn-A 被断开，turn-B 仍在", () => {
    // 测试目的：H2 的核心断言之二——定向断连不得波及其它 turn。
    // 可能发现缺陷：disconnectTurn 内部误调 disconnectAll / 遍历整池，
    //   导致取消 A 的同时把 B 的流也掐断（用户表现：另一个会话突然卡死）。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
    });
    act(() => {
      void result.current.connect("task-B", "turn-B");
    });

    act(() => {
      result.current.disconnectTurn("turn-A");
    });

    expect(connOf("turn-A").disconnect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-B").disconnect).not.toHaveBeenCalled();
  });

  it("断开 turn-A 后再断 turn-B：各自恰好被断一次（无重复断连）", () => {
    // 测试目的：连接被断后应从池中摘除，二次断连不得重复调用 disconnect。
    // 可能发现缺陷：只 disconnect 未 delete，残留条目在 disconnectAll 时被重复断开，
    //   或 onStateChange 竞态回写把已废弃连接的 CLOSED 状态钉到 UI 上。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
    });
    act(() => {
      void result.current.connect("task-B", "turn-B");
    });

    act(() => {
      result.current.disconnectTurn("turn-A");
      result.current.disconnectTurn("turn-A"); // 重复调用应为 no-op
      result.current.disconnectTurn("turn-B");
    });

    expect(connOf("turn-A").disconnect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-B").disconnect).toHaveBeenCalledTimes(1);
  });

  it("三 turn 并发：断中间一个，另外两个不受影响", () => {
    // 测试目的：把隔离性放大到 3 条并发连接，验证「断 1 剩 2」的不变式；
    // 可能发现缺陷：Map 键使用错误（如用 taskId 作键）导致多 turn 相互覆盖，
    //   池内实际只留最后一条，断连行为错乱。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
      void result.current.connect("task-B", "turn-B");
      void result.current.connect("task-C", "turn-C");
    });
    expect(instances).toHaveLength(3);

    act(() => {
      result.current.disconnectTurn("turn-B");
    });

    expect(connOf("turn-B").disconnect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-A").disconnect).not.toHaveBeenCalled();
    expect(connOf("turn-C").disconnect).not.toHaveBeenCalled();
  });

  it("同一 turnId 重复 connect：旧连接被断开并替换（同 task 内 turn 串行）", () => {
    // 测试目的：隔离不等于放任堆积——同一 turnId 重连时旧连接必须被断开回收，
    //   否则同一 turn 出现两条并行流，事件重复入 store（UI 内容翻倍）。
    // 可能发现缺陷：为实现隔离而完全去掉了同 turn 的旧连接清理。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
    });
    act(() => {
      void result.current.connect("task-B", "turn-B");
    });
    const firstA = instances.filter((c) => c.turnId === "turn-A")[0]!;

    act(() => {
      void result.current.connect("task-A", "turn-A"); // 同 turnId 重连
    });

    const aConns = instances.filter((c) => c.turnId === "turn-A");
    expect(aConns).toHaveLength(2);
    // 同 turnId 的旧连接被断开
    expect(firstA.disconnect).toHaveBeenCalledTimes(1);
    // 新连接未被断开
    expect(aConns[1]!.disconnect).not.toHaveBeenCalled();
    // 其它 turn 完全不受影响（这才是 H2 的价值）
    expect(connOf("turn-B").disconnect).not.toHaveBeenCalled();
  });
});

describe("useSSE - H2：disconnectAll 与并发池的关系", () => {
  it("disconnectAll：池内所有连接都被断开", () => {
    // 测试目的：全局清理（组件卸载）必须覆盖池内全部连接，不能只断最后一个。
    // 可能发现缺陷：改为 Map 后 disconnectAll 忘记遍历，只处理单个引用，
    //   造成卸载后仍有 fetch 在飞（内存泄漏 + 幽灵事件写入已卸载组件的 store）。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
      void result.current.connect("task-B", "turn-B");
      void result.current.connect("task-C", "turn-C");
    });

    act(() => {
      result.current.disconnectAll();
    });

    expect(connOf("turn-A").disconnect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-B").disconnect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-C").disconnect).toHaveBeenCalledTimes(1);
  });

  it("disconnectAll 后池已清空：再次 disconnectTurn 为 no-op（不重复断连）", () => {
    // 测试目的：disconnectAll 必须 clear 整个 Map；
    // 可能发现缺陷：只遍历断开未清空，后续 disconnectTurn 会对已断连接重复调用。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
      void result.current.connect("task-B", "turn-B");
    });
    act(() => {
      result.current.disconnectAll();
    });
    act(() => {
      result.current.disconnectTurn("turn-A");
      result.current.disconnectTurn("turn-B");
    });
    expect(connOf("turn-A").disconnect).toHaveBeenCalledTimes(1);
    expect(connOf("turn-B").disconnect).toHaveBeenCalledTimes(1);
  });

  it("空池上调用 disconnectAll：不抛异常", () => {
    // 测试目的：边界——从未建连就卸载组件（用户秒退）时不得崩溃。
    // 可能发现缺陷：对 undefined 引用调用 forEach/disconnect。
    const { result } = renderHook(() => useSSE());
    expect(() => {
      act(() => {
        result.current.disconnectAll();
      });
    }).not.toThrow();
    expect(instances).toHaveLength(0);
  });
});

describe("useSSE - H2：连接参数与 taskId/turnId 对应关系", () => {
  it("connect(taskId, turnId) 正确透传到 SSEConnection 构造参数", () => {
    // 测试目的：池按 turnId 建键的前提是构造参数正确；若 taskId/turnId 传参顺序颠倒，
    //   池键与实际流会错配，disconnectTurn 断错连接。
    // 可能发现缺陷：参数错位（connect 内把 turnId 当 taskId 传给 SSEConnection）。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
      void result.current.connect("task-B", "turn-B");
    });
    expect(connOf("turn-A").taskId).toBe("task-A");
    expect(connOf("turn-B").taskId).toBe("task-B");
  });

  it("断开某 turn 不影响另一 turn 继续投递事件入 store", () => {
    // 测试目的：从「数据是否还能落盘」这一用户可感知维度验证隔离，
    //   而非只看 disconnect spy。可能发现缺陷：定向断连时误清共享的攒批缓冲/
    //   或误断另一连接，导致 B 的后续事件全部丢失（UI 停止更新）。
    const { result } = renderHook(() => useSSE());
    act(() => {
      void result.current.connect("task-A", "turn-A");
      void result.current.connect("task-B", "turn-B");
    });

    act(() => {
      result.current.disconnectTurn("turn-A");
    });

    // turn-B 的连接仍活着，继续投递事件
    act(() => {
      connOf("turn-B").onEvent({
        event_id: "b-1",
        task_id: "task-B",
        turn_id: "turn-B",
        event_type: "model_output_delta",
        payload: { text: "still alive" },
        sequence: 1,
        created_at: new Date().toISOString(),
      } as unknown as RuntimeEvent);
    });

    // 触发 flush（rAF/定时器）
    act(() => {
      result.current.disconnectTurn("turn-B");
    });

    const ids = useEventStore.getState().events.map((e) => e.event_id);
    expect(ids).toContain("b-1");
  });
});
