import type { ThreadMessage } from "@assistant-ui/react";

import type {
  TransportError,
  TransportMessage,
  TransportRun,
  TransportState,
} from "@/lib/assistant/contract";
import {
  toPendingUserMessage,
  toSnapshotErrorMessage,
  toThreadMessageWithRenderContext,
  type UserAddMessageCommand,
} from "@/lib/assistant/converter";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";

export type TransportFrameEnvelope = {
  task_id: number;
  kind: "full" | "mutation" | "resync_required";
  mutations: Array<{
    kind: "set" | "append-text";
    path: Array<string | number>;
    value: unknown;
  }>;
  state?: TransportState;
  source_run_id?: number | null;
  current_run_id?: number | null;
  current_run_status?: string | null;
  resync_reason?: string | null;
  target_run_id: number;
  target_run_status?: string | null;
};

export type FrameStoreItem =
  | { kind: "canonical"; message: TransportMessage; context: MessageContext }
  | { kind: "pending"; command: UserAddMessageCommand }
  | { kind: "error"; error: TransportError };

type MessageContext = {
  runId: TransportRun["runId"];
  runStatus: TransportRun["status"];
  endReason: TransportRun["endReason"];
  error: TransportRun["error"];
  isLastRunMessage: boolean;
};

type StoreSnapshot = {
  state: TransportState;
  items: readonly FrameStoreItem[];
  isRunning: boolean;
  targetRunId: number | null;
  targetRunStatus: string | null;
  pendingCommands: readonly UserAddMessageCommand[];
  resyncRequired: boolean;
};

type Listener = () => void;

const TERMINAL_STATUSES = new Set(["idle", "completed", "failed", "cancelled", "interrupted"]);

/**
 * Validate and normalize the small SSE envelope before it reaches the projection store.
 * Snapshot validation remains the single contract validator for full state; this guard only
 * owns frame metadata and mutation shape so malformed transport data fails at the boundary.
 */
export function parseTransportFrame(value: unknown): TransportFrameEnvelope {
  if (typeof value !== "object" || value === null) {
    throw new Error("transport frame must be an object");
  }
  const frame = value as Record<string, unknown>;
  if (!Number.isInteger(frame.task_id)) throw new Error("transport frame task_id is invalid");
  if (frame.kind !== "full" && frame.kind !== "mutation" && frame.kind !== "resync_required") {
    throw new Error("transport frame kind is invalid");
  }
  const kind = frame.kind as TransportFrameEnvelope["kind"];
  if (!Array.isArray(frame.mutations)) throw new Error("transport frame mutations are invalid");
  const mutations = frame.mutations.map((value, index) => {
    if (typeof value !== "object" || value === null) {
      throw new Error(`transport frame mutation[${index}] is invalid`);
    }
    const mutation = value as Record<string, unknown>;
    if (mutation.kind !== "set" && mutation.kind !== "append-text") {
      throw new Error(`transport frame mutation[${index}].kind is invalid`);
    }
    if (!Array.isArray(mutation.path) || !mutation.path.every(
      (part) => (typeof part === "string") || (typeof part === "number" && Number.isInteger(part)),
    )) {
      throw new Error(`transport frame mutation[${index}].path is invalid`);
    }
    return {
      kind: mutation.kind as "set" | "append-text",
      path: mutation.path as Array<string | number>,
      value: mutation.value,
    };
  });
  if (!Number.isInteger(frame.target_run_id)) throw new Error("transport frame target_run_id is invalid");
  if (frame.target_run_status !== undefined
    && frame.target_run_status !== null
    && typeof frame.target_run_status !== "string") {
    throw new Error("transport frame target_run_status is invalid");
  }
  if (kind === "full" && frame.state === undefined) {
    throw new Error("full transport frame must include state");
  }
  return {
    task_id: frame.task_id as number,
    kind,
    mutations,
    state: frame.state === undefined ? undefined : parseTransportState(frame.state),
    source_run_id: parseOptionalInteger(frame.source_run_id, "source_run_id"),
    current_run_id: parseOptionalInteger(frame.current_run_id, "current_run_id"),
    current_run_status: parseOptionalString(frame.current_run_status, "current_run_status"),
    resync_reason: parseOptionalString(frame.resync_reason, "resync_reason"),
    target_run_id: frame.target_run_id as number,
    target_run_status: parseOptionalString(frame.target_run_status, "target_run_status"),
  };
}

function parseOptionalInteger(value: unknown, field: string): number | null | undefined {
  if (value !== undefined && value !== null && !Number.isInteger(value)) {
    throw new Error(`transport frame ${field} is invalid`);
  }
  return value as number | null | undefined;
}

