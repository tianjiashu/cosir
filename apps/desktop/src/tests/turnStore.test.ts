import { beforeEach, describe, expect, it } from "vitest";
import { useTurnStore } from "@/stores/turnStore";
import type { TurnRecord } from "@shared/turn";

beforeEach(() => {
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnId: null });
});

function makeTurn(turnId: string, overrides: Partial<TurnRecord> = {}): TurnRecord {
  const now = new Date().toISOString();
  return {
    turn_id: turnId,
    task_id: "task-1",
    input_text: "hello",
    status: "pending",
    end_reason: null,
    response_text: null,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

describe("turnStore — 轮次状态更新", () => {
  it("updateTurn 只更新指定 task 下的指定 turn", () => {
    const s = useTurnStore.getState();
    s.setTurnsForTask("task-1", [makeTurn("turn-1"), makeTurn("turn-2")]);
    s.setTurnsForTask("task-2", [makeTurn("turn-1", { task_id: "task-2" })]);

    s.updateTurn("task-1", "turn-1", {
      status: "failed",
      end_reason: "boom",
      updated_at: "2026-07-23T00:00:00.000Z",
    });

    const state = useTurnStore.getState();
    expect(state.turnsByTaskId["task-1"][0]).toMatchObject({
      turn_id: "turn-1",
      status: "failed",
      end_reason: "boom",
      updated_at: "2026-07-23T00:00:00.000Z",
    });
    expect(state.turnsByTaskId["task-1"][1].status).toBe("pending");
    expect(state.turnsByTaskId["task-2"][0].status).toBe("pending");
  });

  it("updateTurn 找不到目标 turn 时保持该 task 列表为空", () => {
    useTurnStore.getState().updateTurn("missing-task", "turn-1", { status: "completed" });
    expect(useTurnStore.getState().turnsByTaskId["missing-task"]).toEqual([]);
  });
});
