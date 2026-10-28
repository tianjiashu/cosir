import type { MutableRefObject } from "react";
import type { CreateAttachment } from "@assistant-ui/core";
import type { InitialConversationAttachment } from "@/components/new-conversation";

import type { TransportState } from "@/lib/assistant/contract";
import type { EditIntent } from "@/lib/assistant/converter";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type { TransportIssue } from "@/components/assistant/transport-status";
import type { BackendRuntimeSnapshot } from "@/src/runtime-config";

export type AssistantRuntimeProps = {
  taskId: number;
  workspaceId?: number | null;
  workspaceRoot?: string;
  initialState: TransportState;
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
  /** 正在 fork 的消息 id：fork 按消息切分，入口挂在带结局的回复上。 */
  forkingMessageId?: string | null;
  onForkMessage?: (messageId: string) => void;
  onTaskStateChanged?: () => void;
};

export type ComposerRestore = {
  restoreNewMessage: (text: string, attachments?: readonly CreateAttachment[]) => void;
  restoreEditMessage: (
    sourceId: string,
    intent: EditIntent,
    text: string,
    attachments?: readonly CreateAttachment[],
  ) => boolean;
};

export type RuntimeSessionContext = {
  taskId: number;
  workspaceId?: number | null;
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
  /** 用户已请求停止、当前活动尚未结束：Task 级标记，不比对 runId。 */
  cancelRequestedRef: MutableRefObject<boolean>;
  composerRestoreRef: MutableRefObject<ComposerRestore | null>;
};
