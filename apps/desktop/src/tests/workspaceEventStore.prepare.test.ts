/**
 * workspaceEventStore 的「SSE 订阅与 prepare 并行触发」回归测试。
 *
 * 背景：Tauri WebView 中 workspace 事件流连接后若无数据推送（prepare 尚未触发），
 * ``await fetch`` 可能一直不 resolve。若把 ``prepareWorkspace`` 串行挂在
 * ``connectWorkspaceEventStream(...).then(...)``（或 onConnected 回调，仍在 fetch 之后）
 * 之后，prepare 会被延迟到连接结束才发出，前端卡在 preparing。
 *
 * 修复：``startEvent`` 让 SSE 订阅与 ``prepareWorkspace`` **并行**独立发起，二者互不依赖。
 * 本测试验证：即使 SSE 连接 promise 永不 resolve，``prepareWorkspace`` 也会立即触发。
 *
 * @module tests/workspaceEventStore.prepare
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkspaceEventStore } from "@/stores/workspaceEventStore";
import type { WorkspacePrepareResponse } from "@shared/workspace";
import type { WorkspaceEvent } from "@shared/workspaceEvent";

/**
 * vi.mock 工厂会被 hoist 到文件顶部，工厂内引用的变量必须用 vi.hoisted 声明，
 * 否则触发 "Cannot access before initialization"。
 */
/** connectWorkspaceEventStream 的签名（用于 mock 类型化，避免 vi.fn() 参数被推断为 never）。 */
type ConnectWorkspaceEventStream = (
  workspaceId: string,
  onEvent: (event: WorkspaceEvent) => void,
) => Promise<() => void>;

const { connectMock, prepareMock } = vi.hoisted(() => ({
  /** connectWorkspaceEventStream 的 mock。 */
  connectMock: vi.fn<ConnectWorkspaceEventStream>(),
  /** prepareWorkspace 的 mock。 */
  prepareMock: vi.fn<() => Promise<WorkspacePrepareResponse>>(),
}));

vi.mock("@/services/api", () => ({
  connectWorkspaceEventStream: connectMock,
  prepareWorkspace: prepareMock,
}));

/** 构造一条终态 workspace 状态事件。 */
function makeEvent(eventType: "workspace_ready" | "workspace_degraded"): WorkspaceEvent {
  return {
    event_id: `evt-${Date.now()}`,
    workspace_id: "ws-1",
    workspace_path: "/tmp/ws",
    event_type: eventType,
    created_at: new Date().toISOString(),
    payload: {},
  } as unknown as WorkspaceEvent;
}

/** 让 connectWorkspaceEventStream 返回一个永不 resolve 的 promise（模拟 Tauri 延迟场景）。 */
function mockConnectNeverResolves(): void {
  connectMock.mockImplementation(() => {
    // 返回永不 resolve 的 promise，模拟 SSE fetch 延迟 resolve（无数据推送时卡住）。
    return new Promise<() => void>(() => () => {});
  });
}

describe("workspaceEventStore.startEvent 并行触发 SSE 订阅与 prepare", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkspaceEventStore.setState({
      statusByWorkspaceId: {},
      inflightWorkspaceIds: new Set(),
      activeWorkspaceIds: new Set(),
      cleanupByWorkspaceId: {},
    });
  });

  it("即使 SSE 连接 promise 永不 resolve，prepareWorkspace 也会被立即调用", async () => {
    mockConnectNeverResolves();
    prepareMock.mockResolvedValue({
      ready: true,
      state: "ready",
      action_taken: "init",
      files_changed: 312,
      duration_ms: 1346,
      degraded_reason: null,
    } as WorkspacePrepareResponse);

    // 触发 startEvent
    useWorkspaceEventStore.getState().startEvent("ws-1");

    // 两个调用都被发起（并行，互不依赖）。
    expect(connectMock).toHaveBeenCalledTimes(1);
    // 关键：即使 connect 永不 resolve，prepare 也已同步发起。
    expect(prepareMock).toHaveBeenCalledTimes(1);
    expect(prepareMock).toHaveBeenCalledWith("ws-1");

    // 等 prepare 结果微任务落定 → 状态推进到 ready（自愈兜底）。
    await vi.waitFor(() => {
      expect(useWorkspaceEventStore.getState().statusByWorkspaceId["ws-1"]?.state).toBe("ready");
    });
  });

  it("收到 SSE 终态事件时经 onEvent 更新状态", async () => {
    // SSE 连接建立后立即（模拟异步）推送 ready 终态事件。
    connectMock.mockImplementation((_wsId: string, onEventCb: (e: WorkspaceEvent) => void) => {
      setTimeout(() => onEventCb(makeEvent("workspace_ready")), 0);
      return new Promise<() => void>(() => () => {});
    });

    useWorkspaceEventStore.getState().startEvent("ws-1");
    await vi.waitFor(() => {
      expect(useWorkspaceEventStore.getState().statusByWorkspaceId["ws-1"]?.state).toBe("ready");
    });
  });
});
