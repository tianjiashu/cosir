import { describe, it, expect, beforeEach } from "vitest";
import {
  useTaskStore,
  selectActiveTask,
  selectActiveTaskStatus,
} from "@/stores/taskStore";
import { makeTask } from "@/tests/test-utils/factories";

beforeEach(() => {
  useTaskStore.getState().clearTasks();
});

describe("taskStore — selectors 与动作", () => {
  it("addTask 添加到列表，且首个任务自动成为 active", () => {
    useTaskStore.getState().addTask(makeTask("t1"));
    const st = useTaskStore.getState();
    expect(st.tasks).toHaveLength(1);
    expect(st.activeTaskId).toBe("t1");
  });

  it("addTask 多个任务时 activeTaskId 不被覆盖", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1"));
    s.addTask(makeTask("t2"));
    expect(useTaskStore.getState().activeTaskId).toBe("t1");
  });

  it("updateTask 按 task_id 更新状态", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1", { status: "running" }));
    s.updateTask("t1", { status: "completed" });
    expect(useTaskStore.getState().tasks[0].status).toBe("completed");
  });

  it("setActiveTask 设置活跃任务", () => {
    useTaskStore.getState().setActiveTask("t2");
    expect(useTaskStore.getState().activeTaskId).toBe("t2");
  });

  it("setActiveTask(null) 清空活跃任务", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1"));
    s.setActiveTask(null);
    expect(useTaskStore.getState().activeTaskId).toBeNull();
  });

  it("getTaskById 找到匹配任务", () => {
    useTaskStore.getState().addTask(makeTask("t1"));
    expect(useTaskStore.getState().getTaskById("t1")?.task_id).toBe("t1");
  });

  it("getTaskById 未找到返回 undefined", () => {
    expect(useTaskStore.getState().getTaskById("nope")).toBeUndefined();
  });

  it("selectActiveTask 返回活跃任务记录", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1"));
    expect(selectActiveTask(useTaskStore.getState())?.task_id).toBe("t1");
  });

  it("selectActiveTask 无活跃任务时返回 undefined", () => {
    expect(selectActiveTask(useTaskStore.getState())).toBeUndefined();
  });

  it("selectActiveTaskStatus 返回活跃任务状态", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1", { status: "running" }));
    expect(selectActiveTaskStatus(useTaskStore.getState())).toBe("running");
  });

  it("selectActiveTaskStatus 无活跃任务时返回 null", () => {
    expect(selectActiveTaskStatus(useTaskStore.getState())).toBeNull();
  });

  it("clearTasks 清空列表与活跃任务", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1"));
    s.clearTasks();
    const st = useTaskStore.getState();
    expect(st.tasks).toHaveLength(0);
    expect(st.activeTaskId).toBeNull();
  });

  it("setTasks 保留仍存在的 activeTaskId", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1", { latest_turn_id: "turn-1" }));
    s.setTasks([
      makeTask("t2", { latest_turn_id: "turn-2" }),
      makeTask("t1", { latest_turn_id: "turn-1" }),
    ]);
    expect(useTaskStore.getState().activeTaskId).toBe("t1");
    expect(useTaskStore.getState().activeTurnId).toBe("turn-1");
  });

  it("setTasks 在同一 active task 更新时切到最新 turn", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1", { latest_turn_id: "old-turn" }));
    s.setActiveTurn("old-turn");
    s.setTasks([makeTask("t1", { latest_turn_id: "new-turn" })]);
    expect(useTaskStore.getState().activeTaskId).toBe("t1");
    expect(useTaskStore.getState().activeTurnId).toBe("new-turn");
  });

  it("setTasks 修复已不存在的 activeTaskId", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("removed", { latest_turn_id: "removed-turn" }));
    s.setTasks([makeTask("next", { latest_turn_id: "next-turn" })]);
    expect(useTaskStore.getState().activeTaskId).toBe("next");
    expect(useTaskStore.getState().activeTurnId).toBe("next-turn");
  });

  it("setTasks 传入空列表时清空 activeTaskId 和 activeTurnId", () => {
    const s = useTaskStore.getState();
    s.addTask(makeTask("t1", { latest_turn_id: "turn-1" }));
    s.setTasks([]);
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(useTaskStore.getState().activeTurnId).toBeNull();
  });

  it("空集合时 selectors 不抛错", () => {
    expect(() => selectActiveTask(useTaskStore.getState())).not.toThrow();
    expect(() => selectActiveTaskStatus(useTaskStore.getState())).not.toThrow();
    expect(() => useTaskStore.getState().getTaskById("x")).not.toThrow();
  });
});
