// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";

// taskStore 内部使用 localStorage（持久化活跃任务）与 logger，happy-dom 提供
// localStorage 实现；logger 为纯函数副作用，无需 mock，但需屏蔽其日志噪声。
// 用 importOriginal 保留其余导出，避免后续 taskStore 新增 logger 调用时隐性炸。
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
}));

import { useTaskStore, selectActiveTask, selectActiveTaskStatus } from "@/stores/taskStore";
import { useEventStore } from "@/stores/eventStore";
import type { TaskRecord } from "@shared/task";

function makeTask(taskId: string, workspaceId = "ws"): TaskRecord {
  return {
    task_id: taskId,
    workspace_id: workspaceId,
    agent_id: "dev",
    input_text: "hi",
    title: "hi",
    last_message_preview: "hi",
    latest_turn_id: "turn-1",
    status: "completed",
    execution_status: "completed",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TaskRecord;
}

describe("taskStore.setWorkspaceTasks + 分组缓存", () => {
  beforeEach(() => {
    localStorage.clear();
    // 复用 store 自身重置入口，确保 loadedWorkspaceIds 等全部状态字段一并回到初始态，
    // 避免测试间加载态（已加载/失败标记）串味导致分组缓存判定失真。
    useTaskStore.getState().clearTasks();
  });

  it("按 workspace 写入分组且不污染其它 workspace", () => {
    const a = [makeTask("task-a", "ws-1")];
    const b = [makeTask("task-b", "ws-2")];
    useTaskStore.getState().setWorkspaceTasks("ws-1", a);
    useTaskStore.getState().setWorkspaceTasks("ws-2", b);

    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toEqual(a);
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-2"]).toEqual(b);
    expect(useTaskStore.getState().tasksById["task-a"]).toEqual(a[0]);
    expect(useTaskStore.getState().tasksById["task-b"]).toEqual(b[0]);
  });

  it("setWorkspaceTasks 是纯写入：无活跃任务时也不自动选中（首屏恢复由 App 层单点负责）", () => {
    const tasks = [makeTask("task-a", "ws-1"), makeTask("task-b", "ws-1")];
    useTaskStore.getState().setWorkspaceTasks("ws-1", tasks);

    // 需求不变量：展开/加载 workspace 的任务列表绝不意外切走中央对话，
    // 故 setWorkspaceTasks 不内嵌任何活跃任务恢复/回退编排。
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toEqual(tasks);
  });

  it("已有持久化任务落在加载分组中则恢复，不回退首项", () => {
    localStorage.setItem("coding-agent.activeTaskId", "task-b");
    useTaskStore.setState({ activeTaskId: "task-b", activeTurnId: null });
    const tasks = [makeTask("task-a", "ws-1"), makeTask("task-b", "ws-1")];
    useTaskStore.getState().setWorkspaceTasks("ws-1", tasks);

    expect(useTaskStore.getState().activeTaskId).toBe("task-b");
  });

  it("已有活跃任务（其它 workspace）时不覆盖（点 workspace 不切视图）", () => {
    // 模拟中央正展示 ws-2 的任务 task-b，此时展开 ws-1 不应切走。
    useTaskStore.getState().setWorkspaceTasks("ws-2", [makeTask("task-b", "ws-2")]);
    useTaskStore.setState({ activeTaskId: "task-b", activeTurnId: "turn-b" });
    const tasks = [makeTask("task-a", "ws-1")];
    useTaskStore.getState().setWorkspaceTasks("ws-1", tasks);

    expect(useTaskStore.getState().activeTaskId).toBe("task-b");
    expect(useTaskStore.getState().activeTurnId).toBe("turn-b");
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toEqual(tasks);
  });

  it("刷新 workspace 列表会清理同 workspace 已消失的活跃任务实体", () => {
    const invalidateSpy = vi.spyOn(useEventStore.getState(), "invalidateTask");
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().setActiveTask("task-a");

    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-b", "ws-1")]);

    expect(useTaskStore.getState().getTaskById("task-a")).toBeUndefined();
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(useTaskStore.getState().activeTurnId).toBeNull();
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"].map((task) => task.task_id)).toEqual(["task-b"]);
    expect(invalidateSpy).toHaveBeenCalledWith("task-a");
    invalidateSpy.mockRestore();
  });
});

describe("taskStore 跨分组查找", () => {
  beforeEach(() => {
    localStorage.clear();
    // 复用 store 自身重置入口，确保 loadedWorkspaceIds 等全部状态字段一并回到初始态，
    // 避免测试间加载态（已加载/失败标记）串味导致分组缓存判定失真。
    useTaskStore.getState().clearTasks();
  });

  it("getTaskById 从实体缓存命中", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().setWorkspaceTasks("ws-2", [makeTask("task-b", "ws-2")]);

    expect(useTaskStore.getState().getTaskById("task-b")?.workspace_id).toBe("ws-2");
    expect(useTaskStore.getState().getTaskById("missing")).toBeUndefined();
  });

  it("addTask 落到已加载 workspace 分组并更新活跃任务（无活跃时）", () => {
    // 先标记 ws-3 已加载（空组），再 addTask 才会写入分组。
    useTaskStore.getState().setWorkspaceTasks("ws-3", []);
    useTaskStore.getState().addTask(makeTask("task-new", "ws-3"));
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-3"]?.[0].task_id).toBe("task-new");
    expect(useTaskStore.getState().activeTaskId).toBe("task-new");
  });

  it("addTask 对未加载 workspace 不注入分组（守住 undefined 不被伪造）", () => {
    // 未加载分组不接收注入，否则会把「未加载」伪造成「已加载且只有 1 条」，
    // 导致后续 ensureLoaded 跳过真实拉取。活跃任务仍应更新。
    useTaskStore.getState().addTask(makeTask("task-new", "ws-unloaded"));
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-unloaded"]).toBeUndefined();
    expect(useTaskStore.getState().getTaskById("task-new")?.workspace_id).toBe("ws-unloaded");
    expect(useTaskStore.getState().isWorkspaceLoaded("ws-unloaded")).toBe(false);
    expect(useTaskStore.getState().activeTaskId).toBe("task-new");
    expect(selectActiveTask(useTaskStore.getState())?.task_id).toBe("task-new");
  });

  it("clearWorkspaceTasks 级联清空属该 workspace 的活跃任务", () => {
    // 活跃任务归属被删 workspace 时，必须清空悬空活跃态并失效其事件缓存，
    // 且从 loadedWorkspaceIds 移除该 workspace（级联判定基于 activeTask 的 workspace，
    // 不依赖分组是否加载）。
    const invalidateSpy = vi.spyOn(useEventStore.getState(), "invalidateTask");
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().setActiveTask("task-a");
    useTaskStore.getState().clearWorkspaceTasks("ws-1");
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toBeUndefined();
    expect(useTaskStore.getState().isWorkspaceLoaded("ws-1")).toBe(false);
    expect(invalidateSpy).toHaveBeenCalledWith("task-a");
    invalidateSpy.mockRestore();
  });

  it("clearWorkspaceTasks 会清理由未加载 workspace 新建出的实体任务", () => {
    const invalidateSpy = vi.spyOn(useEventStore.getState(), "invalidateTask");
    useTaskStore.getState().addTask(makeTask("task-a", "ws-unloaded"));
    useTaskStore.getState().setActiveTask("task-a");
    useTaskStore.getState().clearWorkspaceTasks("ws-unloaded");
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(useTaskStore.getState().getTaskById("task-a")).toBeUndefined();
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-unloaded"]).toBeUndefined();
    expect(invalidateSpy).toHaveBeenCalledWith("task-a");
    invalidateSpy.mockRestore();
  });

  it("clearWorkspaceTasks 不误清归属其它 workspace 的活跃任务", () => {
    // 反向用例：被删 workspace 不含当前活跃任务时，活跃态须保留。
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().setWorkspaceTasks("ws-2", [makeTask("task-b", "ws-2")]);
    useTaskStore.getState().setActiveTask("task-b");
    useTaskStore.getState().clearWorkspaceTasks("ws-1");
    expect(useTaskStore.getState().activeTaskId).toBe("task-b");
  });

  it("updateTask 跨分组更新对应任务", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().updateTask("task-a", { title: "changed" });
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"][0].title).toBe("changed");
  });

  it("removeTask 跨分组清理并失效事件缓存", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().removeTask("task-a");
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toEqual([]);
  });

  it("clearWorkspaceTasks 级联清理指定 workspace 分组", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().clearWorkspaceTasks("ws-1");
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toBeUndefined();
  });

  it("removeTask 跨分组清理并失效事件缓存", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    const invalidateSpy = vi.spyOn(useEventStore.getState(), "invalidateTask");
    useTaskStore.getState().removeTask("task-a");
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-1"]).toEqual([]);
    expect(invalidateSpy).toHaveBeenCalledWith("task-a");
  });

  it("selectActiveTask 跨分组返回活跃任务", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-1", [makeTask("task-a", "ws-1")]);
    useTaskStore.getState().setWorkspaceTasks("ws-2", [makeTask("task-b", "ws-2")]);
    useTaskStore.setState({ activeTaskId: "task-b", activeTurnId: null });
    expect(selectActiveTask(useTaskStore.getState())?.task_id).toBe("task-b");
  });

  it("selectActiveTaskStatus 跨分组返回活跃任务状态", () => {
    useTaskStore.getState().setWorkspaceTasks("ws-2", [makeTask("task-b", "ws-2")]);
    useTaskStore.setState({ activeTaskId: "task-b", activeTurnId: null });
    expect(selectActiveTaskStatus(useTaskStore.getState())).toBe("completed");
  });
});
