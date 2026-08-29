/**
 * 修复验证测试：缺陷 1（P0）- 创建轮次缺少 provider_id 导致后端 400
 *
 * 覆盖点：
 *  1. useTask.createTurn 请求体同时含 provider_id 与 model_name，且来自同一选中模型条目
 *  2. 跨厂商重名场景（provider_id 不同、model_name 相同或去前缀后相同），选中其一，
 *     请求体 provider_id 必须是选中那一个
 *  3. validateModelSend 二元组语义：仅 model_name 相同但 provider_id 不同时应判为不匹配；
 *     完全匹配时放行
 *  4. taskStore.setSelectedModel / loadPersistedSelectedModel 持久化往返正确
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

// useTask / useModelSendGuard 内部使用了 react 的 Hooks。非组件环境下把 react 整体
// 降级为最小实现（含 zustand 所需的 useSyncExternalStore），以便直接调用被测 hook，
// 避免引入真实 react 运行时的 Hook 调度依赖。
vi.mock("react", () => {
  // subscribe 在本桩中不使用（无真实订阅），仅需返回 getSnapshot 的当前值。
  const useSyncExternalStore = (_subscribe: any, getSnapshot: any) => getSnapshot();
  const useCallback = (fn: any) => fn;
  const useRef = (init: any) => ({ current: init });
  const useState = (init: any) => [typeof init === "function" ? init() : init, vi.fn()];
  const useDebugValue = () => {};
  const named = { useSyncExternalStore, useCallback, useRef, useState, useDebugValue };
  // zustand 使用 `import * as React`，需要 default 与命名空间一致，否则报 "No default export"。
  return { ...named, default: named };
});

// crypto.randomUUID 在 node 环境可能缺失，提供稳定实现
if (typeof (globalThis as any).crypto?.randomUUID !== "function") {
  let counter = 0;
  vi.stubGlobal("crypto", {
    randomUUID: () => `uuid-${(counter++).toString(36)}-${Date.now().toString(36)}`,
  });
}

// ---------------------------------------------------------------------------
// localStorage mock（node 环境默认无 localStorage）
// ---------------------------------------------------------------------------
class MemoryStorage {
  private map = new Map<string, string>();
  getItem(key: string): string | null {
    return this.map.has(key) ? (this.map.get(key) as string) : null;
  }
  setItem(key: string, value: string): void {
    this.map.set(key, String(value));
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  clear(): void {
    this.map.clear();
  }
  get length(): number {
    return this.map.size;
  }
  key(index: number): string | null {
    return Array.from(this.map.keys())[index] ?? null;
  }
}
vi.stubGlobal("localStorage", new MemoryStorage());

// ---------------------------------------------------------------------------
// Mock 依赖模块（useTask 的深层依赖）
// 用 vi.hoisted 声明 mock 函数，避免 vi.mock 工厂被 hoist 后引用未初始化变量。
// ---------------------------------------------------------------------------
const h = vi.hoisted(() => ({
  mockCreateTaskTurn: vi.fn(),
  mockCreateTask: vi.fn(),
  mockReplaceTaskId: vi.fn(),
  mockRemoveTurnId: vi.fn(),
  mockUpsertTurn: vi.fn(),
  mockSetTurnsForTask: vi.fn(),
  mockSetStreamingTurn: vi.fn(),
  mockConnect: vi.fn(async () => {}),
  mockDisconnectTurn: vi.fn(),
  mockSetEvents: vi.fn(),
  mockInvalidateTask: vi.fn(),
}));

vi.mock("@/services/api", () => ({
  createTaskTurn: (...args: any[]) => h.mockCreateTaskTurn(...args),
  createTask: (...args: any[]) => h.mockCreateTask(...args),
  getTask: vi.fn(async () => ({})),
  listTaskTurns: vi.fn(async () => []),
  listTaskEvents: vi.fn(async () => []),
  cancelTurn: vi.fn(async () => ({})),
  listModels: vi.fn(async () => []),
}));

vi.mock("@/stores/turnStore", () => {
  const store = {
    setTurnsForTask: h.mockSetTurnsForTask,
    upsertTurn: h.mockUpsertTurn,
    replaceTurnId: h.mockReplaceTaskId,
    removeTurnId: h.mockRemoveTurnId,
    setStreamingTurn: h.mockSetStreamingTurn,
  };
  const useTurnStore = (selector?: any) => (selector ? selector(store) : store);
  (useTurnStore as any).getState = () => store;
  return { useTurnStore };
});

vi.mock("@/stores/eventStore", () => {
  const store = { setEvents: h.mockSetEvents, invalidateTask: h.mockInvalidateTask };
  const useEventStore = (selector?: any) => (selector ? selector(store) : store);
  (useEventStore as any).getState = () => store;
  return { useEventStore };
});

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({ connect: h.mockConnect, disconnectTurn: h.mockDisconnectTurn }),
}));

vi.mock("@/services/tracePropagation", () => ({
  beginClientTrace: vi.fn(),
  endClientTrace: vi.fn(),
  hasClientTrace: vi.fn(() => false),
  readBackendTraceHeaders: vi.fn(() => null),
  recordBackendTrace: vi.fn(),
  buildTraceHeaders: vi.fn(() => ({ trace: { traceId: "t" }, headers: {} })),
}));

vi.mock("@/lib/perf", () => ({
  PerfTrace: { markCurrent: vi.fn(), startCurrent: () => ({ mark: vi.fn(), traceId: "t" }) },
}));

vi.mock("@/lib/logger", () => ({
  logError: vi.fn(),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
}));

vi.mock("./useWorkspaceTaskLazyLoad", () => ({
  loadWorkspaceTasks: vi.fn(async () => {}),
}));

// 必须在 mock 之后 import 被测模块
import { useTask } from "@/hooks/useTask";
import { useTaskStore, loadPersistedSelectedModel, type SelectedModel } from "@/stores/taskStore";
import { validateModelSend } from "@/hooks/useModelSendGuard";
import type { ModelEntryRecord } from "@shared/model";

// 别名导出 hoisted mock，供测试逻辑引用
// 仅别名导出本文件实际断言所依赖的 mock；未使用的不别名，避免 TS6133 未读变量告警。
const { mockCreateTaskTurn, mockCreateTask, mockReplaceTaskId, mockRemoveTurnId, mockUpsertTurn } =
  h;

// 构造一个最小 ModelEntryRecord 工厂
function makeModel(provider_id: number, model_name: string, api_key_configured = true): ModelEntryRecord {
  return {
    provider_id,
    model_name,
    provider_name: `p${provider_id}`,
    provider_type: "openai-compatible",
    api_key_configured,
    is_enabled: true,
    reasoning_effort: { supported: false, effort_map: {} },
  } as unknown as ModelEntryRecord;
}

const OK_TURN = {
  turn_id: 12345,
  task_id: 1,
  input_text: "hello",
  status: "pending",
  end_reason: null,
  response_text: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

beforeEach(() => {
  localStorage.clear();
});

describe("缺陷1: createTurn 请求体同时含 provider_id 与 model_name（值正确配对）", () => {
  beforeEach(() => {
    mockCreateTaskTurn.mockReset();
    mockCreateTaskTurn.mockResolvedValue(OK_TURN);
    mockCreateTask.mockReset();
    mockReplaceTaskId.mockReset();
    mockRemoveTurnId.mockReset();
    mockUpsertTurn.mockReset();
    useTaskStore.setState({ activeTaskId: 1, selectedModel: null, selectedReasoningEffort: null });
  });

  it("选中模型后 createTurn 请求体同时含 provider_id 与 model_name，且来自同一选中条目", async () => {
    const selected: SelectedModel = { provider_id: 7, model_name: "gpt-4o" };
    useTaskStore.setState({ activeTaskId: 1, selectedModel: selected });

    const { createTurn } = useTask();
    let ok = false;
    ok = await createTurn("帮我写代码");
    expect(ok).toBe(true);
    expect(mockCreateTaskTurn).toHaveBeenCalledTimes(1);

    const [, body] = mockCreateTaskTurn.mock.calls[0] as [string, any];
    expect(body.provider_id).toBe(7);
    expect(body.model_name).toBe("gpt-4o");
    expect(body.input_text).toBe("帮我写代码");
  });

  it("未选模型（selectedModel=null）时 provider_id 与 model_name 均为 undefined（不抛出，交由后端拦截）", async () => {
    useTaskStore.setState({ activeTaskId: 1, selectedModel: null });
    const { createTurn } = useTask();
    await createTurn("无模型");
    const [, body] = mockCreateTaskTurn.mock.calls[0] as [string, any];
    expect(body.provider_id).toBeUndefined();
    expect(body.model_name).toBeUndefined();
  });
});

describe("缺陷1: 跨厂商重名场景，选中其一，provider_id 必须是选中那一个", () => {
  beforeEach(() => {
    mockCreateTaskTurn.mockReset();
    mockCreateTaskTurn.mockResolvedValue(OK_TURN);
  });

  it("model_name 完全相同但 provider_id 不同：选中 azure 时请求体 provider_id 必须是 azure 的", async () => {
    const selected: SelectedModel = { provider_id: 2, model_name: "gpt-4o" };
    useTaskStore.setState({ activeTaskId: 1, selectedModel: selected });

    const { createTurn } = useTask();
    await createTurn("跨厂商重名");
    const [, body] = mockCreateTaskTurn.mock.calls[0] as [string, any];
    expect(body.model_name).toBe("gpt-4o");
    expect(body.provider_id).toBe(2); // 必须是选中的 azure(2)，而非 openai(1)
  });

  it("model_name 去前缀后相同（azure/gpt-4o-mini 与 openai/gpt-4o-mini）重名时配对正确", async () => {
    const selected: SelectedModel = { provider_id: 9, model_name: "gpt-4o-mini" };
    useTaskStore.setState({ activeTaskId: 1, selectedModel: selected });

    const { createTurn } = useTask();
    await createTurn("去前缀重名");
    const [, body] = mockCreateTaskTurn.mock.calls[0] as [string, any];
    expect(body.provider_id).toBe(9);
    expect(body.model_name).toBe("gpt-4o-mini");
  });

  it("选中 provider_id 较小一方时请求体不会误用另一方的 provider_id", async () => {
    const selected: SelectedModel = { provider_id: 3, model_name: "claude-3" };
    useTaskStore.setState({ activeTaskId: 1, selectedModel: selected });

    const { createTurn } = useTask();
    await createTurn("另一方为 provider_id=5 的 claude-3");
    const [, body] = mockCreateTaskTurn.mock.calls[0] as [string, any];
    expect(body.provider_id).toBe(3);
  });
});

describe("缺陷1: validateModelSend 二元组语义", () => {
  const openai = makeModel(1, "gpt-4o");
  const azure = makeModel(2, "gpt-4o"); // 跨厂商重名

  it("完全匹配（provider_id + model_name 同时命中）时放行", () => {
    const res = validateModelSend({
      availableModels: [openai, azure],
      modelsLoaded: true,
      selectedModel: { provider_id: 1, model_name: "gpt-4o" },
    });
    expect(res.ok).toBe(true);
  });

  it("仅 model_name 相同但 provider_id 不同应判为不匹配（model_missing）", () => {
    // 可用列表里只有 openai(1) 的 gpt-4o；选中 azure(2) 的 gpt-4o（不在可用列表），
    // 二元组不完全匹配 → 判为不可匹配（model_missing），而非误判为可用。
    const res = validateModelSend({
      availableModels: [openai],
      modelsLoaded: true,
      selectedModel: { provider_id: 2, model_name: "gpt-4o" },
    });
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.block.reason).toBe("model_missing");
    }
  });

  it("缓存为空拦截 no_models", () => {
    const res = validateModelSend({
      availableModels: [],
      modelsLoaded: false,
      selectedModel: { provider_id: 1, model_name: "gpt-4o" },
    });
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.block.reason).toBe("no_models");
  });

  it("未选模型拦截 no_model_selected", () => {
    const res = validateModelSend({
      availableModels: [openai],
      modelsLoaded: true,
      selectedModel: null,
    });
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.block.reason).toBe("no_model_selected");
  });

  it("选中模型厂商 api_key 未配置拦截 api_key_missing", () => {
    const noKey = makeModel(3, "gpt-4o", false);
    const res = validateModelSend({
      availableModels: [noKey],
      modelsLoaded: true,
      selectedModel: { provider_id: 3, model_name: "gpt-4o" },
    });
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.block.reason).toBe("api_key_missing");
  });
});

describe("缺陷1: setSelectedModel / loadPersistedSelectedModel 持久化往返", () => {
  beforeEach(() => {
    localStorage.clear();
    useTaskStore.setState({ selectedModel: null, selectedReasoningEffort: null });
  });

  it("setSelectedModel 写入后，loadPersistedSelectedModel 重读得到相同二元组", () => {
    const entry = makeModel(11, "gemini-pro");
    useTaskStore.getState().setSelectedModel(entry);
    const restored = loadPersistedSelectedModel();
    expect(restored).not.toBeNull();
    expect(restored).toEqual({ provider_id: 11, model_name: "gemini-pro" });
  });

  it("setSelectedModel(null) 清除持久化，重读为 null", () => {
    useTaskStore.getState().setSelectedModel(makeModel(11, "gemini-pro"));
    useTaskStore.getState().setSelectedModel(null);
    expect(loadPersistedSelectedModel()).toBeNull();
  });

  it("localStorage 非法 JSON 时降级为 null 不抛异常", () => {
    localStorage.setItem("coding-agent.selectedModel", "{这不是合法json");
    expect(() => loadPersistedSelectedModel()).not.toThrow();
    expect(loadPersistedSelectedModel()).toBeNull();
  });

  it("localStorage 缺字段（仅有 model_name）时降级为 null", () => {
    localStorage.setItem("coding-agent.selectedModel", JSON.stringify({ model_name: "only-name" }));
    expect(loadPersistedSelectedModel()).toBeNull();
  });

  it("localStorage 仅有 provider_id（类型不符）时降级为 null", () => {
    localStorage.setItem("coding-agent.selectedModel", JSON.stringify({ provider_id: "abc" }));
    expect(loadPersistedSelectedModel()).toBeNull();
  });

  it("持久化落盘值为完整二元组 JSON（含 provider_id 字段名，非旧 product_id）", () => {
    useTaskStore.getState().setSelectedModel(makeModel(42, "mistral"));
    const raw = localStorage.getItem("coding-agent.selectedModel");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw as string);
    expect(parsed).toHaveProperty("provider_id", 42);
    expect(parsed).toHaveProperty("model_name", "mistral");
    expect(parsed).not.toHaveProperty("product_id");
  });
});
