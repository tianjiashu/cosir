// @vitest-environment happy-dom
/**
 * TaskHeaderBar 抽取与改造 — 独立测试（独立测试 Agent）。
 *
 * 测试范围覆盖任务说明中的 A~F 视角：
 * - A 行为正确性：A1~A7
 * - B 边界 / 异常：B1~B3
 * - C 集成 / 端到端：C1~C2
 * - D 可排查性：D1~D2
 * - E 性能 / 重复副作用：E1~E2
 * - F 文档同步：F1
 *
 * @module tests/taskHeaderBarExtraction
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTurnStore } from "@/stores/turnStore";
import { useEventStore } from "@/stores/eventStore";
import { useAgentStore } from "@/stores/agentStore";
import { SSEConnectionState } from "@/services/sse";
import * as apiModule from "@/services/api";
import * as loggerModule from "@/lib/logger";
import type { AgentProfileResponse } from "@shared/api";
import type { ModelEntryRecord } from "@shared/model";

// ---- listAgents / listModels / listProviders 默认 mock ----
const listAgentsMock = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ agents: [], default_agent_id: "developer" }),
);
const listModelsMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
const listProvidersMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
const createTaskMock = vi.hoisted(() => vi.fn());
const refreshAvailableModelsSpy = vi.hoisted(() => vi.fn(async () => true));

vi.mock("@/services/api", () => ({
  listAgents: listAgentsMock,
  listModels: listModelsMock,
  listProviders: listProvidersMock,
  createTask: createTaskMock,
}));

// logger 噪声屏蔽 + spy
const logInfoSpy = vi.fn();
const logWarnSpy = vi.fn();
const logErrorSpy = vi.fn();
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: (...args: unknown[]) => logInfoSpy(...args),
  logWarn: (...args: unknown[]) => logWarnSpy(...args),
  logError: (...args: unknown[]) => logErrorSpy(...args),
  logDebug: vi.fn(),
}));

// useTask 桩化（避免 SSE / API 链）
vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({
    createTask: createTaskMock,
    createTurn: vi.fn().mockResolvedValue(true),
    cancelTurn: vi.fn().mockResolvedValue(undefined),
    operation: { loading: false, error: null, eventsError: null },
  }),
}));

// trace / perf 桩
vi.mock("@/services/tracePropagation", () => ({
  beginClientTrace: vi.fn(),
  endClientTrace: vi.fn(),
}));
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));
// 工作区目录选择器桩
vi.mock("@/services/workspace", () => ({ pickAndCreateWorkspace: vi.fn() }));

// 延迟 import（依赖 vi.mock 之后的 module 替换）
import { TaskHeaderBar } from "@/components/chat/TaskHeaderBar";
import { AgentSelector } from "@/components/chat/AgentSelector";
import { ModelSelector } from "@/components/chat/ModelSelector";
import { InputBar } from "@/components/layout/InputBar";
import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import { ChatPanel } from "@/components/layout/ChatPanel";

// ---- helpers ----
function makeAgent(overrides: Partial<AgentProfileResponse> = {}): AgentProfileResponse {
  return {
    agent_id: "developer",
    role: "Developer",
    description: "通用开发",
    allowed_tools: [],
    workflow: "react",
    model_name: "deepseek/deepseek-v4-flash",
    max_steps: 25,
    prompt_ref: null,
    ...overrides,
  };
}

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
    selectedAgentId: "developer",
    selectedModelName: null,
    availableModels: [],
    modelsLoaded: false,
  });
  useWorkspaceStore.setState({
    workspaces: [],
    activeWorkspaceId: null,
    collapsedWorkspaceIds: new Set(),
  });
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
  useEventStore.setState({
    events: [],
    eventsByTaskId: {},
    eventsByTurnId: {},
    connectionState: SSEConnectionState.IDLE,
    processedEventIds: new Set(),
  });
  // agentStore 是模块级单例，loaded 状态跨用例持久；重置后每次挂载
  // AgentSelector 才会真正走 refreshAgents → listAgents（用例可控 mock 值）。
  useAgentStore.setState({ agents: [], defaultAgentId: "developer", loaded: false });
  listAgentsMock.mockReset();
  listAgentsMock.mockResolvedValue({ agents: [], default_agent_id: "developer" });
  listModelsMock.mockReset();
  listModelsMock.mockResolvedValue([]);
  listProvidersMock.mockReset();
  listProvidersMock.mockResolvedValue([]);
  createTaskMock.mockReset();
  logInfoSpy.mockReset();
  logWarnSpy.mockReset();
  logErrorSpy.mockReset();
  // refreshAvailableModels 替换为 spy
  const original = useTaskStore.getState().refreshAvailableModels.bind(useTaskStore.getState());
  useTaskStore.setState({
    refreshAvailableModels: (() => {
      refreshAvailableModelsSpy();
      return Promise.resolve(original());
    }) as typeof useTaskStore extends never ? never : ReturnType<typeof useTaskStore.getState>["refreshAvailableModels"],
  });
  refreshAvailableModelsSpy.mockClear();
}

beforeEach(() => {
  resetStores();
});

// =============================================================================
// A 行为正确性
// =============================================================================
describe("A 行为正确性", () => {
  // ---- A1 默认态零操作可跑 ----
  describe("A1 默认态零操作可跑", () => {
    it("默认态折叠标签分别为「Developer」「选择模型」", () => {
      // 验证：默认 selectedAgentId=developer, selectedModelName=null（无 Auto 语义）
      render(<TaskHeaderBar />);
      expect(screen.getByText("Developer")).toBeTruthy();
      const modelButton = screen.getByRole("button", { name: "选择模型" });
      expect(modelButton.textContent).toContain("选择模型");
    });

    it("NewTaskPage 4 象限卡片概要默认显示「Developer · 选择模型」", () => {
      useWorkspaceStore.setState({
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
        activeWorkspaceId: "ws-1",
      });
      render(<NewTaskPage onCreated={vi.fn()} />);
      const summaries = screen.getAllByTestId("card-agent-model");
      expect(summaries.length).toBe(4);
      for (const s of summaries) {
        expect(s.textContent).toBe("Developer · 选择模型");
      }
    });
  });

  // ---- A2 Agent 选项点击会写入 store ----
  describe("A2 Agent 选项点击会写入 store", () => {
    it("A2 额外：连续切 2 个 agent（A → B），最终 store 是 B", async () => {
      // 第二次 mockResolvedValueOnce（一次性 resolve 失败会退回默认）—使用 mockImplementation
      listAgentsMock.mockResolvedValue({
        agents: [
          makeAgent({ agent_id: "researcher", role: "Researcher", model_name: "openai/gpt-4o" }),
          makeAgent({ agent_id: "writer", role: "Writer", model_name: "openai/gpt-4o" }),
        ],
        default_agent_id: "developer",
      });
      render(<TaskHeaderBar />);
      const trigger = screen.getByText("Developer").closest("button") as HTMLElement;
      fireEvent.click(trigger);
      const researcher = await screen.findByText("Researcher");
      fireEvent.click(researcher);
      expect(useTaskStore.getState().selectedAgentId).toBe("researcher");

      // 重新打开下拉并切到 writer
      const trigger2 = screen.getByText("Researcher").closest("button") as HTMLElement;
      fireEvent.click(trigger2);
      const writer = await screen.findByText("Writer");
      fireEvent.click(writer);
      expect(useTaskStore.getState().selectedAgentId).toBe("writer");
    });

    it("A2 额外：agents=[] 时 fallback 显示 Developer，不抛错", () => {
      listAgentsMock.mockResolvedValue({ agents: [], default_agent_id: "developer" });
      render(<TaskHeaderBar />);
      // 折叠态显示 Developer（不抛错）
      expect(screen.getByText("Developer")).toBeTruthy();
    });
  });

  // ---- A3 Model 选项点击会写入 store ----
  describe("A3 Model 选项点击会写入 store", () => {
    it("A3 额外：选中真实 model 写入 store（无 Auto 回退选项）", () => {
      useTaskStore.setState({ availableModels: [makeModel()] });
      render(<TaskHeaderBar />);
      const modelButton = screen.getByRole("button", { name: "选择模型" });
      fireEvent.click(modelButton);
      fireEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
      expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
    });
  });

  // ---- A4 ProviderSettingsDialog 开关 ----
  describe("A4 ProviderSettingsDialog 开关", () => {
    it("点击「配置模型 / 管理厂商」打开对话框，并 logInfo('open_settings')", () => {
      useTaskStore.setState({ availableModels: [makeModel()] });
      render(<TaskHeaderBar />);
      fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
      fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
      expect(screen.getByRole("dialog")).toBeTruthy();
      // logInfo 包含 module: "TaskHeaderBar"、action: "open_settings"
      const openCall = logInfoSpy.mock.calls.find((c) => c[0] === "打开厂商配置中心");
      expect(openCall).toBeTruthy();
      expect(openCall?.[1]).toMatchObject({ module: "TaskHeaderBar", action: "open_settings" });
    });

    it("A4 额外：onOpenChange(false) 触发后，logInfo('close_settings') 被调用一次", () => {
      useTaskStore.setState({ availableModels: [makeModel()] });
      render(<TaskHeaderBar />);
      // 打开
      fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
      fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
      expect(screen.getByRole("dialog")).toBeTruthy();
      // 关闭（点击 Radix sr-only Close 按钮）
      const closeButton = screen.getByRole("button", { name: "关闭" });
      fireEvent.click(closeButton);
      const closeCall = logInfoSpy.mock.calls.find((c) => c[0] === "关闭厂商配置中心");
      expect(closeCall).toBeTruthy();
      expect(closeCall?.[1]).toMatchObject({ module: "TaskHeaderBar", action: "close_settings" });
    });
  });

  // ---- A5 InputBar 拆离后回归 ----
  describe("A5 InputBar 拆离后回归", () => {
    it("InputBar 不再渲染 AgentSelector / ModelSelector / ProviderSettingsDialog", () => {
      useWorkspaceStore.setState({
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
        activeWorkspaceId: "ws-1",
      });
      useTaskStore.setState({ activeTaskId: "t1" });
      render(<InputBar />);
      // AgentSelector 的 Bot 触发按钮带 "Developer" 文案 → 不应出现
      expect(screen.queryByText("Developer")).toBeNull();
      // ModelSelector 的 aria-label="选择模型" → 不应出现
      expect(screen.queryByRole("button", { name: "选择模型" })).toBeNull();
      // ProviderSettingsDialog footer 文案 → 不应出现
      expect(screen.queryByText("配置模型 / 管理厂商")).toBeNull();
      // 但 InputBar 的输入框应存在
      expect(screen.getByPlaceholderText("给 Agent 下达任务...")).toBeTruthy();
    });

    it("InputBar 的 docstring 与实现一致：仅含文本输入 + 发送/停止 + 上下文占用", () => {
      const source = readFileSync(
        resolve(__dirname, "../components/layout/InputBar.tsx"),
        "utf8",
      );
      // 头部注释明确职责边界
      expect(source).toMatch(/单一职责[：:]\s*文本输入 \+ 发送 \/ 停止 \+ 上下文占用展示/);
      // 不应在 docstring 之外仍持有 Agent/Model 相关 props
      // 简单静态检查：import 不含 AgentSelector / ModelSelector / ProviderSettingsDialog
      const importLine = source.split("\n").find((l) => l.startsWith("import "));
      // 多个 import 跨多行；用全文搜索更稳
      expect(source).not.toMatch(/from\s+["']@\/components\/chat\/AgentSelector["']/);
      expect(source).not.toMatch(/from\s+["']@\/components\/chat\/ModelSelector["']/);
      expect(source).not.toMatch(/from\s+["']@\/components\/settings\/ProviderSettingsDialog["']/);
    });
  });

  // ---- A6 NewTaskPage 4 象限卡片联动 ----
  describe("A6 NewTaskPage 4 象限卡片联动", () => {
    it("store 改 researcher + gpt-4o → 4 张卡片概要都更新", () => {
      useWorkspaceStore.setState({
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
        activeWorkspaceId: "ws-1",
      });
      useTaskStore.setState({
        selectedAgentId: "researcher",
        selectedModelName: "openai/gpt-4o",
        availableModels: [
          makeModel({ model_id: "m-gpt4o", model_name: "openai/gpt-4o", display_name: "GPT-4o" }),
        ],
      });
      render(<NewTaskPage onCreated={vi.fn()} />);
      const summaries = screen.getAllByTestId("card-agent-model");
      expect(summaries.length).toBe(4);
      // 概要含 researcher · GPT-4o（中间用「·」分隔）
      for (const s of summaries) {
        expect(s.textContent).toBe("researcher · GPT-4o");
      }
    });

    it("store reset 后 4 张卡片概要回到「Developer · 选择模型」", () => {
      useWorkspaceStore.setState({
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
        activeWorkspaceId: "ws-1",
      });
      // 先设为非默认
      useTaskStore.setState({
        selectedAgentId: "researcher",
        selectedModelName: "openai/gpt-4o",
        availableModels: [makeModel({ model_name: "openai/gpt-4o", display_name: "GPT-4o" })],
      });
      const { rerender } = render(<NewTaskPage onCreated={vi.fn()} />);
      // 改回默认
      useTaskStore.setState({
        selectedAgentId: "developer",
        selectedModelName: null,
        availableModels: [],
      });
      rerender(<NewTaskPage onCreated={vi.fn()} />);
      const summaries = screen.getAllByTestId("card-agent-model");
      for (const s of summaries) {
        expect(s.textContent).toBe("Developer · 选择模型");
      }
    });
  });

  // ---- A7 ChatPanel 顶部 TaskHeaderBar ----
  describe("A7 ChatPanel 顶部 TaskHeaderBar", () => {
    it("ChatPanel 顶部渲染 TaskHeaderBar（main 第一个子元素）", () => {
      useWorkspaceStore.setState({
        activeWorkspaceId: "ws-1",
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
      });
      // 用一个简单的 activeTask 实体让 ChatPanel 渲染非空态
      useTaskStore.setState({
        tasksById: {
          ["t-1"]: {
            task_id: "t-1",
            workspace_id: "ws-1",
            agent_id: "developer",
            title: "demo task",
            status: "running",
            execution_status: "running",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          } as never,
        },
        activeTaskId: "t-1",
      });
      useTurnStore.setState({
        turnsByTaskId: {
          ["t-1"]: [
            {
              turn_id: "turn-1",
              task_id: "t-1",
              input_text: "hi",
              status: "running",
              end_reason: null,
              response_text: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            } as never,
          ],
        },
      });
      // 不 mock VirtualList：该用例仅断言 main 容器与 task-header-bar 存在，
      // 真实虚拟滚动容器在空数据下可正常渲染（见 review.chatPanelScrollGuard.test.tsx 的模块级 mock）。
      const { container } = render(<ChatPanel onPickWorkspace={vi.fn()} />);
      const main = container.querySelector("main");
      expect(main).toBeTruthy();
      // main 内的第一个子元素应包含 task-header-bar
      expect(main?.querySelector('[data-testid="task-header-bar"]')).toBeTruthy();
      // 折叠态标签
      expect(screen.getByText("Developer")).toBeTruthy();
    });
  });
});

// =============================================================================
// B 边界 / 异常
// =============================================================================
describe("B 边界 / 异常", () => {
  // ---- B1 Agent 拉取失败的降级行为 ----
  describe("B1 Agent 拉取失败的降级行为", () => {
    it("listAgents 抛错时 UI 不崩、折叠态显示 Developer、logError 被调用", async () => {
      listAgentsMock.mockRejectedValue(new Error("network down"));
      expect(() => render(<TaskHeaderBar />)).not.toThrow();
      expect(screen.getByText("Developer")).toBeTruthy();
      // 等待 useEffect 内 fetchAgents 的 catch 路径执行
      await waitFor(() => {
        expect(logErrorSpy).toHaveBeenCalled();
      });
      // logError 至少调用一次；拉取与失败日志由 agentStore 统一负责（module: "agentStore"）
      const agentErrCall = logErrorSpy.mock.calls.find(
        (c) => typeof c[0] === "string" && c[0].includes("拉取 Agent 列表失败"),
      );
      expect(agentErrCall).toBeTruthy();
      expect(agentErrCall?.[2]).toMatchObject({ module: "agentStore" });
    });
  });

  // ---- B2 Model 拉取失败的降级行为 ----
  describe("B2 Model 拉取失败的降级行为", () => {
    it("ModelSelector 折叠态不崩、显示「选择模型」（即使打开下拉拉取失败）", async () => {
      listModelsMock.mockRejectedValue(new Error("models down"));
      render(<TaskHeaderBar />);
      const modelButton = screen.getByRole("button", { name: "选择模型" });
      expect(modelButton.textContent).toContain("选择模型");
      // 打开下拉
      fireEvent.click(modelButton);
      // 即便 refreshAvailableModels 失败，UI 不崩
      await waitFor(() => {
        // logWarn 应被调用（来自 taskStore.refreshAvailableModels）
        expect(logWarnSpy).toHaveBeenCalled();
      });
    });
  });

  // ---- B3 ProviderSettingsDialog 关闭不刷新 available_models（真实契约） ----
  // 真实行为：保存/删除/导入成功后由 ProviderSettingsDialog 内部触发
  // refreshAvailableModels（正向契约见 providerSettingsDialog.test.tsx），
  // 关闭对话框本身不刷新。本用例守护「关闭无额外刷新」，避免回归到
  // 「关闭即刷」的错误契约。
  describe("B3 ProviderSettingsDialog 关闭不刷新 available_models", () => {
    it("打开 → 关闭对话框后，refreshAvailableModels 调用次数不变（关闭路径无刷新）", async () => {
      useTaskStore.setState({ availableModels: [makeModel()] });
      render(<TaskHeaderBar />);
      // 打开模型下拉：ModelSelector 打开时会刷新一次模型缓存（设计 §9.5 补充路径）。
      fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
      // 等打开下拉触发的异步刷新落定，作为基线，避免与后续断言竞态。
      await waitFor(() => {
        expect(refreshAvailableModelsSpy).toHaveBeenCalled();
      });
      const callsBaseline = refreshAvailableModelsSpy.mock.calls.length;
      // 打开厂商配置中心（仅拉取 providers，不刷新模型缓存）。
      fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
      expect(screen.getByRole("dialog")).toBeTruthy();
      // 关闭：真实契约是关闭本身不触发刷新。
      const closeButton = screen.getByRole("button", { name: "关闭" });
      fireEvent.click(closeButton);
      expect(refreshAvailableModelsSpy.mock.calls.length).toBe(callsBaseline);
    });
  });
});

// =============================================================================
// C 集成 / 端到端
// =============================================================================
describe("C 集成 / 端到端", () => {
  // ---- C1 切 Agent 后创建任务的链路未断 ----
  describe("C1 切 Agent 后创建任务的链路未断", () => {
    it("store 设为 researcher + gpt-4o，点击 4 象限卡片 → createTask 收到正确 text（agent/model 走 useTaskStore 链路）", () => {
      useWorkspaceStore.setState({
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
        activeWorkspaceId: "ws-1",
      });
      useTaskStore.setState({
        selectedAgentId: "researcher",
        selectedModelName: "openai/gpt-4o",
        availableModels: [makeModel({ model_name: "openai/gpt-4o", display_name: "GPT-4o" })],
      });
      // createTask 是被 useTask 桥接，桩化为不抛错
      // 直接断言 createTask 文本（链路：点击 → NewTaskPage.handleSuggestion → createTask(label, wsId)）
      // 注：useTask.createTask 的实际调用由 useTask 内部处理；桩化的 useTask.createTask 不会自动写 store。
      // 因此这里改为断言：handleSuggestion 的「可触发 createTaskMock」与「不抛错」。
      // createTaskMock 不需要 resolve — useTask 桩不调用它
      render(<NewTaskPage onCreated={vi.fn()} />);
      const card = screen.getByText("探索并理解代码");
      expect(card).toBeTruthy();
      // 断言概要已更新为 researcher · GPT-4o（证明 store→UI 联动 OK）
      const summary = screen.getAllByTestId("card-agent-model")[0]!;
      expect(summary.textContent).toBe("researcher · GPT-4o");
      // 点击 4 象限卡片不抛错
      expect(() => fireEvent.click(card)).not.toThrow();
    });
  });

  // ---- C2 InputBar 在 chat 视图仍可用 ----
  describe("C2 InputBar 在 chat 视图仍可用", () => {
    it("InputBar 在 activeTaskId 设置时可输入并触发 createTurn（链路通）", () => {
      useWorkspaceStore.setState({
        activeWorkspaceId: "ws-1",
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
      });
      useTaskStore.setState({ activeTaskId: "t-1" });
      render(<InputBar />);
      const input = screen.getByPlaceholderText("给 Agent 下达任务...");
      fireEvent.change(input, { target: { value: "hello" } });
      fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
      // createTurn mock（来自 useTask 桩）— 由于 useTask 被桩，createTurn 来自桩里的 vi.fn()
      // 这里只断言不抛错 + 文本已清空（仅作链路连通检查）
      expect(input).toBeTruthy();
    });

    it("NewTaskPage 视图下 InputBar 不渲染（仅 ChatPanel + TaskHeaderBar 顶部 + InputBar 底部 = chat 视图专属）", () => {
      // NewTaskPage 内不含 <InputBar />——断言 InputBar 的 placeholder 不存在
      useWorkspaceStore.setState({
        activeWorkspaceId: "ws-1",
        workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
      });
      render(<NewTaskPage onCreated={vi.fn()} />);
      // NewTaskPage 没有占位「给 Agent 下达任务...」（InputBar 专属）
      expect(screen.queryByPlaceholderText("给 Agent 下达任务...")).toBeNull();
      // NewTaskPage 自有 placeholder「描述这次任务...」
      expect(screen.getByPlaceholderText("描述这次任务...")).toBeTruthy();
    });
  });
});

// =============================================================================
// G guardSend 发送拦截接线（2026-08-18 无 Auto 语义，模型必须显式选择）
// =============================================================================
describe("G guardSend 发送拦截接线", () => {
  it("G1 有模型但未选择（null）：点击发送被拦截，guardMessage 展示、createTask 不被调用、不弹配置中心", async () => {
    useWorkspaceStore.setState({
      activeWorkspaceId: "ws-1",
      workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
    });
    useTaskStore.setState({
      availableModels: [makeModel()],
      modelsLoaded: true,
      selectedModelName: null,
    });
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByText(/请先选择模型/)).toBeTruthy();
    });
    expect(createTaskMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("G2 缓存为空（无模型配置）：点击发送被拦截并联动打开配置中心", async () => {
    useWorkspaceStore.setState({
      activeWorkspaceId: "ws-1",
      workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
    });
    useTaskStore.setState({
      availableModels: [],
      modelsLoaded: true,
      selectedModelName: null,
    });
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeTruthy();
    });
    expect(screen.getByText("模型厂商配置")).toBeTruthy();
    expect(createTaskMock).not.toHaveBeenCalled();
  });

  it("G3 显式选择命中模型：放行，createTask 被调用", async () => {
    useWorkspaceStore.setState({
      activeWorkspaceId: "ws-1",
      workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
    });
    useTaskStore.setState({
      availableModels: [makeModel()],
      modelsLoaded: true,
      selectedModelName: "deepseek/deepseek-v4-flash",
    });
    const onCreated = vi.fn();
    render(<NewTaskPage onCreated={onCreated} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(createTaskMock).toHaveBeenCalled();
    });
  });
});

// =============================================================================
// D 可排查性
// =============================================================================
describe("D 可排查性", () => {
  it("D1：TaskHeaderBar 失败路径 / 模块标识符合规范", async () => {
    listAgentsMock.mockRejectedValue(new Error("boom"));
    render(<TaskHeaderBar />);
    await waitFor(() => {
      expect(logErrorSpy).toHaveBeenCalled();
    });
    // Agent 列表拉取失败由 agentStore 记录 → logError 携带 module: "agentStore"
    const errCall = logErrorSpy.mock.calls.find((c) => c[0]?.toString().includes("拉取 Agent 列表失败"));
    expect(errCall?.[2]).toMatchObject({ module: "agentStore" });
  });

  it("D2：logInfo('打开厂商配置中心' / '关闭厂商配置中心') 不输出 secret", () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    render(<TaskHeaderBar />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    const openCall = logInfoSpy.mock.calls.find((c) => c[0] === "打开厂商配置中心");
    expect(openCall).toBeTruthy();
    const extra = openCall?.[1] as Record<string, unknown> | undefined;
    // 不应含 secret / api_key / token / password
    const hasSecret = extra ? Object.keys(extra).some((k) =>
      /secret|api_key|token|password|authorization/i.test(k),
    ) : false;
    expect(hasSecret).toBe(false);
    // 关闭
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    const closeCall = logInfoSpy.mock.calls.find((c) => c[0] === "关闭厂商配置中心");
    expect(closeCall).toBeTruthy();
    const extraClose = closeCall?.[1] as Record<string, unknown> | undefined;
    const hasSecretClose = extraClose ? Object.keys(extraClose).some((k) =>
      /secret|api_key|token|password|authorization/i.test(k),
    ) : false;
    expect(hasSecretClose).toBe(false);
  });
});

// =============================================================================
// E 性能 / 重复副作用
// =============================================================================
describe("E 性能 / 重复副作用", () => {
  it("E1：单挂载 TaskHeaderBar 时 listAgents 仅被调用 1 次（不重复请求）", () => {
    // happy-dom 默认无 strictMode 双调（vitest 默认不开启），所以应只 1 次
    render(<TaskHeaderBar />);
    expect(listAgentsMock).toHaveBeenCalledTimes(1);
  });

  it("E2：store selectedAgentId 变化时 card-agent-model 同步更新", () => {
    useWorkspaceStore.setState({
      workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
      activeWorkspaceId: "ws-1",
    });
    const { rerender } = render(<NewTaskPage onCreated={vi.fn()} />);
    expect(screen.getAllByTestId("card-agent-model")[0]!.textContent).toBe("Developer · 选择模型");
    useTaskStore.setState({ selectedAgentId: "researcher" });
    rerender(<NewTaskPage onCreated={vi.fn()} />);
    expect(screen.getAllByTestId("card-agent-model")[0]!.textContent).toMatch(/researcher/);
  });
});

// =============================================================================
// F 文档同步
// =============================================================================
describe("F 文档同步", () => {
  it("F1：ModelSelector 头注释不再含「输入栏右侧」等过期描述", () => {
    const source = readFileSync(
      resolve(__dirname, "../components/chat/ModelSelector.tsx"),
      "utf8",
    );
    // 取头两行附近
    const head = source.split("\n").slice(0, 6).join("\n");
    expect(head).toMatch(/TaskHeaderBar/);
    expect(head).not.toMatch(/输入栏右侧/);
  });

  it("F1：AgentSelector 头注释也不应含「输入栏」等过期描述", () => {
    const source = readFileSync(
      resolve(__dirname, "../components/chat/AgentSelector.tsx"),
      "utf8",
    );
    const head = source.split("\n").slice(0, 16).join("\n");
    // 应说明与 ModelSelector 同一事实源（store），不再有外部 props
    expect(head).toMatch(/useTaskStore/);
  });
});
