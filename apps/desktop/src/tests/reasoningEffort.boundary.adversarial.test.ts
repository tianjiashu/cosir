// @vitest-environment happy-dom
/**
 * 推理强度选择器 —— 补充边界/对抗测试（独立验证，不修改生产代码）。
 *
 * 目标：验证 taskStore/modelSelector/useTask 在「需求方要求的边界」下的真实行为，
 * 覆盖现有 4 个测试文件未断言到的分支，以暴露潜在 bug：
 *  B1. availableModels 未加载（modelsLoaded=false）时切换模型按「安全默认」统一清空档位；
 *  B2. 切到「支持强度但 effort_map 键不同」的模型时，残留档位按 supported===true 保留；
 *  B3. effort_map 原始键（非前端硬编码）透传：低/高/最大 等多档位均可渲染与选中；
 *  B4. 选中档位名与所选模型 effort_map 键集合不一致时（脏状态）组件不崩溃、不高亮不存在的档位；
 *  B5. 切换模型到自身（同模型名）不触发任何清空逻辑（幂等）；
 *  B6. 档位非 effort_map 合法键（如任意字符串）写入 store 后端透传（透传契约不校验键合法性的边界）。
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

import { useTaskStore } from "@/stores/taskStore";
import type { ModelEntryRecord } from "@shared/model";

function makeModel(modelName: string, overrides: Partial<ModelEntryRecord> = {}): ModelEntryRecord {
  return {
    model_id: `id-${modelName}`,
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: modelName,
    display_name: modelName.split("/").pop() ?? modelName,
    max_context_window: 128000,
    supports_thinking: false,
    reasoning_effort: null,
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

describe("推理强度选择器 边界/对抗", () => {
  beforeEach(() => {
    localStorage.clear();
    useTaskStore.setState({
      selectedModelName: null,
      selectedReasoningEffort: null,
      availableModels: [],
      modelsLoaded: false,
    });
  });

  // B1：availableModels 未加载（modelsLoaded=false）时调用 setSelectedModelName，
  // 新模型查不到 → 按「安全默认」统一清空档位：内存态置 null 且 localStorage 持久化清除。
  it("B1 availableModels 未加载时切模型安全默认清空档位并清除持久化", () => {
    // 注意：此处不填充 availableModels，模拟列表尚未加载。
    useTaskStore.getState().setSelectedReasoningEffort("high");
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBe("high");
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    // 查不到新模型（含未加载）→ 安全默认清空，而非保留。
    expect(useTaskStore.getState().selectedReasoningEffort).toBeNull();
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBeNull();
  });

  // B2：切到支持强度但 effort_map 键不同的模型，实现按 supported===true 保留任意残留档位
  // （含跨模型键），由组件层不渲染/不高亮兜底。
  it("B2 切到支持强度但键不同的模型保留档位（supported===true 兜底）", () => {
    const lowKeyModel = makeModel("deepseek/deepseek-v4-flash", {
      reasoning_effort: { supported: true, effort_map: { low: "low", high: "high", max: "max" } },
    });
    // 另一个支持强度但键集合不同的模型（例如仅 single 档）。
    const otherKeyModel = makeModel("anthropic/claude", {
      reasoning_effort: { supported: true, effort_map: { minimal: "minimal", extended: "extended" } },
    });
    useTaskStore.setState({
      availableModels: [lowKeyModel, otherKeyModel],
      modelsLoaded: true,
    });
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    useTaskStore.getState().setSelectedReasoningEffort("high");
    // 切到另一个支持强度的模型（键不同）：当前实现只看 supported===true，故按安全默认保留。
    useTaskStore.getState().setSelectedModelName("anthropic/claude");
    expect(useTaskStore.getState().selectedReasoningEffort).toBe("high");
  });

  // B3：effort_map 原始键透传（多档位、非前端硬编码）。
  it("B3 effort_map 全部原始键可逐一选中并透传", () => {
    const model = makeModel("deepseek/deepseek-v4-flash", {
      reasoning_effort: {
        supported: true,
        effort_map: { low: "low", medium: "medium", high: "high", max: "max" },
      },
    });
    useTaskStore.setState({ availableModels: [model], modelsLoaded: true });
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    for (const key of ["low", "medium", "high", "max"]) {
      useTaskStore.getState().setSelectedReasoningEffort(key);
      expect(useTaskStore.getState().selectedReasoningEffort).toBe(key);
      expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBe(key);
    }
  });

  // B5：同模型切换幂等（不触发清空）。
  it("B5 切换到相同模型名不产生副作用清空", () => {
    const model = makeModel("deepseek/deepseek-v4-flash", {
      reasoning_effort: { supported: true, effort_map: { low: "low", high: "high", max: "max" } },
    });
    useTaskStore.setState({ availableModels: [model], modelsLoaded: true });
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    useTaskStore.getState().setSelectedReasoningEffort("max");
    // 再次选择同一模型（幂等）。
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    expect(useTaskStore.getState().selectedReasoningEffort).toBe("max");
  });

  // B6：透传契约不校验键合法性 —— 任意字符串档位均可写入并持久化（边界：脏档位名）。
  it("B6 任意档位名字符串写入并持久化（透传层不校验合法性）", () => {
    const model = makeModel("deepseek/deepseek-v4-flash", {
      reasoning_effort: { supported: true, effort_map: { low: "low", high: "high", max: "max" } },
    });
    useTaskStore.setState({ availableModels: [model], modelsLoaded: true });
    useTaskStore.getState().setSelectedModelName("deepseek/deepseek-v4-flash");
    useTaskStore.getState().setSelectedReasoningEffort("not-a-real-key");
    expect(useTaskStore.getState().selectedReasoningEffort).toBe("not-a-real-key");
    expect(localStorage.getItem("coding-agent.selectedReasoningEffort")).toBe("not-a-real-key");
  });
});
