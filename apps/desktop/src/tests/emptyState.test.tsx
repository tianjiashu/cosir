// @vitest-environment happy-dom
/**
 * 空态展示测试（task 列表为空 / 无活跃任务）。
 *
 * 覆盖两处改动：
 *  - Sidebar：workspace 已加载、无 task、未折叠、未失败 → 显示「该工作区尚无任务」+
 *    「创建第一个任务」按钮（点击触发 onNewTask）。
 *  - ChatPanel：已有工作区但无活跃任务（emptySession 且 !noWorkspace）→ 显示 FolderOpen
 *    图标 + 文案「选择左侧任务开始对话，或直接在下方输入框开始新会话」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { WorkspaceRecord } from "@shared/workspace";

// ---- Sidebar 子组件 / 副作用依赖 mock ----
// HistoryList / PluginList 为占位组件，mock 掉避免无关渲染噪音。
vi.mock("@/components/sidebar/HistoryList", () => ({
  HistoryList: () => <div data-testid="history-list-stub" />,
}));
vi.mock("@/components/sidebar/PluginList", () => ({
  PluginList: () => <div data-testid="plugin-list-stub" />,
}));
// workspaceEventStore 的 startEvent/removeEvent 会触发 SSE 副作用，mock 成空实现。
// 注意：Sidebar 以 useWorkspaceEventStore((s) => s.startEvent) 的选择器形式调用，
// 故 mock 必须返回可被选择器解构的 hook（传入 selector 并回传其求值结果）。
const eventStoreFns = vi.hoisted(() => ({
  startEvent: vi.fn(),
  removeEvent: vi.fn(),
}));
vi.mock("@/stores/workspaceEventStore", () => ({
  useWorkspaceEventStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      statusByWorkspaceId: {},
      startEvent: eventStoreFns.startEvent,
      removeEvent: eventStoreFns.removeEvent,
    }),
}));
// api 含 deleteWorkspace/deleteTask 等网络调用，mock 成安全桩。
// 补充 listAgents / listModels / listProviders：ChatPanel 顶部内嵌的 TaskHeaderBar 会调用，
// 即便 TaskHeaderBar 本身被桩化为空 stub，防御性补齐避免 mock 链式失败。
vi.mock("@/services/api", () => ({
  deleteWorkspace: vi.fn(),
  deleteTask: vi.fn(),
  listWorkspaceTasks: vi.fn(),
  listAgents: vi.fn().mockResolvedValue({ agents: [], default_agent_id: "developer" }),
  listModels: vi.fn().mockResolvedValue([]),
  listProviders: vi.fn().mockResolvedValue([]),
}));
// logger 噪声屏蔽。
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

import { Sidebar } from "@/components/layout/Sidebar";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useEventStore } from "@/stores/eventStore";
import { SSEConnectionState } from "@/services/sse";

// ChatPanel 的 VirtualList 与 perf 走与现有 ChatPanel 测试一致的 mock，
// 使空态渲染路径聚焦在文案/图标上。
// ChatPanel 顶部内嵌 TaskHeaderBar（含 AgentSelector + ModelSelector + ProviderSettingsDialog）；
// 本测试关注空态文案/图标，不展开这些子组件细节，统一桩化为空 stub。
vi.mock("@/components/chat/TaskHeaderBar", () => ({
  TaskHeaderBar: () => null,
}));
vi.mock("@/components/layout/TurnTimeline", () => ({
  TurnTimeline: () => <div data-testid="turn-timeline-stub" />,
}));
vi.mock("@/lib/virtual/VirtualList", () => ({
  VirtualList: <T,>({
    items,
    getKey,
    renderItem,
    containerTestId,
  }: {
    items: T[];
    getKey: (item: T, index: number) => string;
    renderItem: (item: T, index: number) => unknown;
    containerTestId?: string;
  }) => (
    <div data-testid={containerTestId}>
      {items.map((item, index) => (
        <div key={getKey(item, index)}>{renderItem(item, index) as never}</div>
      ))}
    </div>
  ),
}));
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));

const WS_ID = "ws-empty";
const WS_NAME = "空工作区";

function makeWorkspace(): WorkspaceRecord {
  return {
    workspace_id: WS_ID,
    name: WS_NAME,
    root_path: "/tmp/empty",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as WorkspaceRecord;
}

/** 包裹 act 的 Sidebar 渲染，消除 useEffect 触发状态更新的 act 警告。 */
function renderSidebar(props: {
  activeView?: "chat" | "new-task" | "logs";
  onOpenLogs?: () => void;
  onOpenChat?: () => void;
  onNewTask?: () => void;
}) {
  const onOpenLogs = props.onOpenLogs ?? vi.fn();
  const onOpenChat = props.onOpenChat ?? vi.fn();
  const onNewTask = props.onNewTask ?? vi.fn();
  const activeView = props.activeView ?? "chat";
  return act(() =>
    render(
      <Sidebar activeView={activeView} onOpenLogs={onOpenLogs} onOpenChat={onOpenChat} onNewTask={onNewTask} />,
    ),
  );
}

