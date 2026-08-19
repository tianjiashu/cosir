// @vitest-environment happy-dom
/**
 * 缺陷发现型测试：NewTaskPage / InputBar 的 guardSend 拦截路径状态清理与并发。
 *
 * 重点缺陷类型：
 * - S1 拦截后 guardMessage 是否残留（再次发送成功/切换模型后是否清除）；
 * - S2 openSettings 联动打开 → 关闭 → 再发送是否正常；
 * - S3 快速重复 Enter（guardSend 挂起期间）是否重复创建任务/发送（无防抖）；
 * - S4 拦截路径重复 Enter 无副作用；
 * - S5 InputBar api_key_missing 联动 onOpenSettings。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const taskMocks = vi.hoisted(() => ({
  createTask: vi.fn(),
  createTurn: vi.fn(),
  cancelTurn: vi.fn(),
}));
const guardSendMock = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({
    createTask: taskMocks.createTask,
    createTurn: taskMocks.createTurn,
    cancelTurn: taskMocks.cancelTurn,
    operation: { loading: false, error: null, eventsError: null },
  }),
}));
vi.mock("@/hooks/useModelSendGuard", () => ({
  useModelSendGuard: () => ({ guardSend: guardSendMock }),
}));
// TaskHeaderBar 桩：暴露 settingsOpen prop 与 onSettingsOpenChange，便于断言联动与模拟关闭。
vi.mock("@/components/chat/TaskHeaderBar", () => ({
  TaskHeaderBar: ({
    settingsOpen,
    onSettingsOpenChange,
  }: {
    settingsOpen?: boolean;
    onSettingsOpenChange?: (open: boolean) => void;
  }) => (
    <button
      type="button"
      data-testid="mock-header-bar"
      data-open={String(settingsOpen ?? false)}
      onClick={() => onSettingsOpenChange?.(false)}
    />
  ),
}));
vi.mock("@/services/workspace", () => ({ pickAndCreateWorkspace: vi.fn() }));
vi.mock("@/services/tracePropagation", () => ({ beginClientTrace: vi.fn(), endClientTrace: vi.fn() }));
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));
vi.mock("@/lib/logger", () => ({ logInfo: vi.fn(), logWarn: vi.fn(), logError: vi.fn(), logDebug: vi.fn() }));

import { InputBar } from "@/components/layout/InputBar";
import { NewTaskPage } from "@/pages/chat/NewTaskPage";

const BLOCK_SELECT = {
  ok: false,
  block: { reason: "no_model_selected", message: "请先选择模型再开始对话", openSettings: false },
} as const;
const BLOCK_NOMODELS = {
  ok: false,
  block: { reason: "no_models", message: "未配置任何模型，请先在配置中心添加模型厂商", openSettings: true },
} as const;
const BLOCK_APIKEY = {
  ok: false,
  block: {
    reason: "api_key_missing",
    message: "所选模型的厂商 API Key 未配置，请在配置中心为该厂商填写 API Key",
    openSettings: true,
  },
} as const;

/**
 * 构造一个已配置 Key 的可用模型条目（guardSend 放行基线）。
 *
 * @param overrides - 需要覆盖的字段（如 `api_key_configured: false`）。
 */
function makeConfiguredModel(overrides: Record<string, unknown> = {}) {
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
  } as never;
}

function resetStores() {
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    selectedAgentId: "developer",
    selectedModelName: null,
    availableModels: [],
    modelsLoaded: true,
  });
  useWorkspaceStore.setState({
    workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
    activeWorkspaceId: "ws-1",
  });
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
}

beforeEach(() => {
  resetStores();
  vi.clearAllMocks();
  taskMocks.createTask.mockResolvedValue(true);
  taskMocks.createTurn.mockResolvedValue(true);
  guardSendMock.mockReset();
});

describe("S1 guardMessage 状态清理（NewTaskPage）", () => {
  // 测试目的：拦截后展示 guardMessage，随后放行应清除并创建任务。
  it("拦截 → 放行：guardMessage 清除 + createTask 被调", async () => {
    guardSendMock
      .mockResolvedValueOnce(BLOCK_SELECT)
      .mockResolvedValueOnce({ ok: true });
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("请先选择模型再开始对话")).toBeTruthy());
    expect(taskMocks.createTask).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(taskMocks.createTask).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("请先选择模型再开始对话")).toBeNull();
  });

  // 测试目的：拦截后用户切换模型（store 变更），guardMessage 应自动清除。
  // 缺陷修复（2026-08-18）：NewTaskPage 监听 selectedModelName 变化后清除提示；
  // useEffect 在微任务中生效，故用 waitFor 异步断言。
  it("拦截后切换模型 → guardMessage 自动清除", async () => {
    guardSendMock.mockResolvedValue(BLOCK_SELECT);
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("请先选择模型再开始对话")).toBeTruthy());
    // 模拟用户通过 ModelSelector 选中模型
    useTaskStore.setState({ selectedModelName: "deepseek/deepseek-v4-flash" });
    // 期望：切换模型后提示自动消失（useEffect 异步清除）
    await waitFor(() => {
      expect(screen.queryByText("请先选择模型再开始对话")).toBeNull();
    });
  });
});

