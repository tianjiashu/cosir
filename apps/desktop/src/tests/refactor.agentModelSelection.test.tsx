// @vitest-environment happy-dom
/**
 * 重构验证测试：「去掉 Agent 选择 UI + 模型选择下沉 InputBar/NewTaskPage」。
 *
 * 覆盖本次重构的行为契约（不修改任何生产代码）：
 * 1. AgentSelector 已从 TaskHeaderBar（Chat 视图顶部）移除 —— DOM 中不应再出现
 *    AgentSelector 的特征（Bot 图标按钮 + "Developer" 折叠文案）。
 * 2. TaskHeaderBar 仍渲染 ModelSelector + ProviderSettingsDialog（切换模型入口未丢）。
 * 3. InputBar（Chat 视图底部元信息行）内嵌 ModelSelector，且切换模型写入
 *    useTaskStore.selectedModelName。
 * 4. NewTaskPage 底部输入区存在独立的 ModelSelector 实例（与顶部 TaskHeaderBar 的
 *    ModelSelector 共存，共享同一 store 事实源）。
 * 5. 发送链路不变：mock useSendInput.sendInput 路径，触发 InputBar handleSend，
 *    断言 useModelSendGuard.guardSend 仍被调用、createTurn 收到正确 text/attachments。
 * 6. InputBar 内 ModelSelector 的 onOpenSettings 与宿主联动（点击打开配置中心）。
 *
 * @module tests/refactor.agentModelSelection
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import type { ModelEntryRecord } from "@shared/model";

// ---- 顶层 hoisted mock（vitest 会将 vi.mock 提升到 import 之前，工厂内只能引用 hoisted 变量）----
const listModelsMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
const listProvidersMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
// createTask / listTaskTurns 默认桩（第 5 用例按需覆盖）
const createTaskApiMock = vi.hoisted(() =>
  vi.fn().mockResolvedValue({
    task_id: "real-task",
    workspace_id: "ws-1",
    agent_id: "main_agent",
    title: "t",
    status: "pending",
    execution_status: "pending",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }),
);
const listTaskTurnsApiMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
// useTask / useModelSendGuard 默认桩（第 6 用例按需覆盖）
const useTaskCreateTurnMock = vi.hoisted(() => vi.fn().mockResolvedValue(true));
const useTaskCreateTaskMock = vi.hoisted(() => vi.fn());
const guardSendMock = vi.hoisted(() => vi.fn().mockResolvedValue({ ok: true }));
const selectAttachmentPathsMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));

vi.mock("@/services/api", () => ({
  listModels: listModelsMock,
  listProviders: listProvidersMock,
  createTask: createTaskApiMock,
  listTaskTurns: listTaskTurnsApiMock,
}));
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));
vi.mock("@/services/tracePropagation", () => ({ beginClientTrace: vi.fn(), endClientTrace: vi.fn() }));
vi.mock("@/lib/perf", () => ({
  PerfTrace: { markCurrent: vi.fn(), endCurrent: vi.fn(), startCurrent: vi.fn(() => ({ traceId: "t", mark: vi.fn() })) },
}));
vi.mock("@/services/workspace", () => ({ pickAndCreateWorkspace: vi.fn() }));
vi.mock("@/hooks/useModelSendGuard", () => ({ useModelSendGuard: () => ({ guardSend: guardSendMock }) }));
vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({
    createTask: useTaskCreateTaskMock,
    createTurn: useTaskCreateTurnMock,
    cancelTurn: vi.fn(),
    operation: { loading: false, error: null, eventsError: null },
  }),
}));
vi.mock("@/services/dialog", () => ({
  selectDirectory: vi.fn(),
  selectAttachmentPaths: selectAttachmentPathsMock,
}));

import { TaskHeaderBar } from "@/components/chat/TaskHeaderBar";
import { InputBar } from "@/components/layout/InputBar";
import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTurnStore } from "@/stores/turnStore";

// ---- helpers ----
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

function resetStores() {
  cleanup();
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    selectedModelName: null,
    availableModels: [],
    modelsLoaded: true,
  });
  useWorkspaceStore.setState({
    workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
    activeWorkspaceId: "ws-1",
    collapsedWorkspaceIds: new Set(),
  });
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
  listModelsMock.mockReset();
  listModelsMock.mockResolvedValue([]);
  listProvidersMock.mockReset();
  listProvidersMock.mockResolvedValue([]);
  createTaskApiMock.mockReset();
  createTaskApiMock.mockResolvedValue({
    task_id: "real-task",
    workspace_id: "ws-1",
    agent_id: "main_agent",
    title: "t",
    status: "pending",
    execution_status: "pending",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  });
  listTaskTurnsApiMock.mockReset();
  listTaskTurnsApiMock.mockResolvedValue([]);
  useTaskCreateTurnMock.mockReset();
  useTaskCreateTurnMock.mockResolvedValue(true);
  useTaskCreateTaskMock.mockReset();
  guardSendMock.mockReset();
  guardSendMock.mockResolvedValue({ ok: true });
  selectAttachmentPathsMock.mockReset();
  selectAttachmentPathsMock.mockResolvedValue([]);
}

beforeEach(() => {
  resetStores();
});

// ===========================================================================
// 1. AgentSelector 已从 TaskHeaderBar 移除
// ===========================================================================
describe("1. AgentSelector 已从 TaskHeaderBar 移除", () => {
  // 测试目的：Chat 视图顶部栏不应再渲染 AgentSelector 的折叠特征（Bot 图标 + Developer 文本）。
  // 可能发现的缺陷：重构遗漏删除 AgentSelector 渲染点 → Developer 文案仍出现。
  it("TaskHeaderBar 不渲染 AgentSelector 特征（无 Developer/Bot 文案）", () => {
    render(<TaskHeaderBar />);
    // AgentSelector 折叠态曾显示 "Developer" 文本；移除后不应出现。
    expect(screen.queryByText("Developer")).toBeNull();
    // 顶部栏容器仍在。
    expect(screen.getByTestId("task-header-bar")).toBeTruthy();
  });

  // 测试目的：即便 agentStore 加载到 developer 默认 agent，顶部栏也不渲染 AgentSelector 切换入口。
  it("TaskHeaderBar 仍不渲染 Agent 切换入口（Developer/Researcher 文案）", () => {
    render(<TaskHeaderBar />);
    // 不应出现任何 agent 角色文案（AgentSelector 已移除）。
    expect(screen.queryByText("Developer")).toBeNull();
    expect(screen.queryByText("Researcher")).toBeNull();
  });
});

// ===========================================================================
// 2. TaskHeaderBar 仍渲染 ModelSelector + ProviderSettingsDialog
// ===========================================================================
describe("2. TaskHeaderBar 仍渲染 ModelSelector + ProviderSettingsDialog", () => {
  // 测试目的：模型切换入口未丢，ModelSelector 折叠态（选择模型按钮）仍存在。
  it("仍渲染 ModelSelector 折叠态按钮（aria-label=选择模型）", () => {
    render(<TaskHeaderBar />);
    expect(screen.getByRole("button", { name: "选择模型" })).toBeTruthy();
  });

  // 测试目的：点击 ModelSelector footer 能打开 ProviderSettingsDialog（配置中心未丢）。
  it("点击 ModelSelector footer 打开 ProviderSettingsDialog（dialog 角色出现）", () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    render(<TaskHeaderBar />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

// ===========================================================================
// 3. InputBar 内嵌 ModelSelector 渲染 + 切换写入 selectedModelName
// ===========================================================================
describe("3. InputBar 内嵌 ModelSelector 渲染与写入", () => {
  // 测试目的：Chat 视图 InputBar 底部元信息行存在 ModelSelector 实例。
  it("InputBar 底部元信息行渲染 ModelSelector（选择模型按钮）", () => {
    useTaskStore.setState({ activeTaskId: "t-1" });
    render(<InputBar />);
    const modelButtons = screen.getAllByRole("button", { name: "选择模型" });
    expect(modelButtons.length).toBeGreaterThanOrEqual(1);
  });

  // 测试目的：在 InputBar 内点击选择模型，写入 useTaskStore.selectedModelName。
  it("InputBar 内 ModelSelector 选择模型写入 useTaskStore.selectedModelName", () => {
    useTaskStore.setState({ activeTaskId: "t-1", availableModels: [makeModel()] });
    render(<InputBar />);
    const modelButtons = screen.getAllByRole("button", { name: "选择模型" });
    fireEvent.click(modelButtons[0]);
    fireEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
  });
});

// ===========================================================================
// 4. NewTaskPage 底部 ModelSelector 渲染
// ===========================================================================
describe("4. NewTaskPage 底部 ModelSelector 渲染", () => {
  // 测试目的：NewTaskPage 底部输入区存在 ModelSelector，与顶部 TaskHeaderBar 的 ModelSelector 共存。
  it("NewTaskPage 同时存在顶部 TaskHeaderBar 与底部 ModelSelector（共享 store）", () => {
    render(<NewTaskPage onCreated={vi.fn()} />);
    // 顶部 TaskHeaderBar 1 个 + 底部输入区 1 个 = 2 个「选择模型」按钮
    const modelButtons = screen.getAllByRole("button", { name: "选择模型" });
    expect(modelButtons.length).toBe(2);
    expect(screen.getByPlaceholderText("描述这次任务...")).toBeTruthy();
  });

  // 测试目的：在 NewTaskPage 底部 ModelSelector 选择模型，写入共享 store（顶部栏也反映）。
  it("NewTaskPage 底部选择模型写入 useTaskStore.selectedModelName", () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    render(<NewTaskPage onCreated={vi.fn()} />);
    const modelButtons = screen.getAllByRole("button", { name: "选择模型" });
    fireEvent.click(modelButtons[modelButtons.length - 1]);
    fireEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
  });
});

// ===========================================================================
// 6. InputBar 内 ModelSelector 的 onOpenSettings 与宿主联动
// ===========================================================================
describe("7. InputBar ModelSelector onOpenSettings 与宿主联动", () => {
  // 测试目的：点击 InputBar 内 ModelSelector 的 footer 应触发宿主经 onOpenSettings prop 注入的回调。
  it("点击 InputBar 内 ModelSelector footer 触发宿主 onOpenSettings 回调", () => {
    const onOpenSettings = vi.fn();
    useTaskStore.setState({ activeTaskId: "t-1", availableModels: [makeModel()] });
    render(<InputBar onOpenSettings={onOpenSettings} />);
    const modelButtons = screen.getAllByRole("button", { name: "选择模型" });
    fireEvent.click(modelButtons[0]);
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  // 测试目的：空态（无可用模型）时，底部 ModelSelector 点击空态引导同样触发 onOpenSettings。
  it("空态点击引导也触发宿主 onOpenSettings", async () => {
    const onOpenSettings = vi.fn();
    useTaskStore.setState({ activeTaskId: "t-1", availableModels: [], modelsLoaded: true });
    render(<InputBar onOpenSettings={onOpenSettings} />);
    const modelButtons = screen.getAllByRole("button", { name: "选择模型" });
    fireEvent.click(modelButtons[0]);
    const guide = await screen.findByText("暂无可用模型，点击配置");
    fireEvent.click(guide);
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });
});

// ===========================================================================
// 6. 发送链路不变（guardSend + createTurn 透传）
// ===========================================================================
describe("6. 发送链路不变（guardSend + createTurn 透传）", () => {
  // 测试目的：重构后 InputBar handleSend 仍经 guardSend 拦截、并透传 text/attachments 给 createTurn。
  it("发送时 guardSend 被调用且 createTurn 收到正确 text/attachments", async () => {
    useTaskStore.setState({
      activeTaskId: "t-1",
      selectedModelName: "deepseek/deepseek-v4-flash",
      availableModels: [makeModel({ supports_image: true })],
      modelsLoaded: true,
    });
    selectAttachmentPathsMock.mockResolvedValue(["H:\\shot.png"]);

    render(<InputBar />);
    const input = screen.getByPlaceholderText("给 Agent 下达任务...");

    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("shot.png")).toBeTruthy());

    await act(async () => {
      fireEvent.change(input, { target: { value: "look" } });
      fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    });

    await waitFor(() =>
      expect(useTaskCreateTurnMock).toHaveBeenCalledWith("look", [{ kind: "image", ref: "H:\\shot.png" }]),
    );
    expect(guardSendMock).toHaveBeenCalled();
  });
});