describe("Sidebar 空态：无 task 的工作区", () => {
  beforeEach(() => {
    useWorkspaceStore.setState({
      workspaces: [makeWorkspace()],
      activeWorkspaceId: WS_ID,
      collapsedWorkspaceIds: new Set<string>(),
    });
    useTaskStore.setState({
      tasksById: {},
      tasksByWorkspaceId: { [WS_ID]: [] },
      loadedWorkspaceIds: new Set([WS_ID]),
      activeTaskId: null,
      activeTurnId: null,
      selectedAgentId: "developer",
    });
  });

  // 测试目的：验证 workspace 已加载、无 task、未折叠、未失败时，Sidebar 渲染空态文案。
  // 可能发现的缺陷：空态条件判断错误（漏判加载态/折叠态），导致错误展示「加载中」或任务列表。
  it("已加载且无 task 的工作区显示「该工作区尚无任务」", () => {
    renderSidebar({});

    expect(screen.getByText("该工作区尚无任务")).not.toBeNull();
    // 反向断言：不应出现加载态、不应出现任务项。
    expect(screen.queryByText("加载中…")).toBeNull();
    expect(screen.queryByText("加载失败，点击重试")).toBeNull();
  });

  // 测试目的：验证空态下存在「创建第一个任务」按钮，且该按钮触发 onNewTask 回调。
  // 可能发现的缺陷：按钮文字不符契约、onClick 未绑定 onNewTask、或按钮根本未渲染。
  it("空态下渲染「创建第一个任务」按钮并点击触发 onNewTask", () => {
    const onNewTask = vi.fn();
    renderSidebar({ onNewTask });

    const button = screen.getByRole("button", { name: /创建第一个任务/ });
    expect(button).not.toBeNull();

    fireEvent.click(button);
    expect(onNewTask).toHaveBeenCalledTimes(1);
  });

  // 测试目的：验证 collapsed（折叠）的 workspace 不展示任务列表区，故不显示空态文案/按钮。
  // 可能发现的缺陷：折叠态下仍渲染任务列表区（空态泄漏到折叠态），破坏折叠契约。
  it("折叠态工作区不渲染空态文案与创建按钮", () => {
    useWorkspaceStore.setState({ collapsedWorkspaceIds: new Set([WS_ID]) });
    renderSidebar({});

    expect(screen.queryByText("该工作区尚无任务")).toBeNull();
    expect(screen.queryByRole("button", { name: /创建第一个任务/ })).toBeNull();
  });

  // 测试目的：验证未加载（loadedWorkspaceIds 不含）的 workspace 展示「加载中…」而非空态，
  // 证明空态文案仅在已加载完成后出现（区分加载态与空态）。
  // 可能发现的缺陷：空态条件未优先判定加载态，未加载时误判为空任务列表。
  it("未加载的工作区显示「加载中…」而非空态文案", () => {
    useTaskStore.setState({ loadedWorkspaceIds: new Set<string>() });
    renderSidebar({});

    expect(screen.getByText("加载中…")).not.toBeNull();
    expect(screen.queryByText("该工作区尚无任务")).toBeNull();
  });
});