describe("S2 openSettings 联动打开 → 关闭 → 再发送（NewTaskPage）", () => {
  // 测试目的：no_models 拦截联动 settingsOpen=true；关闭后未配置再发送仍拦截并再次联动。
  it("关闭配置中心后再发送（仍未配置）→ 再次拦截并再次联动打开", async () => {
    guardSendMock.mockResolvedValue(BLOCK_NOMODELS);
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByTestId("mock-header-bar").getAttribute("data-open")).toBe("true");
    });
    expect(screen.getByText("未配置任何模型，请先在配置中心添加模型厂商")).toBeTruthy();
    expect(taskMocks.createTask).not.toHaveBeenCalled();

    // 用户关闭配置中心
    fireEvent.click(screen.getByTestId("mock-header-bar"));
    expect(screen.getByTestId("mock-header-bar").getAttribute("data-open")).toBe("false");

    // 再发送：仍无模型 → 再次拦截并再次联动打开
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByTestId("mock-header-bar").getAttribute("data-open")).toBe("true");
    });
    expect(taskMocks.createTask).not.toHaveBeenCalled();
  });

  // 测试目的：配置完成（guardSend 放行）后发送成功，guardMessage 清除且 settingsOpen 不被误关。
  it("配置后放行：createTask 被调 + guardMessage 清除", async () => {
    guardSendMock
      .mockResolvedValueOnce(BLOCK_NOMODELS)
      .mockResolvedValueOnce({ ok: true });
    const onCreated = vi.fn();
    render(<NewTaskPage onCreated={onCreated} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByTestId("mock-header-bar").getAttribute("data-open")).toBe("true");
    });
    // 模拟用户在配置中心完成配置（guardSend 下次放行）
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(taskMocks.createTask).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("未配置任何模型，请先在配置中心添加模型厂商")).toBeNull();
  });
});

describe("S3/S4 并发与重复 Enter（NewTaskPage）", () => {
  // 测试目的：guardSend 挂起期间快速按两次 Enter，两次都放行 → createTask 被调 2 次。
  // 可能发现的缺陷：放行路径无防抖/in-flight 保护，快速双击会重复创建任务。
  it("快速双击 Enter（guardSend 挂起）→ createTask 被调 2 次（重复创建缺陷）", async () => {
    const resolvers: Array<(v: { ok: boolean }) => void> = [];
    guardSendMock.mockImplementation(() => new Promise((res) => resolvers.push(res)));

    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(guardSendMock).toHaveBeenCalledTimes(2);

    resolvers[0]!({ ok: true });
    resolvers[1]!({ ok: true });
    await waitFor(() => expect(taskMocks.createTask).toHaveBeenCalledTimes(2));
  });

  // 测试目的：拦截路径快速重复 Enter 无副作用（不创建、提示不变）。
  it("拦截路径重复 Enter → createTask 0 次、guardMessage 显示、guardSend 调 2 次", async () => {
    guardSendMock.mockResolvedValue(BLOCK_SELECT);
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByText("请先选择模型再开始对话")).toBeTruthy();
      expect(guardSendMock).toHaveBeenCalledTimes(2);
    });
    expect(taskMocks.createTask).not.toHaveBeenCalled();
  });
});

