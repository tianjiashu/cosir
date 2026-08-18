// @vitest-environment happy-dom
/**
 * ProviderSettingsDialog 配置中心测试（设计 §10.2）。
 *
 * 守护不变量：
 * 1. 打开时拉取并渲染厂商卡片（名称/类型/模型数/Key 状态）；
 * 2. 启用 Switch 即时调用 updateProvider；
 * 3. 删除为按钮级二次确认：第一次点击不发请求，确认提示出现后再点才删除；
 * 4. 空态展示「一键导入 DeepSeek」引导，点击后创建厂商并导入默认模型；
 * 5. 删除/导入成功后刷新可用模型缓存（taskStore.refreshAvailableModels 数据源被重拉）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import type { ProviderRecord } from "@shared/model";

vi.mock("@/services/api", () => ({
  listProviders: vi.fn(),
  createProvider: vi.fn(),
  updateProvider: vi.fn(),
  deleteProvider: vi.fn(),
  discoverProviderModels: vi.fn(),
  importProviderModels: vi.fn(),
  listModels: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

import {
  listProviders,
  createProvider,
  updateProvider,
  deleteProvider,
  importProviderModels,
  listModels,
} from "@/services/api";
import { ProviderSettingsDialog } from "@/components/settings/ProviderSettingsDialog";
import { useTaskStore } from "@/stores/taskStore";

const mockListProviders = vi.mocked(listProviders);
const mockCreateProvider = vi.mocked(createProvider);
const mockUpdateProvider = vi.mocked(updateProvider);
const mockDeleteProvider = vi.mocked(deleteProvider);
const mockImportProviderModels = vi.mocked(importProviderModels);
const mockListModels = vi.mocked(listModels);

function makeProvider(overrides: Partial<ProviderRecord> = {}): ProviderRecord {
  return {
    provider_id: "provider-1",
    name: "DeepSeek 官方",
    type: "deepseek",
    base_url: null,
    api_key_configured: true,
    enabled: true,
    model_count: 2,
    sort_order: 0,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
    ...overrides,
  };
}

describe("ProviderSettingsDialog", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    mockListModels.mockResolvedValue([]);
    useTaskStore.setState({ availableModels: [], modelsLoaded: false });
  });

  it("打开时拉取并渲染厂商卡片（名称 / 类型 / 模型数 / Key 状态）", async () => {
    mockListProviders.mockResolvedValue([
      makeProvider(),
      makeProvider({
        provider_id: "provider-2",
        name: "Ollama 本地",
        type: "ollama",
        api_key_configured: false,
        model_count: 0,
      }),
    ]);
    render(<ProviderSettingsDialog open onOpenChange={vi.fn()} />);

    expect(await screen.findByText("DeepSeek 官方")).toBeTruthy();
    expect(screen.getByText("Ollama 本地")).toBeTruthy();
    // Key 未配置徽标（§10.2 告警提示）。
    expect(screen.getByText("Key 未设置")).toBeTruthy();
    // 模型数量。
    expect(screen.getByText("2 模型")).toBeTruthy();
  });

  it("启用 Switch 即时调用 updateProvider（enabled 随开合传参）", async () => {
    const provider = makeProvider({ enabled: true });
    mockListProviders.mockResolvedValue([provider]);
    mockUpdateProvider.mockResolvedValue(makeProvider({ enabled: false }));
    render(<ProviderSettingsDialog open onOpenChange={vi.fn()} />);

    const sw = await screen.findByRole("switch", { name: "启用 DeepSeek 官方" });
    fireEvent.click(sw);
    await waitFor(() => {
      expect(mockUpdateProvider).toHaveBeenCalledWith("provider-1", { enabled: false });
    });
  });

  it("删除为二次确认：第一次点击仅进入确认态，第二次点击才调用 deleteProvider", async () => {
    mockListProviders.mockResolvedValue([makeProvider()]);
    mockDeleteProvider.mockResolvedValue(undefined);
    mockListProviders.mockResolvedValueOnce([makeProvider()]);
    render(<ProviderSettingsDialog open onOpenChange={vi.fn()} />);

    const deleteButton = await screen.findByRole("button", { name: "删除 DeepSeek 官方" });
    fireEvent.click(deleteButton);
    // 第一次点击：进入确认态，不发请求。
    expect(mockDeleteProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/再点一次删除按钮确认/)).toBeTruthy();

    fireEvent.click(deleteButton);
    await waitFor(() => {
      expect(mockDeleteProvider).toHaveBeenCalledWith("provider-1");
    });
  });

  it("空态展示一键导入引导：创建 DeepSeek 厂商并导入默认模型", async () => {
    mockListProviders.mockResolvedValue([]);
    mockCreateProvider.mockResolvedValue(makeProvider({ model_count: 0 }));
    mockImportProviderModels.mockResolvedValue({
      provider_id: "provider-1",
      imported: [],
      skipped_model_names: [],
    });
    render(<ProviderSettingsDialog open onOpenChange={vi.fn()} />);

    // 按角色限定按钮，避免与空态引导段落文案（同样含「一键导入 DeepSeek 官方」）双匹配。
    const importButton = await screen.findByRole("button", { name: /一键导入 DeepSeek 官方/ });
    fireEvent.click(importButton);

    await waitFor(() => {
      // 一键导入不再携带 API Key（Key 由用户在编辑表单中填写，DB 为唯一事实来源）。
      expect(mockCreateProvider).toHaveBeenCalledWith(
        expect.objectContaining({ type: "deepseek", name: "DeepSeek 官方" }),
      );
      // 显式断言调用参数不含 api_key 键（undefined 匹配有歧义，改用键存在性断言）。
      const createCall = mockCreateProvider.mock.calls[0]?.[0] as unknown as
        | Record<string, unknown>
        | undefined;
      expect(createCall).not.toHaveProperty("api_key");
      // 导入请求体为模型创建请求数组（与后端 ModelBulkImportRequest 契约一致）。
      expect(mockImportProviderModels).toHaveBeenCalledWith(
        "provider-1",
        expect.arrayContaining([
          expect.objectContaining({
            model_name: "deepseek/deepseek-v4-flash",
            max_context_window: 128000,
          }),
        ]),
      );
    });
  });

  it("删除成功后刷新可用模型缓存（listModels 被重新拉取，设计 §9.5）", async () => {
    mockListProviders.mockResolvedValue([makeProvider()]);
    mockDeleteProvider.mockResolvedValue(undefined);
    render(<ProviderSettingsDialog open onOpenChange={vi.fn()} />);

    const deleteButton = await screen.findByRole("button", { name: "删除 DeepSeek 官方" });
    fireEvent.click(deleteButton);
    fireEvent.click(deleteButton);

    await waitFor(() => {
      // refreshAll 同时刷 providers 与可用模型缓存（经 taskStore.refreshAvailableModels）。
      expect(mockListProviders.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    await waitFor(() => {
      expect(useTaskStore.getState().modelsLoaded).toBe(true);
    });
  });
});
