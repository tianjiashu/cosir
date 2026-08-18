// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { useEventStore } from "@/stores/eventStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { SSEConnectionState } from "@/services/sse";

vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));

// ChatPanel 顶部现内嵌 TaskHeaderBar（含 AgentSelector + ModelSelector + ProviderSettingsDialog）；
// 本测试关注 active task 实体缓存渲染路径，不展开这些子组件细节，统一桩化为空 stub，
// 避免触发 listAgents / listModels / listProviders 网络依赖。
// 防御性同时 mock @/services/api，屏蔽未来 TaskHeaderBar mock 失效时的链式失败。
vi.mock("@/components/chat/TaskHeaderBar", () => ({
  TaskHeaderBar: () => null,
}));
vi.mock("@/services/api", () => ({
  listAgents: vi.fn().mockResolvedValue({ agents: [], default_agent_id: "developer" }),
  listModels: vi.fn().mockResolvedValue([]),
  listProviders: vi.fn().mockResolvedValue([]),
}));
// logger 噪声屏蔽。
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
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
    renderItem: (item: T, index: number) => ReactNode;
    containerTestId?: string;
  }) => (
    <div data-testid={containerTestId}>
      {items.map((item, index) => (
        <div key={getKey(item, index)}>{renderItem(item, index)}</div>
      ))}
    </div>
  ),
}));

const TASK_ID = "task-entity-only";
const TURN_ID = "turn-entity-only";

function makeTask(): TaskRecord {
  return {
    task_id: TASK_ID,
    workspace_id: "ws-unloaded",
    agent_id: "dev",
    input_text: "visible user prompt",
    title: "visible user prompt",
    last_message_preview: "visible user prompt",
    latest_turn_id: TURN_ID,
    status: "running",
    execution_status: "running",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as TaskRecord;
}

function makeTurn(): TurnRecord {
  return {
    turn_id: TURN_ID,
    task_id: TASK_ID,
    input_text: "visible user prompt",
    status: "running",
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

describe("ChatPanel active task 实体缓存渲染", () => {
  beforeEach(() => {
    useWorkspaceStore.setState({
      activeWorkspaceId: "ws-unloaded",
      workspaces: [],
      collapsedWorkspaceIds: new Set(),
    });
    useTaskStore.setState({
      tasksById: { [TASK_ID]: makeTask() },
      tasksByWorkspaceId: {},
      loadedWorkspaceIds: new Set(),
      activeTaskId: TASK_ID,
      activeTurnId: TURN_ID,
      selectedAgentId: "developer",
    });
    useTurnStore.setState({
      turnsByTaskId: { [TASK_ID]: [makeTurn()] },
      streamingTurnIds: {},
    });
    useEventStore.setState({
      events: [],
      eventsByTaskId: {},
      eventsByTurnId: {},
      connectionState: SSEConnectionState.IDLE,
      processedEventIds: new Set(),
    });
  });

  it("workspace 分组未加载时仍显示当前 active task 的用户消息", () => {
    render(<ChatPanel onPickWorkspace={vi.fn()} />);

    expect(screen.queryByText("在下方输入框发送指令开始对话")).toBeNull();
    expect(screen.getByText("visible user prompt")).not.toBeNull();
  });
});
