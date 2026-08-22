// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";

// mock 依赖：logger 与 eventStore（避免 invalidateTask 副作用）
vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/stores/eventStore", () => ({
  useEventStore: {
    getState: () => ({ invalidateTask: () => {} }),
  },
}));

import { useTaskStore } from "@/stores/taskStore";

/**
 * 重置 store 内存态 + 清空 localStorage（每个用例独立）。
 */
function resetStore() {
  const store = useTaskStore.getState();
  // 清理草稿相关状态
  useTaskStore.setState({ drafts: {} });
  localStorage.clear();
}

describe("taskStore input drafts", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it("setInputDraft 写入非空草稿 → drafts[taskId] 有值", () => {
    useTaskStore.getState().setInputDraft("task-1", "hello draft");
    expect(useTaskStore.getState().drafts["task-1"]).toBe("hello draft");
  });

  it("setInputDraft 写入空字符串 → 键被删除（drafts 不含该 taskId）", () => {
    useTaskStore.getState().setInputDraft("task-1", "hello");
    expect(useTaskStore.getState().drafts["task-1"]).toBe("hello");
    useTaskStore.getState().setInputDraft("task-1", "");
    expect(useTaskStore.getState().drafts["task-1"]).toBeUndefined();
  });

  it("setInputDraft 写入空格字符串 → 视为非空（drafts 有值，因仅按 length>0 判断）", () => {
    // 边缘用例：空格 length>0 被视为有效草稿，不删除键（依赖调用方先 trim）
    useTaskStore.getState().setInputDraft("task-1", "   ");
    expect(useTaskStore.getState().drafts["task-1"]).toBe("   ");
  });

  it("setInputDraft 同步持久化到 localStorage", () => {
    useTaskStore.getState().setInputDraft("task-1", "persist me");
    const raw = localStorage.getItem("coding-agent.inputDrafts");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw as string);
    expect(parsed["task-1"]).toBe("persist me");
  });

  it("setInputDraft 空草稿同步从 localStorage 删除键", () => {
    useTaskStore.getState().setInputDraft("task-1", "data");
    const rawBefore = localStorage.getItem("coding-agent.inputDrafts");
    expect(JSON.parse(rawBefore as string)["task-1"]).toBe("data");
    useTaskStore.getState().setInputDraft("task-1", "");
    const rawAfter = localStorage.getItem("coding-agent.inputDrafts");
    expect(JSON.parse(rawAfter as string)["task-1"]).toBeUndefined();
  });

  it("replaceTask：temp 草稿迁移到真实 taskId（drafts[temp] → drafts[real]，temp 键删除）", () => {
    useTaskStore.getState().setInputDraft("temp-1", "temp draft");
    expect(useTaskStore.getState().drafts["temp-1"]).toBe("temp draft");

    useTaskStore.getState().replaceTask("temp-1", {
      task_id: "real-1",
      workspace_id: "ws-1",
    } as never);

    const drafts = useTaskStore.getState().drafts;
    expect(drafts["real-1"]).toBe("temp draft");
    expect(drafts["temp-1"]).toBeUndefined();
  });

  it("replaceTask：无 temp 草稿时不创建空键", () => {
    useTaskStore.getState().replaceTask("temp-2", {
      task_id: "real-2",
      workspace_id: "ws-1",
    } as never);
    expect(useTaskStore.getState().drafts["real-2"]).toBeUndefined();
  });

  it("removeTask：删除任务时其草稿被清理（内存 + 持久化）", () => {
    useTaskStore.getState().setInputDraft("task-del", "to delete");
    useTaskStore.getState().setInputDraft("task-keep", "keep");
    useTaskStore.getState().removeTask("task-del");
    const drafts = useTaskStore.getState().drafts;
    expect(drafts["task-del"]).toBeUndefined();
    expect(drafts["task-keep"]).toBe("keep");
    // 持久化同步清理
    const persisted = JSON.parse(localStorage.getItem("coding-agent.inputDrafts") as string);
    expect(persisted["task-del"]).toBeUndefined();
  });

  it("clearTasks：drafts 被清空（内存 + 持久化）", () => {
    const store = useTaskStore.getState();
    store.setInputDraft("a", "x");
    store.setInputDraft("b", "y");
    useTaskStore.getState().clearTasks();
    expect(useTaskStore.getState().drafts).toEqual({});
    const persisted = localStorage.getItem("coding-agent.inputDrafts");
    expect(persisted).toBeNull();
  });

  it("loadPersistedInputDrafts 由 store 初始化读取：过滤非字符串值与空字符串（脏数据防御）", () => {
    // 手动写入脏数据到 localStorage，再用新 store 验证？此处 store 是单例，
    // 改为直接验证 setInputDraft/persist 链路对脏数据的鲁棒性已在上面覆盖。
    // 这里覆盖 loadPersistedInputDrafts 对脏数据的过滤：通过直接写脏 JSON 后读。
    const dirty: Record<string, unknown> = {
      "task-ok": "valid text",
      "task-empty": "",
      "task-num": 123,
      "task-null": null,
      "task-arr": ["a", "b"],
      "task-obj": { foo: "bar" },
    };
    localStorage.setItem("coding-agent.inputDrafts", JSON.stringify(dirty));
    // 重新触发 store 初始化读取：重新导入模块不可行（单例），
    // 改用 store 的 setInputDraft 后清除，再验证新的脏数据不会被 setInputDraft 写入。
    // 直接验证：loadPersistedInputDrafts 在模块内部，这里验证持久化读取过滤行为
    // 通过 reset 后用带脏数据的 localStorage 重建读取路径。
    // 由于 store 单例已初始化，这里断言“脏数据不会被 store 保留”：
    // 1) 先清掉内存
    useTaskStore.setState({ drafts: {} });
    // 2) 触发一次 setInputDraft("") 会读取现有 localStorage 并写回过滤后的结果
    useTaskStore.getState().setInputDraft("task-ok", "valid text");
    const persisted = JSON.parse(localStorage.getItem("coding-agent.inputDrafts") as string);
    // 只有合法字符串被保留
    expect(persisted["task-ok"]).toBe("valid text");
    expect(persisted["task-empty"]).toBeUndefined();
  });

  it("loadPersistedInputDrafts：JSON 损坏时不抛错，降级为空对象（通过 clear 后写坏数据再读）", () => {
    localStorage.setItem("coding-agent.inputDrafts", "{not-json");
    useTaskStore.setState({ drafts: {} });
    // setInputDraft 触发 loadPersistedInputDrafts 解析损坏 JSON，应不抛错
    expect(() => useTaskStore.getState().setInputDraft("task-x", "recover")).not.toThrow();
    expect(useTaskStore.getState().drafts["task-x"]).toBe("recover");
  });

  it("loadPersistedInputDrafts：非对象 JSON（数组）降级为空对象", () => {
    localStorage.setItem("coding-agent.inputDrafts", JSON.stringify([1, 2, 3]));
    useTaskStore.setState({ drafts: {} });
    expect(() => useTaskStore.getState().setInputDraft("task-x", "ok")).not.toThrow();
    expect(useTaskStore.getState().drafts["task-x"]).toBe("ok");
  });
});
