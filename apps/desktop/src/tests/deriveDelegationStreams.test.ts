// @vitest-environment happy-dom
/**
 * deriveDelegationStreams 数据结构契约验证（D 类边界，独立验证）。
 *
 * 说明（重要）：`deriveDelegationStreams` 与 `isChildRunTerminalDescriptor` 在 `useDelegationStreams.ts`
 * 中均为文件内私有函数（未 export），无法从测试文件直接 import 做纯函数单元测。这是被测代码的
 * 可测性局限（见交付报告「建议」）。本文件改用 hook 集成层间接验证其消费契约：
 *   - 消费方（useDelegationStreams）在 delegation 级终态到达、但 child run 未终态时，不依据
 *     delegationTerminal 断开连接（证明 hook 区分 delegationTerminal 与 childTerminal 两个字段，
 *     且 disconnect 依据 childTerminal 而非 delegationTerminal）。
 *   - 消费方在 child run 终态到达时主动断开连接（证明 childTerminal 字段被正确消费）。
 * 行为层面的差异即印证两字段均存在且语义独立。
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

const TASK_ID = "task-d-derive";
const PARENT_TURN_ID = "turn-parent";
const CHILD_TURN_ID = "turn-child";
const DELEGATION_ID = "delegation-1";

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

function delegationChildStarted(): RuntimeEvent {
  return runtimeEvent(
    "child-started",
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

describe("deriveDelegationStreams 数据结构契约（D，通过 hook 集成间接验证）", () => {
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

  // 测试目的：delegation 级终态（delegationTerminal=true）到达时，child run 未终态（childTerminal=false），
  // 连接仍保持打开。证明 hook 的 disconnect 判定不消费 delegationTerminal 字段（否则此处会被断开）。
  // 可能发现的缺陷：disconnect 误依据 delegationTerminal → 流被提前断开，丢失滞后 child run 事件。
  it("D | delegation 级终态到达但 child run 未终态：连接仍打开（disconnect 不依据 delegationTerminal）", async () => {
    const stream = controlledStreamResponse();
    const fetchMock = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchMock);

    const delegationFinished = runtimeEvent(
      "delegation-finished",
      "delegation_finished",
      PARENT_TURN_ID,
      { delegation_id: DELEGATION_ID, child_turn_id: CHILD_TURN_ID, status: "completed" },
      2,
    );

    renderHook(() => useDelegationStreams(TASK_ID));
    act(() => {
      useEventStore.getState().appendEvent(delegationChildStarted());
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/turns/${CHILD_TURN_ID}/events/stream`,
        expect.objectContaining({ headers: expect.objectContaining({ Accept: "text/event-stream" }) }),
      );
    });

    await act(async () => {
      useEventStore.getState().appendEvent(delegationFinished);
    });
    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTaskId[TASK_ID] ?? []).map((event) => event.event_id),
      ).toContain("delegation-finished");
    });

    // delegation 级终态（delegationTerminal=true）不应断开连接。
    expect(stream.cancelled()).toBe(false);
  });

  // 测试目的：child run 终态（childTerminal=true）到达时，连接被主动断开，且事件已落库。
  // 证明 hook 正确消费 childTerminal 字段作为 disconnect 依据。
  // 说明：happy-dom 的 ReadableStream 在 abort 时不中断 reader.read()，故用 disconnect spy 断言断开被触发。
  // 可能发现的缺陷：childTerminal 字段未被消费 / disconnect 不触发 / delegation 终态误判未断开。
  it("D | child run 终态到达：连接被主动断开且事件已落库（disconnect 依据 childTerminal）", async () => {
    const stream = controlledStreamResponse();
    const fetchMock = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchMock);

    const disconnectedChildTurnIds: string[] = [];
    const disconnectSpy = vi
      .spyOn(DelegationStreamConnection.prototype, "disconnect")
      .mockImplementation(function (this: DelegationStreamConnection) {
        disconnectedChildTurnIds.push(
          (this as unknown as { options: { childTurnId: string } }).options.childTurnId,
        );
      });

    const childFinished = runtimeEvent(
      "child-finished",
      "run_finished",
      CHILD_TURN_ID,
      { status: "completed" },
      2,
    );

    renderHook(() => useDelegationStreams(TASK_ID));
    act(() => {
      useEventStore.getState().appendEvent(delegationChildStarted());
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/turns/${CHILD_TURN_ID}/events/stream`,
        expect.objectContaining({ headers: expect.objectContaining({ Accept: "text/event-stream" }) }),
      );
    });

    // 不 close 流，验证 hook 主动断开。
    await act(async () => {
      stream.enqueue(sseFrame(childFinished));
    });
    await waitFor(() => {
      expect(
        (useEventStore.getState().eventsByTurnId[CHILD_TURN_ID] ?? []).map((event) => event.event_id),
      ).toContain("child-finished");
    });
    // child run 终态驱动 disconnect（消费 childTerminal 字段）。
    await waitFor(() => {
      expect(disconnectedChildTurnIds).toContain(CHILD_TURN_ID);
    });

    disconnectSpy.mockRestore();
  });
});
