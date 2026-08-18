// @vitest-environment happy-dom
/**
 * taskStore 模型选择状态测试（selectedModelName 持久化 + availableModels 缓存）。
 *
 * 守护不变量：
 * 1. setSelectedModelName 双向持久化：显式选择写入 localStorage，null（Auto）清除；
 * 2. 启动时从 localStorage 恢复最近选择（模块重载路径）；
 * 3. refreshAvailableModels 成功写入缓存并置 modelsLoaded，失败保留旧缓存并复位标记。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/services/api", () => ({
  listModels: vi.fn(),
}));

vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

import { listModels } from "@/services/api";
import { useTaskStore } from "@/stores/taskStore";
import type { ModelEntryRecord } from "@shared/model";

const mockListModels = vi.mocked(listModels);

function makeModel(modelName: string): ModelEntryRecord {
  return {
    model_id: `id-${modelName}`,
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: modelName,
    display_name: modelName.split("/").pop() ?? modelName,
    max_context_window: 128000,
    supports_thinking: false,
    temperature: null,
    top_p: null,
    max_tokens: null,
    enabled: true,
    api_key_configured: true,
    sort_order: 0,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
  };
}

describe("taskStore selectedModelName 持久化", () => {
  beforeEach(() => {
    localStorage.clear();
    mockListModels.mockReset();
    useTaskStore.setState({
      selectedModelName: null,
      availableModels: [],
      modelsLoaded: false,
    });
  });

  it("显式选择模型时写入 localStorage", () => {
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
    expect(localStorage.getItem("coding-agent.selectedModelName")).toBe(
      "deepseek/deepseek-v4-flash",
    );
  });

  it("切回 Auto（null）时清除持久化值", () => {
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    useTaskStore.getState().setSelectedModelName(null);
    expect(useTaskStore.getState().selectedModelName).toBeNull();
    expect(localStorage.getItem("coding-agent.selectedModelName")).toBeNull();
  });

  it("localStorage 不可用时降级不抛错（仅内存态生效）", () => {
    const brokenStorage = {
      getItem: () => {
        throw new Error("storage unavailable");
      },
      setItem: () => {
        throw new Error("storage unavailable");
      },
      removeItem: () => {
        throw new Error("storage unavailable");
      },
    };
    const original = window.localStorage;
    vi.stubGlobal("localStorage", brokenStorage);
    try {
      useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
      // 持久化失败不影响内存态与调用方。
      expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
    } finally {
      vi.unstubAllGlobals();
      // 恢复后重新验证（避免 stub 泄漏影响后续用例）。
      expect(original).toBeTruthy();
    }
  });

  it("启动时从 localStorage 恢复最近选择（模块重载初始化路径）", async () => {
    localStorage.setItem("coding-agent.selectedModelName", "deepseek/deepseek-v4-flash");
    // 模块重载：让 store 重新执行 loadPersistedSelectedModelName 初始化。
    vi.resetModules();
    const { useTaskStore: freshStore } = await import("@/stores/taskStore");
    expect(freshStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
  });
});

describe("taskStore refreshAvailableModels 缓存契约", () => {
  beforeEach(() => {
    localStorage.clear();
    mockListModels.mockReset();
    useTaskStore.setState({ availableModels: [], modelsLoaded: false });
  });

  it("成功：写入缓存并置 modelsLoaded=true", async () => {
    const models = [makeModel("deepseek/deepseek-v4-flash")];
    mockListModels.mockResolvedValue(models);
    const ok = await useTaskStore.getState().refreshAvailableModels();
    expect(ok).toBe(true);
    expect(useTaskStore.getState().availableModels).toEqual(models);
    expect(useTaskStore.getState().modelsLoaded).toBe(true);
  });

  it("空列表也是有效加载态（驱动空态引导而非报错）", async () => {
    mockListModels.mockResolvedValue([]);
    await useTaskStore.getState().refreshAvailableModels();
    expect(useTaskStore.getState().availableModels).toEqual([]);
    expect(useTaskStore.getState().modelsLoaded).toBe(true);
  });

  it("失败：保留旧缓存并复位 modelsLoaded=false（允许重试，校验按空缓存拦截）", async () => {
    const stale = [makeModel("deepseek/stale")];
    useTaskStore.setState({ availableModels: stale, modelsLoaded: true });
    mockListModels.mockRejectedValue(new Error("backend down"));
    const ok = await useTaskStore.getState().refreshAvailableModels();
    expect(ok).toBe(false);
    expect(useTaskStore.getState().availableModels).toEqual(stale);
    expect(useTaskStore.getState().modelsLoaded).toBe(false);
  });
});