function parseOptionalString(value: unknown, field: string): string | null | undefined {
  if (value !== undefined && value !== null && typeof value !== "string") {
    throw new Error(`transport frame ${field} is invalid`);
  }
  return value as string | null | undefined;
}

/**
 * Owns one task's canonical frame projection for assistant-ui.
 *
 * The store applies mutation paths immutably, cloning only the state ancestors on the path.
 * A changed message receives a new source object and a new external message array; untouched
 * message objects and their converted assistant-ui values remain reusable. It is a render store,
 * not a second persistence source and never adds ordering/version fields to the wire state.
 */
export class TransportFrameStore {
  private state: TransportState;
  private items: FrameStoreItem[] = [];
  private pendingCommands: readonly UserAddMessageCommand[] = [];
  private targetRunId: number | null;
  private targetRunStatus: string | null;
  private resyncRequired = false;
  private snapshot: StoreSnapshot;
  private readonly listeners = new Set<Listener>();
  private runItems: FrameStoreItem[][] = [];
  private canonicalItems: FrameStoreItem[] = [];
  private runRanges: Array<{ start: number; end: number }> = [];

  public constructor(initialState: TransportState) {
    this.state = initialState;
    this.targetRunId = initialState.current_run_id;
    this.targetRunStatus = findRun(initialState, this.targetRunId)?.status ?? null;
    this.rebuildAllItems();
    this.snapshot = this.makeSnapshot();
  }

  public subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  public getSnapshot = (): StoreSnapshot => this.snapshot;

  public applyFrame(frame: TransportFrameEnvelope): void {
    if (this.resyncRequired && frame.kind !== "full") return;
    if (frame.kind === "resync_required") {
      this.resyncRequired = true;
      this.targetRunStatus = frame.target_run_status ?? this.targetRunStatus;
      this.publish();
      return;
    }
    if (frame.kind === "full") {
      if (!frame.state) throw new Error("full transport frame must include state");
      this.state = frame.state;
      this.targetRunId = frame.target_run_id;
      this.targetRunStatus = frame.target_run_status
        ?? findRun(this.state, this.targetRunId)?.status
        ?? null;
      this.resyncRequired = false;
      this.pendingCommands = [];
      this.rebuildAllItems();
      this.publish();
      return;
    }

    const affectedRuns = new Set<number>();
    let rebuildAll = false;
    let structureChanged = false;
    for (const mutation of frame.mutations) {
      if (mutation.path.length === 0) {
        rebuildAll = true;
      }
      if (mutation.path[0] === "runs") {
        if (typeof mutation.path[1] === "number") {
          affectedRuns.add(mutation.path[1]);
          if (!this.runRanges[mutation.path[1]]) structureChanged = true;
        } else {
          rebuildAll = true;
        }
      }
      this.state = applyMutation(this.state, mutation);
    }
    this.targetRunId = frame.target_run_id;
    this.targetRunStatus = frame.target_run_status ?? this.targetRunStatus;
    if (rebuildAll || structureChanged) {
      this.rebuildAllItems();
    } else {
      const changedRuns = [...affectedRuns].sort((left, right) => right - left);
      if (changedRuns.length > 0) this.items = [...this.items];
      for (const runIndex of changedRuns) {
        const run = this.state.runs[runIndex];
        const range = this.runRanges[runIndex];
        if (!run || !range) continue;
        const nextItems = buildRunItems(run, this.runItems[runIndex]);
        this.runItems[runIndex] = nextItems;
        this.canonicalItems.splice(range.start, range.end - range.start, ...nextItems);
        this.items.splice(range.start, range.end - range.start, ...nextItems);
      }
      if (changedRuns.length > 0) this.rebuildRunRanges();
    }
    if (rebuildAll || frame.mutations.some((mutation) => mutation.path[0] === "error")) {
      this.composeItems();
    }
    this.publish();
  }

  public setPendingCommands(commands: readonly UserAddMessageCommand[]): void {
    this.pendingCommands = [...commands];
    this.composeItems();
    this.publish();
  }

  public clearPendingCommands(): void {
    if (this.pendingCommands.length === 0) return;
    this.pendingCommands = [];
    this.composeItems();
    this.publish();
  }

  public importState(state: TransportState): void {
    this.state = state;
    this.targetRunId = state.current_run_id;
    this.targetRunStatus = findRun(state, this.targetRunId)?.status ?? null;
    this.resyncRequired = false;
    this.rebuildAllItems();
    this.publish();
  }

