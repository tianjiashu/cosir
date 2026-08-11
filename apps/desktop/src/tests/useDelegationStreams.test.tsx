// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";
import { useDelegationStreams } from "@/hooks/useDelegationStreams";
import { useEventStore } from "@/stores/eventStore";
import { SSEConnectionState } from "@/services/sse";
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

const TASK_ID = "task-delegation";
const PARENT_TURN_ID = "turn-parent";
const CHILD_TURN_ID = "turn-child";
const DELEGATION_ID = "delegation-1";

/**
 * 构造测试用 RuntimeEvent。
 *
 * @param eventId - 事件标识。
 * @param eventType - 运行时事件类型。
 * @param turnId - 事件归属 turn。
 * @param payload - 事件 payload。
 * @param sequence - 排序序号。
 * @returns 测试事件。
 */
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

/**
 * 构造 delegation_child_started 事件。
 *
 * @returns 指向 CHILD_TURN_ID 的委派子流启动事件。
 */
function delegationChildStartedEvent(): RuntimeEvent {
  return runtimeEvent(
    "delegation-child-started",
    "delegation_child_started",
    PARENT_TURN_ID,
    {
      delegation_id: DELEGATION_ID,
      parent_turn_id: PARENT_TURN_ID,
      child_turn_id: CHILD_TURN_ID,
      child_agent_id: "delegate_reviewer",
      delegation_type: "review",
      status: "running",
    },
    1,
  );
}

/**
 * 构造一条 SSE 帧文本。
 *
 * @param event - 要序列化的运行时事件。
 * @returns SSE 帧文本。
 */
function sseFrame(event: RuntimeEvent): string {
  return `event: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`;
}

/**
 * 构造 ReadableStream SSE 响应。
 *
 * @param chunks - 按顺序推送的文本块。
 * @returns fetch Response。
 */
function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[index]));
      index += 1;
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

/**
 * 构造可由测试手动推进或取消的 SSE 响应。
 *
 * @returns fetch Response 与流控制方法。
 */
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

/**
 * 构造失败的 SSE 响应。
 *
 * @returns HTTP 500 Response。
 */
function failedResponse(): Response {
  return {
    ok: false,
    status: 500,
    statusText: "Internal Server Error",
    headers: new Headers(),
    body: null,
  } as unknown as Response;
}

/**
 * 鍒涘缓鍙墜鍔ㄨВ鍐崇殑 Promise銆?
 *
 * @returns Promise 涓庡叾 resolve 鍑芥暟銆?
 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("useDelegationStreams", () => {
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

  it("subscribes to a child turn stream and appends child events without changing parent SSE state", async () => {
    const childDelta = runtimeEvent(
      "child-delta",
      "model_output_delta",
      CHILD_TURN_ID,
      { step_id: "step-1", text: "child details" },
      2,
    );
    const childFinished = runtimeEvent(
      "child-finished",
      "run_finished",
      CHILD_TURN_ID,
      { status: "completed" },
      3,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => streamResponse([sseFrame(childDelta), sseFrame(childFinished)])),
    );
    useEventStore.getState().setConnectionState(SSEConnectionState.STREAMING);

    renderHook(() => useDelegationStreams(TASK_ID));
    act(() => {
      useEventStore.getState().appendEvent(delegationChildStartedEvent());
    });

    await waitFor(() => {
      expect((useEventStore.getState().eventsByTurnId[CHILD_TURN_ID] ?? []).map((event) => event.event_id))
        .toContain("child-delta");
    });

    expect(fetch).toHaveBeenCalledWith(
      `/turns/${CHILD_TURN_ID}/events/stream`,
      expect.objectContaining({
        headers: expect.objectContaining({ Accept: "text/event-stream" }),
      }),
    );
    expect(useEventStore.getState().connectionState).toBe(SSEConnectionState.STREAMING);
  });

  it("forces task event backfill when a child stream cannot continue even if task events are cached", async () => {
    const firstBackfill = deferred<RuntimeEvent[]>();
    const backfilledTerminal = runtimeEvent(
      "child-terminal-backfill",
      "run_failed",
      CHILD_TURN_ID,
      { error: "child stream interrupted" },
      4,
    );
    vi.stubGlobal("fetch", vi.fn(async () => failedResponse()));
    vi.mocked(api.listTaskEvents)
      .mockReturnValueOnce(firstBackfill.promise)
      .mockResolvedValueOnce([delegationChildStartedEvent(), backfilledTerminal]);

    useEventStore.getState().setEvents([delegationChildStartedEvent()], TASK_ID);
    renderHook(() => useDelegationStreams(TASK_ID));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        `/turns/${CHILD_TURN_ID}/events/stream`,
        expect.objectContaining({
          headers: expect.objectContaining({ Accept: "text/event-stream" }),
        }),
      );
    });

    expect(api.listTaskEvents).toHaveBeenCalledTimes(1);
    firstBackfill.resolve([delegationChildStartedEvent()]);

    expect((useEventStore.getState().eventsByTurnId[CHILD_TURN_ID] ?? []).map((event) => event.event_id))
      .not.toContain("child-terminal-backfill");

    await waitFor(() => {
      expect(api.listTaskEvents).toHaveBeenCalledTimes(2);
    });

    await waitFor(() => {
      expect((useEventStore.getState().eventsByTurnId[CHILD_TURN_ID] ?? []).map((event) => event.event_id))
      .toContain("child-terminal-backfill");
    });
  });

  it("keeps child stream open when final_response arrives before run_finished", async () => {
    const stream = controlledStreamResponse();
    const childFinalResponse = runtimeEvent(
      "child-final-response",
      "final_response",
      CHILD_TURN_ID,
      { step_id: "step-1", status: "completed", text: "child final" },
      2,
    );
    const childFinished = runtimeEvent(
      "child-finished",
      "run_finished",
      CHILD_TURN_ID,
      { status: "completed" },
      3,
    );
    vi.stubGlobal("fetch", vi.fn(async () => stream.response));

    renderHook(() => useDelegationStreams(TASK_ID));
    act(() => {
      useEventStore.getState().appendEvent(delegationChildStartedEvent());
    });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        `/turns/${CHILD_TURN_ID}/events/stream`,
        expect.objectContaining({
          headers: expect.objectContaining({ Accept: "text/event-stream" }),
        }),
      );
    });

    await act(async () => {
      stream.enqueue(sseFrame(childFinalResponse));
    });

    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTurnId[CHILD_TURN_ID] ?? []).map((event) => event.event_id),
      ).toContain("child-final-response");
    });
    expect(stream.cancelled()).toBe(false);

    await act(async () => {
      stream.enqueue(sseFrame(childFinished));
      stream.close();
    });

    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTurnId[CHILD_TURN_ID] ?? []).map((event) => event.event_id),
      ).toContain("child-finished");
    });
  });
});
