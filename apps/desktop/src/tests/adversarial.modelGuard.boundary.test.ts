// @vitest-environment happy-dom
/**
 * 缺陷发现型测试：validateModelSend 纯函数 + useModelSendGuard.guardSend 边界。
 *
 * 重点缺陷类型（2026-08-18 无 Auto 语义改造）：
 * - B1 规则 3 只做 model_name 存在性匹配，不校验条目 enabled（禁用模型可能放行）；
 * - B2 modelsLoaded=false 但缓存有旧数据的拦截语义；
 * - B3 selectedModelName 空字符串是否绕过拦截；
 * - B4 同 model_name 重复条目的顺序敏感；
 * - B5 guardSend 并发补拉无防抖；B6 补拉失败旧缓存误放行防护。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ModelEntryRecord } from "@shared/model";
import {
  validateModelSend,
  useModelSendGuard,
  type ModelSendGuardInput,
} from "@/hooks/useModelSendGuard";
import { useTaskStore } from "@/stores/taskStore";

vi.mock("@/services/api", () => ({ listModels: vi.fn() }));
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));

import { listModels } from "@/services/api";
const mockListModels = vi.mocked(listModels);

function makeModel(overrides: Partial<ModelEntryRecord> = {}): ModelEntryRecord {
  return {
    model_id: "model-1",
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: "deepseek/deepseek-v4-flash",
    display_name: "deepseek-v4-flash",
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
    ...overrides,
  };
}

function makeInput(overrides: Partial<ModelSendGuardInput> = {}): ModelSendGuardInput {
  const model = makeModel();
  return {
    availableModels: [model],
    modelsLoaded: true,
    selectedModelName: model.model_name,
    ...overrides,
  };
}

describe("B1 validateModelSend：enabled=false 条目", () => {
  beforeEach(() => {
    mockListModels.mockReset().mockResolvedValue([]);
    useTaskStore.setState({ selectedModelName: null, availableModels: [], modelsLoaded: false });
  });

  // 测试目的：规则 3 只按 model_name 存在性判断，不校验条目 enabled。
  // 可能发现的缺陷：缓存被污染（enabled=false 混入）时放行禁用模型。
  it("命中 enabled=false 条目 → 仍返回 ok:true（未校验 enabled 字段）", () => {
    const disabled = makeModel({ enabled: false });
    const result = validateModelSend({
      availableModels: [disabled],
      modelsLoaded: true,
      selectedModelName: disabled.model_name,
    });
    expect(result.ok).toBe(true);
  });

  // 测试目的：同 model_name 重复条目时 find 命中第一条，拦截判定顺序敏感。
  // 可能发现的缺陷：第一条启用的会掩盖第二条 api_key_configured=false。
  it("同 model_name 重复条目：命中第一条的 key 判定（顺序敏感）", () => {
    const enabled = makeModel({ model_id: "m1", model_name: "dup/model", api_key_configured: true });
    const noKey = makeModel({ model_id: "m2", model_name: "dup/model", api_key_configured: false });
    expect(
      validateModelSend({ availableModels: [enabled, noKey], modelsLoaded: true, selectedModelName: "dup/model" }).ok,
    ).toBe(true);
    const r2 = validateModelSend({
      availableModels: [noKey, enabled],
      modelsLoaded: true,
      selectedModelName: "dup/model",
    });
    expect(r2.ok).toBe(false);
    if (!r2.ok) expect(r2.block.reason).toBe("api_key_missing");
  });
});

describe("B2/B3/B4 validateModelSend 边界", () => {
  // 测试目的：modelsLoaded=false 即使缓存非空也拦截（旧缓存不误放行）。
  it("modelsLoaded=false + 缓存非空 → no_models 拦截", () => {
    const r = validateModelSend(makeInput({ modelsLoaded: false }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.block.reason).toBe("no_models");
  });

  // 测试目的：selectedModelName=""（空字符串，非 null）不绕过拦截。
  it("selectedModelName 空字符串 → model_missing 拦截（不绕过）", () => {
    const r = validateModelSend(makeInput({ selectedModelName: "" }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.block.reason).toBe("model_missing");
  });

  // 测试目的：规则优先级——modelsLoaded=false 先于 selectedModelName=null。
  it("规则优先级：modelsLoaded=false 优先于 null 选择 → no_models", () => {
    const r = validateModelSend(makeInput({ modelsLoaded: false, selectedModelName: null }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.block.reason).toBe("no_models");
  });
});

describe("B5/B6 guardSend hook 行为", () => {
  beforeEach(() => {
    mockListModels.mockReset().mockResolvedValue([]);
    useTaskStore.setState({ selectedModelName: null, availableModels: [], modelsLoaded: false });
  });

  // 测试目的：modelsLoaded=false 时 guardSend 先补拉，成功后按新缓存放行。
  it("补拉成功 → 放行（新缓存校验）", async () => {
    const model = makeModel();
    mockListModels.mockResolvedValue([model]);
    useTaskStore.setState({ availableModels: [], modelsLoaded: false, selectedModelName: model.model_name });
    const { result } = renderHook(() => useModelSendGuard());
    const res = await result.current.guardSend();
    expect(mockListModels).toHaveBeenCalledTimes(1);
    expect(res.ok).toBe(true);
  });

  // 测试目的：补拉失败 → no_models 拦截（旧缓存不误放行）。
  it("补拉失败 + 缓存有旧数据 → no_models 拦截", async () => {
    const model = makeModel();
    mockListModels.mockRejectedValue(new Error("down"));
    useTaskStore.setState({ availableModels: [model], modelsLoaded: false, selectedModelName: model.model_name });
    const { result } = renderHook(() => useModelSendGuard());
    const res = await result.current.guardSend();
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.block.reason).toBe("no_models");
  });

  // 测试目的：modelsLoaded=true 时不补拉直接校验。
  it("modelsLoaded=true 不补拉", async () => {
    const model = makeModel();
    useTaskStore.setState({ availableModels: [model], modelsLoaded: true, selectedModelName: model.model_name });
    const { result } = renderHook(() => useModelSendGuard());
    const res = await result.current.guardSend();
    expect(mockListModels).not.toHaveBeenCalled();
    expect(res.ok).toBe(true);
  });

  // 测试目的：并发调用 guardSend（modelsLoaded=false）→ refreshAvailableModels 被调 2 次。
  // 可能发现的缺陷：无防抖/无 in-flight 去重，快速双击会发重复 GET /models 请求。
  it("并发 guardSend 两次 → listModels 被调 2 次（无去重）", async () => {
    const model = makeModel();
    mockListModels.mockResolvedValue([model]);
    useTaskStore.setState({ availableModels: [], modelsLoaded: false, selectedModelName: model.model_name });
    const { result } = renderHook(() => useModelSendGuard());
    const [r1, r2] = await Promise.all([result.current.guardSend(), result.current.guardSend()]);
    expect(mockListModels).toHaveBeenCalledTimes(2);
    expect(r1.ok && r2.ok).toBe(true);
  });
});
