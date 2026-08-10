/**
 * SSEConnection 生命周期缺陷验证测试。
 *
 * 目标：验证以下疑似缺陷：
 * - connect() 在 fetch 已发起后被 disconnect()，`_abortController` 被置 null，
 *   导致后续无法再次 abort（连接状态判定与资源释放的一致性）。
 * - connect() 抛错后 `_abortController` 未清理（泄漏 + 无法重连时状态残留）。
 * - 收到终态事件后流仍继续，`_terminalReceived` 无法回退，
 *   同一实例复用时的状态串扰（已由 connect 开头重置覆盖，作为回归护栏）。
 *
 * @module tests/sse.lifecycle
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { SSEConnection, SSEConnectionState } from "@/services/sse";
import type { RuntimeEvent } from "@shared/events";

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

/**
 * 构造一个把给定文本分片推送的 SSE 响应。
 *
 * @param chunks - 要按顺序推送的原始 SSE 文本分片。
 * @param onCancel - reader 被取消时的回调。
 * @returns 可作为 fetch 返回值的 Response-like 对象。
 */
function makeStreamResponse(chunks: string[], onCancel?: () => void): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[i]));
      i += 1;
    },
    cancel() {
      onCancel?.();
    },
  });
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers(),
    body: stream,
  } as unknown as Response;
}

/** 构造一条 SSE 帧文本。 */
function frame(eventType: RuntimeEvent["event_type"], payload: Record<string, unknown>): string {
  const event = {
    event_id: `evt-${Math.random().toString(36).slice(2)}`,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as RuntimeEvent;
  return `event: ${eventType}\ndata: ${JSON.stringify(event)}\n\n`;
}

describe("SSEConnection 生命周期", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("正常收到终态事件后关闭，不上报异常结束", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => makeStreamResponse([frame("run_started", {}), frame("run_finished", {})])),
    );
    const onError = vi.fn();
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError,
    });
    await conn.connect();
    expect(onError).not.toHaveBeenCalled();
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
  });

  it("未收到终态事件而流结束时上报异常", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([frame("run_started", {})])));
    const onError = vi.fn();
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError,
    });
    await conn.connect();
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("connect 抛错后应清理 AbortController，不残留已失效的中止句柄", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        headers: new Headers(),
        body: null,
      })),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    await expect(conn.connect()).rejects.toThrow();

    // 失败后 controller 应已释放；否则实例上会残留一个永远不会被用到的 controller。
    const leaked = (conn as unknown as { _abortController: AbortController | null })._abortController;
    expect(leaked).toBeNull();
  });

  it("disconnect 后再次 disconnect 不应因 controller 被置 null 而漏掉中止", async () => {
    let cancelled = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Promise<Response>((resolve) => {
            setTimeout(
              () =>
                resolve(
                  makeStreamResponse([frame("run_started", {})], () => {
                    cancelled += 1;
                  }),
                ),
              5,
            );
          }),
      ),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    const p = conn.connect();
    // fetch 尚未 resolve 时断开：abort 生效，connect 走 AbortError 分支
    conn.disconnect();
    await p;
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
    expect(cancelled).toBe(0);
  });

  it("disconnect 后仍存活（不置 null），可兜底多次 disconnect 中止", async () => {
    // 验证修复点：disconnect() 只 abort 不置 null，controller 在 connect 飞行期间
    // 始终存活，连续多次 disconnect 都能通过同一 controller 兜底中止。
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            // 模拟真实 fetch：signal abort 时 reader 抛 AbortError，fetch 层面 reject。
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted", "AbortError"));
            });
          }),
      ),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    const p = conn.connect();
    // 等微任务让 connect 创建 controller
    await new Promise((r) => setTimeout(r, 10));

    conn.disconnect();
    const afterFirst = (conn as unknown as { _abortController: AbortController | null })
      ._abortController;
    expect(afterFirst).not.toBeNull();

    // 第二次 disconnect 仍能访问同一 controller 并 abort（幂等性回归护栏）
    conn.disconnect();
    const afterSecond = (conn as unknown as { _abortController: AbortController | null })
      ._abortController;
    expect(afterSecond).not.toBeNull();

    // 让 connect 收尾：abort 后 fetch 因 signal abort reject（AbortError），
    // connect 的 catch(AbortError) 分支视为正常取消并 resolve，finally 置 null + CLOSED。
    await p;
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
  });
});
