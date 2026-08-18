// @vitest-environment happy-dom
/**
 * ModelSelector 组件测试（设计 §10.1）。
 *
 * 守护不变量：
 * 1. 未选择（null）时折叠态显示「选择模型」占位引导显式选择（无 Auto 语义）；
 * 2. 按厂商分组渲染（组头厂商名 + 行内 display_name/窗口缩写/thinking 徽标）；
 * 3. 选中模型回调写入 taskStore（litellm 路由名）；
 * 4. 空态引导「暂无可用模型，点击配置」触发 onOpenSettings；
 * 5. 底部 footer「配置模型 / 管理厂商」触发 onOpenSettings。
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

describe("ModelSelector", () => {
  const onOpenSettings = vi.fn();

  /**
   * 准备测试数据：同步设置 store 缓存与 listModels mock。
   *
   * 打开下拉会触发 refreshAvailableModels（mock listModels 覆盖缓存），
   * 两处数据源必须一致，否则刷新会把 setState 的数据冲掉。
   */
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
      availableModels: [],
      modelsLoaded: true,
    });
  });

  it("未选择（null）时折叠态显示「选择模型」占位", () => {
    seedModels([]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    expect(screen.getByRole("button", { name: "选择模型" }).textContent).toContain("选择模型");
  });

  it("按厂商分组渲染：组头厂商名 + 行内 display_name + 窗口缩写", () => {
    seedModels([
      makeModel(),
      makeModel({
        model_id: "model-2",
        provider_id: "provider-2",
        provider_name: "Ollama 本地",
        model_name: "ollama/qwen3",
        display_name: "qwen3",
        max_context_window: 32000,
        supports_thinking: false,
      }),
    ]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    // 组头（厂商名）与行内展示名各自出现。
    expect(screen.getByText("DeepSeek 官方")).toBeTruthy();
    expect(screen.getByText("Ollama 本地")).toBeTruthy();
    expect(screen.getByText("deepseek-v4-flash")).toBeTruthy();
    expect(screen.getByText("qwen3")).toBeTruthy();
    // 窗口缩写（131072 → 131k；32000 → 32k）。
    expect(screen.getByText("131k")).toBeTruthy();
    expect(screen.getByText("32k")).toBeTruthy();
  });

  it("选中模型写入 taskStore（litellm 路由名）", () => {
    seedModels([makeModel()]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
  });

  it("空态：无可用模型时展示引导并触发 onOpenSettings", async () => {
    seedModels([]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    // cmdk 空列表（无过滤词）时 CommandEmpty 渲染空态引导。
    const guide = await screen.findByText("暂无可用模型，点击配置");
    fireEvent.click(guide);
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it("底部 footer「配置模型 / 管理厂商」触发 onOpenSettings", () => {
    seedModels([makeModel()]);
    render(<ModelSelector onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });
});
