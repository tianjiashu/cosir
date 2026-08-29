// @vitest-environment happy-dom
/**
 * 独立验证重点边界：clearTasks 必须复位 selectedModelName / selectedReasoningEffort
 * 的运行时态并同步持久化清除（避免选择态跨会话残留）。
 *
 * 该边界在既有候选测试文件中未被直接断言（taskStore.inputDraft.test.ts 仅验证了 drafts），
 * 故在此独立补充，作为独立测试 Agent 的覆盖补漏与缺陷暴露手段（不修改任何生产代码）。
 *
 * @module tests/taskStore.clearTasks.modelReset
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/services/api", () => ({
  listModels: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

import { useTaskStore } from "@/stores/taskStore";

describe("taskStore clearTasks 复位模型/档位选择态（独立边界验证）", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    useTaskStore.setState({
      tasksById: {},
      tasksByWorkspaceId: {},
      loadedWorkspaceIds: new Set(),
      activeTaskId: null,
      activeTurnId: null,
      selectedModelName: null,
      selectedReasoningEffort: null,
      availableModels: [],
      modelsLoaded: false,
      drafts: {},
    });
  });

  // 边界：清空任务前已选模型 + 档位，clearTasks 后二者内存态与持久化均复位为 null。
  it("clearTasks 后 selectedModelName / selectedReasoningEffort 内存态与 localStorage 均清除", () => {
    // 预置已选模型与档位并落盘。
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    useTaskStore.getState().setSelectedReasoningEffort("high");
    expect(localStorage.getItem("coding-agent.selectedModelName")).toBe(
      "deepseek/deepseek-v4-flash",
    );
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBe("high");

    useTaskStore.getState().clearTasks();

    // 内存态复位。
    expect(useTaskStore.getState().selectedModelName).toBeNull();
    expect(useTaskStore.getState().selectedReasoningEffort).toBeNull();
    // 持久化清除：键应被移除（而非残留空串或旧值）。
    expect(localStorage.getItem("coding-agent.selectedModelName")).toBeNull();
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBeNull();
  });

  // 边界：clearTasks 不应误触 availableModels（清空任务 ≠ 清空模型缓存语义，
  // 仅复位选择态；模型缓存由 refreshAvailableModels 负责，避免下拉空白）。
  it("clearTasks 仅复位选择态，不破坏 availableModels 缓存", () => {
    useTaskStore.setState({
      availableModels: [
        {
          model_id: "m1",
          provider_id: "p1",
          provider_name: "DeepSeek 官方",
          model_name: "deepseek/deepseek-v4-flash",
          display_name: "deepseek-v4-flash",
          max_context_window: 128000,
          supports_thinking: true,
          reasoning_effort: { supported: true, effort_map: { low: "low" } },
          temperature: null,
          top_p: null,
          max_tokens: null,
          enabled: true,
          api_key_configured: true,
          sort_order: 0,
          created_at: "2026-08-17T00:00:00Z",
          updated_at: "2026-08-17T00:00:00Z",
        },
      ],
      modelsLoaded: true,
    });
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    useTaskStore.getState().setSelectedReasoningEffort("low");

    useTaskStore.getState().clearTasks();

    expect(useTaskStore.getState().selectedModelName).toBeNull();
    expect(useTaskStore.getState().selectedReasoningEffort).toBeNull();
    // availableModels 与 modelsLoaded 不应被 clearTasks 清空（选择态复位 != 模型缓存清空）。
    expect(useTaskStore.getState().availableModels).toHaveLength(1);
    expect(useTaskStore.getState().modelsLoaded).toBe(true);
  });
});
