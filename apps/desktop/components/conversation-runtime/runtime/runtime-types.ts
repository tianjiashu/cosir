import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { SessionTransportConnection } from "@/lib/assistant/transport/session-transport-connection";
import type { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import type { TraceId } from "@/lib/logging/trace-id";

/** runtime 只在连接收到首帧后挂载：`store` 一定存在且已有完整状态。 */
export type AssistantRuntimeProps = {
  sessionId: number;
  workspaceId: number;
  workspaceRoot?: string;
  connection: SessionTransportConnection;
  store: TransportFrameStore;
  traceId: TraceId;
  performanceProbe: AssistantPerformanceProbe | null;
  /** 正在分叉的回复 id。 */
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
};

/**
 * 会话内各用例共用的事实：Session 身份、链路 id、连接与它的 store。当前状态一律读 `store.getSnapshot().state`
 * （canonical 的唯一副本），不另存镜像。
 */
export type RuntimeSessionContext = {
  sessionId: number;
  workspaceId: number;
  workspaceRoot?: string;
  traceId: TraceId;
  performanceProbe: AssistantPerformanceProbe | null;
  connection: SessionTransportConnection;
  store: TransportFrameStore;
};