describe("S5 InputBar 拦截/放行/并发", () => {
  // 默认选中已配置 Key 的模型（按钮可点，guardSend 运行时路径可达）。
  // 未选模型时按钮置灰是新的主路径（见 S6），此处的 guardSend 语义作为防御保留。
  function renderInputBar(onOpenSettings = vi.fn()) {
    useTaskStore.setState({
      activeTaskId: "t-1",
      selectedModelName: "deepseek/deepseek-v4-flash",
      availableModels: [makeConfiguredModel()],
      modelsLoaded: true,
    });
    const utils = render(<InputBar onOpenSettings={onOpenSettings} />);
    const input = screen.getByPlaceholderText("给 Agent 下达任务...");
    return { input, onOpenSettings, ...utils };
  }

  // 测试目的：api_key_missing 拦截 → guardMessage 展示 + onOpenSettings 联动。
  it("api_key_missing 拦截 → guardMessage + onOpenSettings 被调，createTurn 不被调", async () => {
    // 选中模型存在但厂商 Key 未配置（api_key_configured=false）。
    useTaskStore.setState({
      availableModels: [makeConfiguredModel({ api_key_configured: false })],
    });
    guardSendMock.mockResolvedValue(BLOCK_APIKEY);
    const { input, onOpenSettings } = renderInputBar();
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByText(/API Key 未配置/)).toBeTruthy();
      expect(onOpenSettings).toHaveBeenCalledTimes(1);
    });
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
  });

  // 测试目的：放行 → guardMessage 清除 + createTurn 被调。
  it("放行 → createTurn 被调 + guardMessage 清除", async () => {
    useTaskStore.setState({
      availableModels: [makeConfiguredModel({ api_key_configured: false })],
    });
    guardSendMock
      .mockResolvedValueOnce(BLOCK_APIKEY)
      .mockResolvedValueOnce({ ok: true });
    const { input } = renderInputBar();
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/API Key 未配置/)).toBeTruthy());

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(taskMocks.createTurn).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/API Key 未配置/)).toBeNull();
  });

  // 测试目的：guardSend 挂起期间快速两次 Enter → createTurn 被调 2 次。
  // 可能发现的缺陷：放行路径无防抖，快速双击重复发送 turn。
  it("快速双击 Enter（guardSend 挂起）→ createTurn 被调 2 次（重复发送缺陷）", async () => {
    const resolvers: Array<(v: { ok: boolean }) => void> = [];
    guardSendMock.mockImplementation(() => new Promise((res) => resolvers.push(res)));
    const { input } = renderInputBar();
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(guardSendMock).toHaveBeenCalledTimes(2);
    resolvers[0]!({ ok: true });
    resolvers[1]!({ ok: true });
    await waitFor(() => expect(taskMocks.createTurn).toHaveBeenCalledTimes(2));
  });

  // 测试目的：拦截路径重复 Enter → createTurn 0 次、onOpenSettings 调用次数=拦截次数。
  it("拦截路径重复 Enter → createTurn 0 次（无副作用）", async () => {
    guardSendMock.mockResolvedValue(BLOCK_NOMODELS);
    const { input, onOpenSettings } = renderInputBar();
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(guardSendMock).toHaveBeenCalledTimes(2));
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
    expect(onOpenSettings).toHaveBeenCalledTimes(2);
  });
});

describe("S6 InputBar 未选模型按钮置灰（方案 §阶段 1.5）", () => {
  // 未选模型（selectedModelName=null）时，发送按钮应 disabled 且 hover 提示
  // 「请先选择模型」，guardSend 不被调用（主路径由按钮置灰承担，guardSend 仅防御）。
  function renderWithoutModel(onOpenSettings = vi.fn()) {
    useTaskStore.setState({
      activeTaskId: "t-1",
      selectedModelName: null,
      availableModels: [makeConfiguredModel()],
      modelsLoaded: true,
    });
    const utils = render(<InputBar onOpenSettings={onOpenSettings} />);
    const input = screen.getByPlaceholderText("给 Agent 下达任务...");
    const sendButton = screen.getByRole("button", { name: "发送" });
    return { input, sendButton, onOpenSettings, ...utils };
  }

  it("未选模型时发送按钮 disabled，Enter 不触发 guardSend / createTurn", async () => {
    const { input, sendButton } = renderWithoutModel();
    expect(sendButton.getAttribute("disabled")).not.toBeNull();
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    // canSend=false 使 handleSend 直接返回，guardSend 与 createTurn 均不被调。
    expect(guardSendMock).not.toHaveBeenCalled();
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
  });

  it("选中模型后发送按钮变为可点，Enter 触发 guardSend 放行链路", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    const { input, sendButton } = renderWithoutModel();
    expect(sendButton.getAttribute("disabled")).not.toBeNull();
    // 用户通过 ModelSelector 选中模型（store 变更，触发重渲染）。
    act(() => {
      useTaskStore.setState({ selectedModelName: "deepseek/deepseek-v4-flash" });
    });
    fireEvent.change(input, { target: { value: "hello" } });
    const sendAfterSelect = screen.getByRole("button", { name: "发送" });
    expect(sendAfterSelect.getAttribute("disabled")).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(taskMocks.createTurn).toHaveBeenCalledTimes(1));
  });
});
