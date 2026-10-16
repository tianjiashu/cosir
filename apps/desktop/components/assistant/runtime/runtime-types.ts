import type { MutableRefObject } from "react";
import type { CreateAttachment } from "@assistant-ui/core";
import type { InitialConversationAttachment } from "@/components/new-conversation";

import type { TransportState } from "@/lib/assistant/contract";
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
  initialDisabledToolGroups?: string[];
  initialBanTools?: string[];
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onTaskStateChanged?: () => void;
};

export type ComposerRestore = {
  restoreNewMessage: (text: string, attachments?: readonly CreateAttachment[]) => void;
  restoreEditMessage: (sourceId: string, text: string, attachments?: readonly CreateAttachment[]) => boolean;
};

export type RuntimeControls = {
  resume: () => void;
  importState: (state: TransportState) => void;
};

export type RuntimeSessionContext = {
  taskId: number;
  workspaceId?: number | null;
  workspaceRoot?: string;
  initialState: TransportState;
  backendBaseUrl: string;
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
  runtimeControlsRef: MutableRefObject<RuntimeControls | null>;
  attachTransportRef: MutableRefObject<(() => Promise<void>) | null>;
  cancelRequestedRunIdRef: MutableRefObject<number | null>;
  lastTransportErrorRef: MutableRefObject<TransportIssue | null>;
  composerRestoreRef: MutableRefObject<ComposerRestore | null>;
};
