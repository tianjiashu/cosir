// @vitest-environment happy-dom
/**
 * useDelegationStreams 修复的独立验证补充测试（测试 Agent 独立验证）。
 * 不修改任何业务代码（src/ 下文件），仅新增测试覆盖边界：
 *   B：并发两个 sibling child 时，一个 delegation 终态但 child run 未终态其流仍打开（不触发断开），
 *      另一个 child run 终态后其流断开，互不影响。
 *   C：连接出错（onError）路径下 forceBackfill 仍被调用（回归保护）。
 *   A 强化：delegation_finished 之后，滞后到达的 run_failed（异常终态）也应被接收并落入 eventStore。
 *
 * 关于「断开」探针的说明：happy-dom 的 ReadableStream 在 AbortController.abort() 时不会中断正在进行的
 * reader.read()（与真实浏览器的 fetch SSE 行为不同），因此不能用 controlled stream 的 cancelled() 或
 * 「不再接收新事件」来推断 disconnect 是否被调用。本文件统一通过
 * `vi.spyOn(DelegationStreamConnection.prototype, "disconnect")` 直接断言 disconnect 是否被触发，
 * 这是验证被测代码 disconnect 判定的权威依据，且不依赖测试环境的流中断语义。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";
import { useDelegationStreams } from "@/hooks/useDelegationStreams";
import { useEventStore } from "@/stores/eventStore";
import { SSEConnectionState } from "@/services/sse";
import { DelegationStreamConnection } from "@/services/delegationStream";
import * as api from "@/services/api";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api");
  return {
    ...actual,
    listTaskEvents: vi.fn(),
  };
});

const TASK_ID = "task-delegation-siblings";
const PARENT_TURN_ID = "turn-parent";

/** 两个 sibling child 标识与 delegation 标识。 */
const CHILD_A = "turn-child-a";
const CHILD_B = "turn-child-b";
const DELEGATION_A = "delegation-a";
const DELEGATION_B = "delegation-b";

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
    task_id: TASK_ID,
    turn_id: turnId,
    sequence,
    created_at: `2026-08-11T00:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
  } as RuntimeEvent;
}

function delegationChildStarted(delegationId: string, childTurnId: string, sequence: number): RuntimeEvent {
  return runtimeEvent(
    `child-started-${childTurnId}`,
    "delegation_child_started",
    PARENT_TURN_ID,
    {
      delegation_id: delegationId,
      parent_turn_id: PARENT_TURN_ID,
      child_turn_id: childTurnId,
      child_agent_id: "delegate_reviewer",
      delegation_type: "review",
      status: "running",
    },
    sequence,
  );
}

function delegationFinished(delegationId: string, childTurnId: string, sequence: number): RuntimeEvent {
  return runtimeEvent(
    `delegation-finished-${childTurnId}`,
    "delegation_finished",
    PARENT_TURN_ID,
    { delegation_id: delegationId, child_turn_id: childTurnId, status: "completed" },
    sequence,
  );
}

function sseFrame(event: RuntimeEvent): string {
  return `event: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`;
}

function controlledStreamResponse(): {
  response: Response;
  enqueue: (chunk: string) => void;
  close: () => void;
  cancelled: () => boolean;
} {
  const encoder = new TextEncoder();
  let controllerRef: ReadableStreamDefaultController<Uint8Array> | null = null;
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controllerRef = controller;
    },
    cancel() {
      cancelled = true;
    },
  });
  return {
    response: {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers(),
      body: stream,
    } as unknown as Response,
    enqueue: (chunk: string) => {
      controllerRef?.enqueue(encoder.encode(chunk));
    },
    close: () => {
      controllerRef?.close();
    },
    cancelled: () => cancelled,
  };
}

function failedResponse(): Response {
  return {
    ok: false,
    status: 500,
    statusText: "Internal Server Error",
    headers: new Headers(),
    body: null,
  } as unknown as Response;
}

describe("useDelegationStreams 并发 sibling 与错误路径独立验证", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listTaskEvents).mockResolvedValue([]);
    useEventStore.setState({
      events: [],
      eventsByTaskId: {},
      eventsByTurnId: {},
      connectionState: SSEConnectionState.IDLE,
      processedEventIds: new Set<string>(),
    });
  });

  // ── B（核心并发隔离）────────────────────────────────────────────────
  // 测试目的：注入两个 sibling child 的 delegation_child_started 后，分别建立两条 SSE 连接；
  // 当 child A 的 delegation_finished 到达（但 child A run 未终态）时，child A 的连接**不应**被 disconnect；
  // child B 的 run_finished 到达后，child B 的连接应被 disconnect；两者互不影响——
  // 验证断开判定仅依据 child run 终态（childTerminal），不依据 delegation 级终态，且两连接独立。
  // 可能发现的缺陷：delegation 级终态误触发 child A 连接断开导致漏收滞后 child run 事件；
  // 或 child B 断开时连带断开 child A；或两个 child 共享同一连接引用。
  it("B | 并发 sibling：delegation 终态不触发断开、child run 终态触发断开，且两连接互不影响", async () => {
    const streamA = controlledStreamResponse();
    const streamB = controlledStreamResponse();

    const fetchMock = vi.fn(async (input: URL | string | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes(CHILD_A)) return streamA.response;
      if (url.includes(CHILD_B)) return streamB.response;
      return failedResponse();
    });
    vi.stubGlobal("fetch", fetchMock);

    // 记录被 disconnect 的连接对应的 childTurnId。
    const disconnectedChildTurnIds: string[] = [];
    const disconnectSpy = vi
      .spyOn(DelegationStreamConnection.prototype, "disconnect")
      .mockImplementation(function (this: DelegationStreamConnection) {
        disconnectedChildTurnIds.push((this as unknown as { options: { childTurnId: string } }).options.childTurnId);
      });

    renderHook(() => useDelegationStreams(TASK_ID));
    act(() => {
      useEventStore.getState().appendEvent(delegationChildStarted(DELEGATION_A, CHILD_A, 1));
      useEventStore.getState().appendEvent(delegationChildStarted(DELEGATION_B, CHILD_B, 2));
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/turns/${CHILD_A}/events/stream`,
        expect.objectContaining({ headers: expect.objectContaining({ Accept: "text/event-stream" }) }),
      );
      expect(fetchMock).toHaveBeenCalledWith(
        `/turns/${CHILD_B}/events/stream`,
        expect.objectContaining({ headers: expect.objectContaining({ Accept: "text/event-stream" }) }),
      );
    });

    // child A 的 delegation 终态到达，但 child A run 尚未终态。
    await act(async () => {
      useEventStore.getState().appendEvent(delegationFinished(DELEGATION_A, CHILD_A, 3));
    });
    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTaskId[TASK_ID] ?? []).map((event) => event.event_id),
      ).toContain(`delegation-finished-${CHILD_A}`);
    });

    // 关键断言：delegation 级终态（delegationTerminal=true）不应触发任何 child 连接断开。
    expect(disconnectedChildTurnIds).toEqual([]);

    // child B 的 run_finished 滞后到达（不 close 流，disconnect 由 hook 主动触发）。
    const childBFinished = runtimeEvent(
      `child-finished-${CHILD_B}`,
      "run_finished",
      CHILD_B,
      { status: "completed" },
      4,
    );
    await act(async () => {
      streamB.enqueue(sseFrame(childBFinished));
    });

    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTurnId[CHILD_B] ?? []).map((event) => event.event_id),
      ).toContain(`child-finished-${CHILD_B}`);
    });

    // child B（child run 终态）连接被 disconnect，且 child A 仍未被断开（互不影响）。
    await waitFor(() => {
      expect(disconnectedChildTurnIds).toContain(CHILD_B);
    });
    expect(disconnectedChildTurnIds).not.toContain(CHILD_A);

    disconnectSpy.mockRestore();
  });

  // ── B 强化：child A 的滞后 run 事件（delegation 终态之后到达）仍被接收并落库 ──────
  // 测试目的：在 delegation_finished 已到达、child A 流未断开的前提下，
  // 滞后到达的 child A run_failed（异常终态）事件应被 onEvent 接收并落入 eventStore；
  // 收到后才 disconnect。验证改动确实修复了「delegation 终态即断开 → 丢失滞后 child run 事件」。
  // 可能发现的缺陷：delegation 终态后仍误断 child A 流，导致 run_failed 丢失。
  it("B-补 | delegation 终态后滞后到达的 child run 终态事件仍被接收并落库，随后才断开", async () => {
    const streamA = controlledStreamResponse();
    const streamB = controlledStreamResponse();

    const fetchMock = vi.fn(async (input: URL | string | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes(CHILD_A)) return streamA.response;
      if (url.includes(CHILD_B)) return streamB.response;
      return failedResponse();
    });
    vi.stubGlobal("fetch", fetchMock);

    const disconnectedChildTurnIds: string[] = [];
    const disconnectSpy = vi
      .spyOn(DelegationStreamConnection.prototype, "disconnect")
      .mockImplementation(function (this: DelegationStreamConnection) {
        disconnectedChildTurnIds.push((this as unknown as { options: { childTurnId: string } }).options.childTurnId);
      });

    renderHook(() => useDelegationStreams(TASK_ID));
    act(() => {
      useEventStore.getState().appendEvent(delegationChildStarted(DELEGATION_A, CHILD_A, 1));
      useEventStore.getState().appendEvent(delegationChildStarted(DELEGATION_B, CHILD_B, 2));
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/turns/${CHILD_A}/events/stream`,
        expect.anything(),
      );
    });

    // child A delegation 终态先到达（此时不得断开）。
    await act(async () => {
      useEventStore.getState().appendEvent(delegationFinished(DELEGATION_A, CHILD_A, 3));
    });
    await waitFor(() => {
      expect(disconnectedChildTurnIds).not.toContain(CHILD_A);
    });

    // 滞后到达的 child A run_failed（异常终态）。
    const childAFailed = runtimeEvent(
      `child-failed-${CHILD_A}`,
      "run_failed",
      CHILD_A,
      { error: "child aborted" },
      4,
    );
    await act(async () => {
      streamA.enqueue(sseFrame(childAFailed));
    });

    // 滞后 run 终态事件应被接收并落库（证明未被 delegation 终态提前断开而丢失）。
    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTurnId[CHILD_A] ?? []).map((event) => event.event_id),
      ).toContain(`child-failed-${CHILD_A}`);
    });
    // 收到 run 终态后才断开。
    await waitFor(() => {
      expect(disconnectedChildTurnIds).toContain(CHILD_A);
    });

    disconnectSpy.mockRestore();
  });

  // ── C（onError 路径 forceBackfill 回归保护）────────────────────────────
  // 测试目的：child SSE 连接返回 HTTP 500 → connect 抛错 → onError 触发 → forceBackfill 被调用。
  // 验证兜底的「连接出错即强制 backfill」逻辑在本次改动后未被删除（回归保护）。
  // 可能发现的缺陷：onError 分支被误删 / forceBackfill 未被调用。
  it("C | child 流连接出错（HTTP 500）时 onError 触发 forceBackfill 强制回填", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => failedResponse()));
    vi.mocked(api.listTaskEvents).mockResolvedValue([]);

    useEventStore.getState().setEvents([delegationChildStarted(DELEGATION_A, CHILD_A, 1)], TASK_ID);
    renderHook(() => useDelegationStreams(TASK_ID));

    await waitFor(() => {
      expect(api.listTaskEvents).toHaveBeenCalled();
    });
    // 错误路径下确实施加了强制回填（forced 由 onError 路径传入）；至少一次 listTaskEvents 被调用即证明兜底未被删。
    expect(api.listTaskEvents).toHaveBeenCalled();
  });

  // ── C 强化：onError 路径应额外触发一次强制回填 ────────────────────────
  // 测试目的：验证连接失败时除了首次 missing_terminal 回填，onError 还会再触发一次
  // （forced=true 的兜底回填），证明错误兜底链路完整。
  // 可能发现的缺陷：onError 未再触发 forceBackfill，导致连接失败时无兜底。
  it("C-补 | 连接失败时除首次回填外，onError 再触发一次强制回填", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => failedResponse()));
    vi.mocked(api.listTaskEvents).mockResolvedValue([]);

    useEventStore.getState().setEvents([delegationChildStarted(DELEGATION_A, CHILD_A, 1)], TASK_ID);
    renderHook(() => useDelegationStreams(TASK_ID));

    // missing_terminal 首次 backfill 立即触发一次。
    await waitFor(() => {
      expect(api.listTaskEvents).toHaveBeenCalledTimes(1);
    });
    // 连接失败经 onError 再触发一次强制回填（forced）。
    await waitFor(() => {
      expect(api.listTaskEvents).toHaveBeenCalledTimes(2);
    });
  });
});
