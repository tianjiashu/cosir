import type { MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { TransportIssue } from "@/components/assistant/transport-status";

export type AssistantRuntimeProps = {
  taskId: number;
  workspaceId?: number | null;
  initialState: TransportState;
  initialMessage?: string;
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onTaskStateChanged?: () => void;
  onRunStateChange?: (isRunning: boolean) => void;
};

export type ComposerRestore = {
  restoreNewMessage: (text: string) => void;
  restoreEditMessage: (sourceId: string, text: string) => boolean;
};

export type RuntimeControls = {
  resume: () => void;
  importState: (state: TransportState) => void;
};

export type RuntimeSessionContext = {
  taskId: number;
  workspaceId?: number | null;
  initialState: TransportState;
  backendBaseUrl: string;
  traceId: string;
  setIssue: (issue: TransportIssue | null) => void;
  onTaskStateChanged?: () => void;
  latestStateRef: MutableRefObject<TransportState>;
  runtimeControlsRef: MutableRefObject<RuntimeControls | null>;
  cancelRequestedRunIdRef: MutableRefObject<number | null>;
  lastTransportErrorRef: MutableRefObject<TransportIssue | null>;
  composerRestoreRef: MutableRefObject<ComposerRestore | null>;
};
