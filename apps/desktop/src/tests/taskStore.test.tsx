// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useTaskStore } from "@/stores/taskStore";

/**
 * taskStore.activeTaskId 本地持久化写行为测试。
 *
 * 覆盖：设置真实任务 ID 写入 localStorage；临时任务 / 清空时移除持久化值；
 * "temp-" 前缀的持久化值在新会话启动时被忽略（store 初始读逻辑）。
 * 注意 store 为模块级单例，初始读发生在 import 阶段，故「启动恢复」通过
 * 直接断言初始 activeTaskId 验证，不依赖运行期 setState。
 */
describe("taskStore activeTaskId 持久化", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("setActiveTask 真实任务 ID 写入 localStorage", () => {
    useTaskStore.getState().setActiveTask("task-real-1", "turn-1");

    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-real-1");
  });

  it("setActiveTask 临时任务 ID 清除既有持久化值", () => {
    useTaskStore.getState().setActiveTask("task-real-1", "turn-1");
    useTaskStore.getState().setActiveTask("temp-123", null);

    expect(localStorage.getItem("coding-agent.activeTaskId")).toBeNull();
  });

  it("删除活跃任务时清除持久化值", () => {
    useTaskStore.getState().setActiveTask("task-real-1", "turn-1");
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-real-1");

    useTaskStore.getState().removeTask("task-real-1");

    expect(localStorage.getItem("coding-agent.activeTaskId")).toBeNull();
    expect(useTaskStore.getState().activeTaskId).toBeNull();
  });

  it("setTasks 保留持久化的活跃任务（即使在列表非首项）", () => {
    useTaskStore.getState().setActiveTask("task-real-2", "turn-2");
    // 列表里 task-real-2 不是首项，但仍是持久化活跃任务。
    useTaskStore.getState().setTasks([
      makeTask("task-real-1", { latest_turn_id: "turn-1" }),
      makeTask("task-real-2", { latest_turn_id: "turn-2" }),
      makeTask("task-real-3", { latest_turn_id: "turn-3" }),
    ]);

    expect(useTaskStore.getState().activeTaskId).toBe("task-real-2");
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-real-2");
  });

  it("setTasks 在持久化任务已被删除时回退首项并清除脏持久化值", () => {
    useTaskStore.getState().setActiveTask("task-deleted", "turn-x");
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-deleted");

    useTaskStore.getState().setTasks([
      makeTask("task-real-1", { latest_turn_id: "turn-1" }),
      makeTask("task-real-2", { latest_turn_id: "turn-2" }),
    ]);

    // 内存态回退到列表首项。
    expect(useTaskStore.getState().activeTaskId).toBe("task-real-1");
    // 持久化脏值被清除，下次启动不会再次命中已删除任务。
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-real-1");
  });

  it("replaceTask 将临时任务转正时同步持久化真实任务 ID（新建任务重启可恢复）", () => {
    // 首次创建任务时先以临时任务占位（不持久化）。
    useTaskStore.getState().setActiveTask("temp-1", null);
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBeNull();

    // 临时任务被真实任务替换且成为活跃任务，持久化必须切换到真实 ID。
    useTaskStore.getState().replaceTask("temp-1", makeTask("task-real-1", { latest_turn_id: "turn-1" }));

    expect(useTaskStore.getState().activeTaskId).toBe("task-real-1");
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-real-1");
  });

  it("clearTasks 清除活跃任务并同步清除持久化值", () => {
    useTaskStore.getState().setActiveTask("task-real-1", "turn-1");
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBe("task-real-1");

    useTaskStore.getState().clearTasks();

    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(localStorage.getItem("coding-agent.activeTaskId")).toBeNull();
  });
});

/**
 * 轻量任务工厂（仅覆盖本文件所需字段），避免引入业务工厂的额外耦合。
 *
 * @param taskId - 任务标识。
 * @param overrides - 覆盖字段。
 * @returns TaskRecord 实例。
 */
function makeTask(
  taskId: string,
  overrides: Partial<{ latest_turn_id: string | null }> = {},
): import("@shared/task").TaskRecord {
  const now = new Date().toISOString();
  return {
    task_id: taskId,
    workspace_id: "ws-1",
    agent_id: "developer",
    input_text: "task",
    title: "task",
    last_message_preview: "",
    latest_turn_id: overrides.latest_turn_id ?? null,
    status: "pending",
    execution_status: "pending",
    created_at: now,
    updated_at: now,
  };
}
