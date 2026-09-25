import type { MutableRefObject } from "react";
import type { CreateAttachment } from "@assistant-ui/core";
import type { InitialConversationAttachment } from "@/components/new-conversation";

import type { TransportState } from "@/lib/assistant/contract";
import type { TransportIssue } from "@/components/assistant/transport-status";

export type AssistantRuntimeProps = {
  taskId: number;
  workspaceId?: number | null;
  workspaceRoot?: string;
  initialState: TransportState;
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
  initialDisabledToolGroups?: string[];
  initialBanTools?: string[];
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onTaskStateChanged?: () => void;
  onRunStateChange?: (isRunning: boolean) => void;
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
  backendRuntimeGeneration: number;
  backendRuntimeAvailable: boolean;
  traceId: string;
  setIssue: (issue: TransportIssue | null) => void;
  onTaskStateChanged?: () => void;
  latestStateRef: MutableRefObject<TransportState>;
  runtimeControlsRef: MutableRefObject<RuntimeControls | null>;
  attachTransportRef: MutableRefObject<(() => Promise<void>) | null>;
  cancelRequestedRunIdRef: MutableRefObject<number | null>;
  lastTransportErrorRef: MutableRefObject<TransportIssue | null>;
  composerRestoreRef: MutableRefObject<ComposerRestore | null>;
};
