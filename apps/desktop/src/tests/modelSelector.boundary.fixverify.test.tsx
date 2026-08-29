// @vitest-environment happy-dom
/**
 * 推理强度选择器修复后的边界验证（补充覆盖任务提示中的边界点）。
 *
 * 重点验证（不含既有测试已覆盖的 happy path）：
 * 1. 后端真实返回 reasoning_effort 非 null 但 supported=false（effort_map 为空）时，
 *    ModelSelector 完全不渲染档位控件（不灰显、不占噪）。
 * 2. 折叠态标签 currentLabel：支持强度 + 档位非空 → 显示「display_name·档位」；
 *    不支持强度时不追加后缀；未选择时显示「选择模型」。
 * 3. refreshAvailableModels 成功后当前模型为 null（未选模型）时不触碰档位、不崩溃。
 * 4. setSelectedModelName(null) 时：不仅清 selectedModelName，也同步持久化清除 selectedReasoningEffort。
 * 5. 旧缓存/缺字段容错：reasoning_effort 为 undefined（非 null，模拟残缺缓存）时，
 *    refreshAvailableModels 校准路径用 ?.supported 判空，不崩溃。
 * 6. effort_map 含厂商原始键（非前端硬编码）时，档位按钮文本即原始键。
 *
 * @module tests/modelSelector.boundary.fixverify
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import type { ModelEntryRecord } from "@shared/model";

vi.mock("@/services/api", () => ({
  listModels: vi.fn().mockResolvedValue([]),
}));

import { listModels } from "@/services/api";
import { useTaskStore } from "@/stores/taskStore";
import { ModelSelector } from "@/components/chat/ModelSelector";

const mockListModels = vi.mocked(listModels);

function makeModel(overrides: Partial<ModelEntryRecord> = {}): ModelEntryRecord {
  return {
    model_id: "model-1",
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: "deepseek/deepseek-v4-flash",
    display_name: "deepseek-v4-flash",
    max_context_window: 131072,
    supports_thinking: true,
    reasoning_effort: {
      supported: true,
      effort_map: { low: "low", high: "high", max: "max" },
    },
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

describe("ModelSelector 边界：后端返回 supported=false（非 null）不渲染档位控件", () => {
  const onOpenSettings = vi.fn();

  const seedModels = (models: ModelEntryRecord[]) => {
    mockListModels.mockResolvedValue(models);
    useTaskStore.setState({ availableModels: models, modelsLoaded: true });
  };

  beforeEach(() => {
    cleanup();
    onOpenSettings.mockClear();
    mockListModels.mockReset().mockResolvedValue([]);
    useTaskStore.setState({
      selectedModelName: null,
      selectedReasoningEffort: null,
      availableModels: [],
      modelsLoaded: true,
    });
  });

  // 边界：reasoning_effort 为真实非空对象但 supported=false（后端实际可能返回的形态），
  // 与「null 容错」不同，验证判空逻辑对 supported 标志而非对象存在性的依赖。
  it("reasoning_effort 非 null 但 supported=false 时，不渲染档位控件", () => {
    seedModels([
      makeModel({
        model_id: "model-unsupported",
        model_name: "ollama/qwen3",
        display_name: "qwen3",
        reasoning_effort: { supported: false, effort_map: {} },
      }),
    ]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    expect(screen.queryByText("强度")).toBeNull();
    expect(screen.queryByRole("button", { name: "low" })).toBeNull();
  });

  // 边界：折叠态标签后缀逻辑 —— 支持强度 + 档位非空时显示「display_name·档位」。
  it("折叠态标签：支持强度且档位非空时显示 display_name·档位", () => {
    seedModels([makeModel()]);
    useTaskStore.setState({
      selectedModelName: "deepseek/deepseek-v4-flash",
      selectedReasoningEffort: "high",
    });
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    const trigger = screen.getByRole("button", { name: "选择模型" });
    expect(trigger.textContent).toContain("deepseek-v4-flash·high");
  });

  // 边界：支持强度但档位为 null（未指定）时，折叠态仅显示 display_name，不追加空后缀。
  it("折叠态标签：支持强度但档位为 null 时不追加后缀", () => {
    seedModels([makeModel()]);
    useTaskStore.setState({
      selectedModelName: "deepseek/deepseek-v4-flash",
      selectedReasoningEffort: null,
    });
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    const trigger = screen.getByRole("button", { name: "选择模型" });
    expect(trigger.textContent).toContain("deepseek-v4-flash");
    expect(trigger.textContent).not.toContain("·");
  });

  // 边界：effort_map 含厂商原始键（非前端硬编码），档位按钮文本即原始键。
  it("档位名来自 effort_map 原始键（厂商语义，如 custom 厂商自定义档位）", () => {
    seedModels([
      makeModel({
        reasoning_effort: {
          supported: true,
          effort_map: { minimal: "minimal", balanced: "balanced", ultra: "ultra" },
        },
      }),
    ]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    expect(screen.getByRole("button", { name: "minimal" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "balanced" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "ultra" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "low" })).toBeNull();
  });
});

describe("taskStore 边界：refreshAvailableModels 校准 + setSelectedModelName(null) 持久化", () => {
  beforeEach(() => {
    localStorage.clear();
    mockListModels.mockReset();
    useTaskStore.setState({
      selectedModelName: null,
      selectedReasoningEffort: null,
      availableModels: [],
      modelsLoaded: false,
    });
  });

  // 边界：当前模型为 null（未选择）时，refreshAvailableModels 成功后不触碰档位（不崩溃）。
  it("refreshAvailableModels 成功后当前模型为 null 时不操作档位", async () => {
    const models = [makeModel()];
    mockListModels.mockResolvedValue(models);
    useTaskStore.setState({
      availableModels: [],
      modelsLoaded: false,
      selectedModelName: null,
      selectedReasoningEffort: null,
    });
    const ok = await useTaskStore.getState().refreshAvailableModels();
    expect(ok).toBe(true);
    expect(useTaskStore.getState().selectedReasoningEffort).toBeNull();
    expect(useTaskStore.getState().selectedModelName).toBeNull();
  });

  // 边界：setSelectedModelName(null) 同时清档位并持久化清除。
  it("setSelectedModelName(null) 时同步持久化清除 selectedReasoningEffort", () => {
    useTaskStore.setState({ selectedReasoningEffort: "high" });
    useTaskStore.getState().setSelectedReasoningEffort("high");
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBe("high");
    // 清模型（切回未选择）：档位也应被清 + 持久化清除。
    useTaskStore.getState().setSelectedModelName(null);
    expect(useTaskStore.getState().selectedModelName).toBeNull();
    expect(useTaskStore.getState().selectedReasoningEffort).toBeNull();
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBeNull();
  });

  // 容错：reasoning_effort 缺字段（undefined，模拟残缺/旧缓存），校准路径 ?.supported 判空不崩溃。
  it("旧缓存 reasoning_effort 缺字段时 refreshAvailableModels 校准不崩溃", async () => {
    // 构造一个残缺对象：无 reasoning_effort 字段（undefined）。
    const broken = makeModel();
    delete (broken as { reasoning_effort?: unknown }).reasoning_effort;
    useTaskStore.setState({
      availableModels: [broken as ModelEntryRecord],
      modelsLoaded: false,
      selectedModelName: "deepseek/deepseek-v4-flash",
      selectedReasoningEffort: "high",
    });
    mockListModels.mockResolvedValue([broken as ModelEntryRecord]);
    const ok = await useTaskStore.getState().refreshAvailableModels();
    // 缺字段模型 reasoning_effort?.supported !== true → 残留档位应被清空（安全默认）。
    expect(ok).toBe(true);
    expect(useTaskStore.getState().selectedReasoningEffort).toBeNull();
  });
});
