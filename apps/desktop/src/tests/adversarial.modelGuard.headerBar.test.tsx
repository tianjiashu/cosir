// @vitest-environment happy-dom
/**
 * 缺陷发现型测试：TaskHeaderBar 受控/非受控双模 + ModelSelector enabled 边界。
 *
 * 重点缺陷类型：
 * - H1 受控模式：settingsOpen + onSettingsOpenChange 成对时行为正确；
 * - H2 半受控（只传 settingsOpen 不传回调）：对话框无法关闭 / 无法打开；
 * - H3 非受控默认自治：打开 → 关闭正常；
 * - H4 受控 → 非受控切换；
 * - H5 ModelSelector 下拉展示 enabled=false 的禁用模型（可能被选中并发送）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ModelEntryRecord } from "@shared/model";

const listAgentsMock = vi.hoisted(() => vi.fn().mockResolvedValue({ agents: [], default_agent_id: "developer" }));
const listModelsMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
const listProvidersMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));

vi.mock("@/services/api", () => ({
  listAgents: listAgentsMock,
  listModels: listModelsMock,
  listProviders: listProvidersMock,
}));
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));

import { TaskHeaderBar } from "@/components/chat/TaskHeaderBar";
import { ModelSelector } from "@/components/chat/ModelSelector";
import { useTaskStore } from "@/stores/taskStore";
import { useAgentStore } from "@/stores/agentStore";

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

function reset() {
  cleanup();
  useTaskStore.setState({
    selectedAgentId: "developer",
    selectedModelName: null,
    availableModels: [],
    modelsLoaded: true,
  });
  useAgentStore.setState({ agents: [], defaultAgentId: "developer", loaded: false });
  listAgentsMock.mockReset().mockResolvedValue({ agents: [], default_agent_id: "developer" });
  listModelsMock.mockReset().mockResolvedValue([]);
  listProvidersMock.mockReset().mockResolvedValue([]);
}

beforeEach(reset);

describe("H1 受控模式（settingsOpen + onSettingsOpenChange 成对）", () => {
  // 测试目的：受控模式下点击 footer 应转发给宿主的 onSettingsOpenChange。
  it("点击「配置模型 / 管理厂商」→ onSettingsOpenChange(true) 被调（宿主接管）", () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    const onChange = vi.fn();
    render(<TaskHeaderBar settingsOpen={false} onSettingsOpenChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(onChange).toHaveBeenCalledWith(true);
    // 宿主未更新 state → dialog 不出现（受控语义）
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("H2 半受控模式缺陷", () => {
  // 测试目的：只传 settingsOpen=true 不传 onSettingsOpenChange → 关闭按钮无法关闭。
  // 可能发现的缺陷：settingsOpen 恒为 controlledOpen，内部 setLocalSettingsOpen 失效，
  // 对话框永久打开无法关闭（半受控无防御）。
  it("只传 settingsOpen=true（无回调）→ dialog 打开后无法关闭", async () => {
    render(<TaskHeaderBar settingsOpen={true} />);
    expect(screen.getByRole("dialog")).toBeTruthy();
    const closeButton = await screen.findByRole("button", { name: "关闭" });
    fireEvent.click(closeButton);
    await waitFor(() => {
      // 缺陷表现：仍渲染 dialog（无法关闭）
      expect(screen.getByRole("dialog")).toBeTruthy();
    });
  });

  // 测试目的：只传 onSettingsOpenChange（不传 settingsOpen）且宿主不更新受控值 →
  // 内部 localSettingsOpen 一直 false → 打开入口无效。
  // 可能发现的缺陷：半受控（回调方不写回）时对话框永远打不开，UI 无反应。
  it("只传 onSettingsOpenChange（宿主不写回）→ dialog 无法打开", async () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    const onChange = vi.fn();
    render(<TaskHeaderBar onSettingsOpenChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(onChange).toHaveBeenCalledWith(true);
    await waitFor(() => {
      // 缺陷表现：dialog 未打开（无任何视觉反馈）
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});

describe("H3 非受控默认自治", () => {
  // 测试目的：默认不传 props 时内部自治：打开 → 关闭正常。
  it("打开 → 关闭 对话框生命周期正常", async () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    render(<TaskHeaderBar />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(screen.getByRole("dialog")).toBeTruthy();
    const closeButton = await screen.findByRole("button", { name: "关闭" });
    fireEvent.click(closeButton);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});

describe("H4 受控 → 非受控切换", () => {
  // 测试目的：先受控（settingsOpen=false）后移除受控 props，自治能力应恢复。
  it("切回非受控后点击 footer 能打开 dialog", async () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    const { rerender } = render(
      <TaskHeaderBar settingsOpen={false} onSettingsOpenChange={vi.fn()} />,
    );
    rerender(<TaskHeaderBar />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

describe("H5 ModelSelector 禁用模型边界", () => {
  // 测试目的：store 缓存中出现 enabled=false 条目时下拉是否渲染。
  // 可能发现的缺陷：ModelSelector 不过滤 enabled=false，禁用模型可被选中，
  // 配合 validateModelSend 不校验 enabled → 禁用模型可被发送。
  it("availableModels 含 enabled=false 条目 → 下拉仍渲染该模型（可选中）", () => {
    const disabled = makeModel({ enabled: false, display_name: "disabled-model" });
    listModelsMock.mockResolvedValue([disabled]);
    useTaskStore.setState({ availableModels: [disabled], modelsLoaded: true });
    render(<ModelSelector onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    const option = screen.getByRole("option", { name: /disabled-model/ });
    expect(option).toBeTruthy();
    fireEvent.click(option);
    // 禁用模型被写入 store（选中路径无 enabled 拦截）
    expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
  });

  // 测试目的：空态（0 模型）时不渲染 CommandEmpty「未找到匹配的模型」，避免语义冗余。
  it("空态：展示引导按钮且不渲染 CommandEmpty 文案", async () => {
    render(<ModelSelector onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    expect(await screen.findByText("暂无可用模型，点击配置")).toBeTruthy();
    expect(screen.queryByText("未找到匹配的模型")).toBeNull();
  });
});
