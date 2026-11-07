import type { MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { TransportIssue } from "@/components/assistant/transport-status";
import type { BackendRuntimeSnapshot } from "@/lib/backend/runtime-config";

export type AssistantRuntimeProps = {
  taskId: number;
  workspaceId: number;
  workspaceRoot?: string;
  initialState: TransportState;
  /** 正在分叉的回复 id。 */
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onTaskStateChanged?: () => void;
};

export type RuntimeSessionContext = {
  taskId: number;
  workspaceId: number;
  workspaceRoot?: string;
  initialState: TransportState;
  /**
   * 当前后端实例的最新快照，由会话在每次渲染时写入。计时器与异步回调读取它判断
   * 后端是否仍可用；后端变化的边沿只由 useBackendTransitions 检测一次。
   */
  backendRef: MutableRefObject<BackendRuntimeSnapshot>;
  traceId: string;
  performanceProbe: AssistantPerformanceProbe | null;
  setIssue: (issue: TransportIssue | null) => void;
  onTaskStateChanged?: () => void;
  latestStateRef: MutableRefObject<TransportState>;
  /** 打开（或替换）Task 长订阅；runtime 挂载前为 null。 */
  attachTransportRef: MutableRefObject<(() => Promise<void>) | null>;
};
