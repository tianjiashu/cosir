// @vitest-environment happy-dom

import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTask } from "@/hooks/useTask";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { makeTask } from "@/tests/test-utils/factories";
import * as api from "@/services/api";
import type { TurnRecord } from "@shared/turn";

const hookMocks = vi.hoisted(() => ({
  connect: vi.fn().mockResolvedValue(undefined),
  disconnect: vi.fn(),
  logError: vi.fn(),
}));

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    connect: hookMocks.connect,
    disconnect: hookMocks.disconnect,
  }),
}));

vi.mock("@/lib/logger", () => ({
  logError: (...args: unknown[]) => hookMocks.logError(...args),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logDebug: vi.fn(),
}));

vi.mock("@/services/api", () => ({
  createTask: vi.fn(),
  createTaskTurn: vi.fn(),
  listTaskTurns: vi.fn(),
  listTaskEvents: vi.fn(),
  getTask: vi.fn(),
  cancelTurn: vi.fn(),
}));

type UseTaskValue = ReturnType<typeof useTask>;

let container: HTMLDivElement;
let root: Root;
let currentHook: UseTaskValue;

function HookHarness(): null {
  const task = useTask();

  useEffect(() => {
    currentHook = task;
  }, [task]);

  return null;
}

function renderHookHarness(): void {
  act(() => {
    root.render(<HookHarness />);
  });
}

function makeTurn(turnId: string, overrides: Partial<TurnRecord> = {}): TurnRecord {
  const now = new Date().toISOString();
  return {
    turn_id: turnId,
    task_id: "task-1",
    input_text: "继续",
    status: "pending",
    end_reason: null,
    response_text: null,
    agent_id: "developer",
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

describe("useTask", () => {
  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    useTaskStore.getState().clearTasks();
    useTaskStore.getState().setSelectedAgentId("developer");
    useTurnStore.setState({ turnsByTaskId: {}, streamingTurnId: null });

    vi.mocked(api.createTask).mockReset();
    vi.mocked(api.createTaskTurn).mockReset();
    vi.mocked(api.listTaskTurns).mockReset();
    hookMocks.connect.mockReset().mockResolvedValue(undefined);
    hookMocks.disconnect.mockReset();
    hookMocks.logError.mockReset();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
  });

  it("追加 turn 时使用最新选中的 Agent", async () => {
    vi.mocked(api.createTaskTurn).mockResolvedValue(makeTurn("turn-2", { agent_id: "reviewer" }));
    useTaskStore.getState().addTask(makeTask("task-1", { latest_turn_id: "turn-1" }));

    renderHookHarness();

    act(() => {
      useTaskStore.getState().setSelectedAgentId("reviewer");
    });

    await act(async () => {
      await currentHook.createTurn("继续");
    });

    expect(api.createTaskTurn).toHaveBeenCalledWith("task-1", {
      input_text: "继续",
      agent_id: "reviewer",
    });
  });

  it("并发创建失败时只清理当前失败调用创建的临时任务", async () => {
    const nowSpy = vi.spyOn(Date, "now");
    nowSpy.mockReturnValueOnce(1).mockReturnValueOnce(2);

    let rejectFirst!: (reason?: unknown) => void;
    vi.mocked(api.createTask)
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectFirst = reject;
          }),
      )
      .mockImplementationOnce(() => new Promise(() => undefined));

    renderHookHarness();

    let firstResult!: Promise<boolean>;
    act(() => {
      firstResult = currentHook.createTask("first", "workspace-1");
      void currentHook.createTask("second", "workspace-1");
    });

    await act(async () => {
      rejectFirst(new Error("boom"));
      await firstResult;
    });

    const taskIds = useTaskStore.getState().tasks.map((task) => task.task_id);
    expect(taskIds).not.toContain("temp-1");
    expect(taskIds).toContain("temp-2");
  });
});