describe("ChatPanel 空态：无活跃任务（已有工作区）", () => {
  beforeEach(() => {
    useWorkspaceStore.setState({
      activeWorkspaceId: WS_ID,
      workspaces: [makeWorkspace()],
      collapsedWorkspaceIds: new Set<string>(),
    });
    // 无活跃任务：activeTaskId 为 null。
    useTaskStore.setState({
      tasksById: {},
      tasksByWorkspaceId: {},
      loadedWorkspaceIds: new Set(),
      activeTaskId: null,
      activeTurnId: null,
      selectedAgentId: "developer",
    });
    useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
    useEventStore.setState({
      events: [],
      eventsByTaskId: {},
      eventsByTurnId: {},
      connectionState: SSEConnectionState.IDLE,
      processedEventIds: new Set(),
    });
  });

  // 测试目的：验证已有 workspace 但无活跃任务时，ChatPanel 渲染空会话引导文案。
  // 可能发现的缺陷：空态文案缺失/拼写不符、或空态条件（emptySession && !noWorkspace）误判。
  it("无活跃任务时显示「选择左侧任务开始对话，或直接在下方输入框开始新会话」", () => {
    render(<ChatPanel onPickWorkspace={vi.fn()} />);

    expect(
      screen.getByText("选择左侧任务开始对话，或直接在下方输入框开始新会话"),
    ).not.toBeNull();
    // 反向断言：不应出现「无工作区」引导（noWorkspace 分支与 emptySession 分支互斥）。
    expect(screen.queryByTestId("workspace-guide")).toBeNull();
  });

  // 测试目的：验证空态渲染 FolderOpen 图标（lucide 图标渲染为 <svg>，role=img）。
  // 可能发现的缺陷：图标未渲染（如误用文本/漏渲染）、或图标类型与契约（FolderOpen）不符。
  it("空态渲染 FolderOpen 图标（svg / role=img）", () => {
    const { container } = render(<ChatPanel onPickWorkspace={vi.fn()} />);

    const svgs = container.querySelectorAll("svg");
    expect(svgs.length).toBeGreaterThan(0);
    // 文案区内的 svg 即 FolderOpen 图标。
    const textDiv = screen.getByText(
      "选择左侧任务开始对话，或直接在下方输入框开始新会话",
    );
    const icon = textDiv.previousElementSibling;
    expect(icon).not.toBeNull();
    expect(icon?.tagName.toLowerCase()).toBe("svg");
  });

  // 测试目的：正向对照——存在活跃任务时，ChatPanel 不渲染空态文案（验证空态分支条件正确）。
  // 可能发现的缺陷：无（对照用例应 PASS；若失败说明空态条件反向，活跃任务时仍显示空态）。
  it("对照：存在活跃任务时不渲染空态文案", () => {
    useTaskStore.setState({
      activeTaskId: "task-present",
      activeTurnId: "turn-present",
      tasksById: {
        "task-present": {
          task_id: "task-present",
          workspace_id: WS_ID,
          agent_id: "dev",
          input_text: "hello",
          title: "hello",
          last_message_preview: "",
          latest_turn_id: "turn-present",
          status: "running",
          execution_status: "running",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        } as never,
      },
      tasksByWorkspaceId: {
        [WS_ID]: [
          {
            task_id: "task-present",
            workspace_id: WS_ID,
            agent_id: "dev",
            input_text: "hello",
            title: "hello",
            last_message_preview: "",
            latest_turn_id: "turn-present",
            status: "running",
            execution_status: "running",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          } as never,
        ],
      },
      loadedWorkspaceIds: new Set([WS_ID]),
    });
    useTurnStore.setState({
      turnsByTaskId: {
        "task-present": [
          {
            turn_id: "turn-present",
            task_id: "task-present",
            input_text: "hello",
            status: "running",
            end_reason: null,
            response_text: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          } as never,
        ],
      },
      streamingTurnIds: {},
    });

    render(<ChatPanel onPickWorkspace={vi.fn()} />);
    expect(
      screen.queryByText("选择左侧任务开始对话，或直接在下方输入框开始新会话"),
    ).toBeNull();
  });
});