  private rebuildAllItems(): void {
    this.runItems = this.state.runs.map((run) => buildRunItems(run));
    this.canonicalItems = [];
    for (const runItems of this.runItems) this.canonicalItems.push(...runItems);
    this.rebuildRunRanges();
    this.composeItems();
  }

  private composeItems(): void {
    const error: FrameStoreItem[] = this.state.error
      ? [{ kind: "error", error: this.state.error }]
      : [];
    const pending: FrameStoreItem[] = this.pendingCommands.map((command) => ({
      kind: "pending",
      command,
    }));
    this.items = [...this.canonicalItems, ...error, ...pending];
  }

  private rebuildRunRanges(): void {
    let start = 0;
    this.runRanges = this.runItems.map((runItems) => {
      const end = start + runItems.length;
      const range = { start, end };
      start = end;
      return range;
    });
  }

  private makeSnapshot(): StoreSnapshot {
    return {
      state: this.state,
      items: this.items,
      isRunning: this.pendingCommands.length > 0
        || (this.targetRunStatus !== null && !TERMINAL_STATUSES.has(this.targetRunStatus)),
      targetRunId: this.targetRunId,
      targetRunStatus: this.targetRunStatus,
      pendingCommands: this.pendingCommands,
      resyncRequired: this.resyncRequired,
    };
  }

  private publish(): void {
    this.snapshot = this.makeSnapshot();
    for (const listener of this.listeners) listener();
  }
}

export function convertFrameStoreItem(item: FrameStoreItem): ThreadMessage {
  if (item.kind === "canonical") {
    return toThreadMessageWithRenderContext(item.message, item.context);
  }
  if (item.kind === "pending") {
    const message = toPendingUserMessage(item.command);
    if (!message) throw new Error("pending add-message command has no renderable parts");
    return message;
  }
  return toSnapshotErrorMessage(item.error);
}

function buildRunItems(
  run: TransportRun,
  previous: readonly FrameStoreItem[] = [],
): FrameStoreItem[] {
  const lastAssistantMessageId = [...run.messages].reverse().find((message) => message.role === "assistant")?.id;
  const previousById = new Map(
    previous
      .filter((item): item is Extract<FrameStoreItem, { kind: "canonical" }> => item.kind === "canonical")
      .map((item) => [item.message.id, item]),
  );
  return run.messages.map((message) => {
    const context: MessageContext = {
      runId: run.runId,
      runStatus: run.status,
      endReason: run.endReason,
      error: run.error,
      isLastRunMessage: message.id === lastAssistantMessageId,
    };
    const previousItem = previousById.get(message.id);
    if (previousItem && sameContext(previousItem.context, context) && previousItem.message === message) {
      return previousItem;
    }
    return { kind: "canonical", message, context };
  });
}

function sameContext(left: MessageContext, right: MessageContext): boolean {
  return left.runId === right.runId
    && left.runStatus === right.runStatus
    && left.endReason === right.endReason
    && left.error === right.error
    && left.isLastRunMessage === right.isLastRunMessage;
}

function findRun(state: TransportState, runId: number | null): TransportRun | undefined {
  return runId === null ? undefined : state.runs.find((run) => run.runId === runId);
}

function applyMutation(
  state: TransportState,
  mutation: TransportFrameEnvelope["mutations"][number],
): TransportState {
  if (mutation.path.length === 0) {
    if (mutation.kind !== "set" || typeof mutation.value !== "object" || mutation.value === null) {
      throw new Error("root transport mutation must set an object");
    }
    return mutation.value as TransportState;
  }
  return updateAtPath(state, mutation.path, mutation.kind, mutation.value) as TransportState;
}

function updateAtPath(
  root: unknown,
  path: readonly (string | number)[],
  kind: "set" | "append-text",
  value: unknown,
): unknown {
  const [key, ...rest] = path;
  if (Array.isArray(root)) {
    const next = [...root];
    if (rest.length === 0) {
      if (kind === "append-text") throw new Error("cannot append text to an array");
      if (typeof key !== "number") throw new Error("array mutation requires numeric index");
      if (key === next.length) next.push(value);
      else next[key] = value;
      return next;
    }
    next[key as number] = updateAtPath(next[key as number], rest, kind, value);
    return next;
  }
  if (typeof root !== "object" || root === null) throw new Error("mutation path has no object parent");
  const next = { ...(root as Record<string, unknown>) };
  if (rest.length === 0) {
    if (kind === "append-text") {
      if (typeof next[key as string] !== "string" || typeof value !== "string") {
        throw new Error("append-text mutation requires string target and value");
      }
      next[key as string] += value;
    } else {
      next[key as string] = value;
    }
    return next;
  }
  next[key as string] = updateAtPath(next[key as string], rest, kind, value);
  return next;
}
