import {
  InMemoryThreadListAdapter,
  useExternalStoreRuntime,
  useRemoteThreadListRuntime,
  type AppendMessage,
  type ExternalStoreAdapter,
  type RemoteThreadListAdapter,
} from "@assistant-ui/react";
import type { ReadonlyJSONValue } from "assistant-stream/utils";
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore, type MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import {
  convertFrameStoreItem,
  parseTransportFrame,
  TransportFrameStore,
  type FrameStoreItem,
} from "@/lib/assistant/transport-frame-store";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";

export type TaskAssistantTransportOptions = {
  initialState: TransportState;
  adapters?: ExternalStoreAdapter<FrameStoreItem>["adapters"];
  api: string;
  resumeApi: string;
  headers: () => Promise<Record<string, string>> | Record<string, string>;
  body: () => Promise<Record<string, unknown>> | Record<string, unknown>;
  prepareSendCommandsRequest?: (body: {
    commands: readonly unknown[];
    state: TransportState;
    config?: unknown;
    parentId?: string | null;
  }) => Promise<Record<string, unknown>> | Record<string, unknown>;
  onResponse?: (response: Response) => void;
  onFinish?: (status: { targetRunId: number | null; targetRunStatus: string | null }) => void;
  onError?: (
    error: Error,
    params: { commands: readonly unknown[]; updateState: (updater: (state: TransportState) => TransportState) => void },
  ) => Promise<void>;
  onCancel?: (params: { error?: Error; commands: readonly unknown[] }) => void;
  attachRef?: MutableRefObject<(() => Promise<void>) | null>;
  readonly?: boolean;
  onStateCommit?: (state: TransportState) => void;
};

type StreamCallbacks = {
  commands: readonly unknown[];
  controller: AbortController;
};

/**
 * Build the assistant-ui runtime on one external frame store.
 *
 * The store is the only stream projection owner. This hook owns request/connection lifetime;
 * a superseded AbortController and object identity check isolate an old SSE connection from a
 * newer attach/send without introducing protocol version fields.
 */
export function useTaskAssistantTransportRuntime(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const store = useMemo(() => new TransportFrameStore(options.initialState), [taskId]);
  const view = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const activeStreamRef = useRef<StreamCallbacks | null>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const closeActiveStream = useCallback(() => {
    activeStreamRef.current?.controller.abort();
    activeStreamRef.current = null;
  }, []);

  const readSse = useCallback(async (response: Response, callbacks: StreamCallbacks) => {
    if (!response.body) throw new Error("Assistant frame SSE response has no body");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const records = buffer.split("\n\n");
      buffer = records.pop() ?? "";
      for (const record of records) {
        const data = record
          .split("\n")
          .find((line) => line.startsWith("data:"))
          ?.slice("data:".length)
          .trim();
        if (!data || data === "[DONE]" || activeStreamRef.current !== callbacks) continue;
        store.applyFrame(parseTransportFrame(JSON.parse(data)));
      }
    }
    buffer += decoder.decode();
    if (buffer.trim() && activeStreamRef.current === callbacks) {
      const data = buffer.split("\n").find((line) => line.startsWith("data:"))?.slice(5).trim();
      if (data && data !== "[DONE]") store.applyFrame(parseTransportFrame(JSON.parse(data)));
    }
  }, [store]);

  const openStream = useCallback(async (
    url: string,
    requestBody: Record<string, unknown>,
    commands: readonly unknown[],
  ) => {
    closeActiveStream();
    const controller = new AbortController();
    const callbacks: StreamCallbacks = { commands, controller };
    activeStreamRef.current = callbacks;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: await optionsRef.current.headers(),
        body: JSON.stringify(requestBody),
        signal: controller.signal,
      });
      if (!response.ok) {
        const body = await response.text();
        // Preserve the existing structured HTTP error boundary used by runtime recovery.
        // The body is parsed by parseTransportError and only its allow-listed message reaches UI.
        throw new Error(`Status ${response.status}: ${body}`);
      }
      if (activeStreamRef.current !== callbacks) return;
      optionsRef.current.onResponse?.(response);
      await readSse(response, callbacks);
      if (activeStreamRef.current === callbacks) {
        activeStreamRef.current = null;
        const snapshot = store.getSnapshot();
        optionsRef.current.onFinish?.({
          targetRunId: snapshot.targetRunId,
          targetRunStatus: snapshot.targetRunStatus,
        });
      }
    } catch (error) {
      if (controller.signal.aborted || activeStreamRef.current !== callbacks) return;
      activeStreamRef.current = null;
      const normalized = error instanceof Error ? error : new Error(String(error));
      await optionsRef.current.onError?.(normalized, {
        commands,
        updateState: (updater) => store.importState(updater(store.getSnapshot().state)),
      });
    }
  }, [closeActiveStream, readSse, store]);

  const sendCommands = useCallback(async (commands: readonly unknown[]) => {
    const body = await optionsRef.current.body();
    const requestBody = optionsRef.current.prepareSendCommandsRequest
      ? await optionsRef.current.prepareSendCommandsRequest({
          ...body,
          commands,
          state: store.getSnapshot().state,
        })
      : { ...body, commands };
    const addCommands = commands.filter(isAddMessageCommand) as UserAddMessageCommand[];
    if (addCommands.length > 0) store.setPendingCommands(addCommands);
    await openStream(optionsRef.current.api, requestBody, commands);
  }, [openStream, store]);

  const attach = useCallback(async () => {
    const state = store.getSnapshot();
    const body = await optionsRef.current.body();
    const runId = state.targetRunId ?? state.state.current_run_id;
    if (runId === null) return;
    await openStream(
      optionsRef.current.resumeApi,
      { ...body, commands: [], runId, taskId, threadId: `task-${taskId}` },
      [],
    );
  }, [openStream, store, taskId]);

  useEffect(() => {
    if (!options.attachRef) return;
    options.attachRef.current = attach;
    return () => {
      if (options.attachRef?.current === attach) options.attachRef.current = null;
    };
  }, [attach, options.attachRef]);

  useEffect(() => {
    options.onStateCommit?.(view.state);
  }, [options.onStateCommit, view.state]);

  useEffect(() => () => closeActiveStream(), [closeActiveStream]);

  const onNew = useCallback((message: AppendMessage) => {
    return sendCommands([toAddMessageCommand(message)]);
  }, [sendCommands]);
  const onEdit = useCallback((message: AppendMessage) => {
    return sendCommands([toAddMessageCommand(message)]);
  }, [sendCommands]);
  const onCancel = useCallback(async () => {
    const state = store.getSnapshot();
    const runId = state.targetRunId ?? state.state.current_run_id;
    closeActiveStream();
    if (runId === null) return;
    try {
      await fetch(`${new URL(optionsRef.current.api).origin}/runs/${runId}/cancel`, {
        method: "POST",
        headers: await optionsRef.current.headers(),
      });
      optionsRef.current.onCancel?.({ commands: state.pendingCommands });
    } catch (error) {
      const normalized = error instanceof Error ? error : new Error(String(error));
      optionsRef.current.onCancel?.({ error: normalized, commands: state.pendingCommands });
      throw normalized;
    }
  }, [closeActiveStream, store]);
  const onRefetchThread = useCallback(async () => {
    const currentSnapshot = store.getSnapshot();
    const attach = optionsRef.current.attachRef?.current;
    if ((currentSnapshot.targetRunStatus === "pending" || currentSnapshot.targetRunStatus === "running") && attach) {
      await attach();
      return;
    }
    const response = await fetch(`${optionsRef.current.api.replace(/\/assistant$/, "")}/tasks/${taskId}/assistant/state`, {
      headers: await optionsRef.current.headers(),
    });
    if (!response.ok) throw new Error(`Assistant snapshot request failed: ${response.status}`);
    store.importState(await response.json() as TransportState);
  }, [store, taskId]);

  const adapter = useMemo<ExternalStoreAdapter<FrameStoreItem>>(() => {
    const base = {
      messages: view.items,
      isRunning: view.isRunning,
      isDisabled: options.readonly,
      isSendDisabled: options.readonly,
      state: view.state as unknown as ReadonlyJSONValue,
      ...(options.readonly ? {} : { onNew, onEdit, onCancel }),
      onRefetchThread,
      onLoadExternalState: (state: unknown) => {
        if (isTransportStateLike(state)) store.importState(state);
      },
      convertMessage: convertFrameStoreItem,
      adapters: options.adapters,
    };
    // The library type models onNew as mandatory for writable runtimes. Readonly runtimes
    // intentionally omit it at runtime so Assistant UI does not advertise a write handler.
    return base as unknown as ExternalStoreAdapter<FrameStoreItem>;
  }, [onCancel, onEdit, onNew, onRefetchThread, options.adapters, options.readonly, store, view]);

  const threadId = `task-${taskId}`;
  const threadListAdapter = useMemo<RemoteThreadListAdapter>(() => {
    const inMemory = new InMemoryThreadListAdapter();
    return Object.assign(inMemory, {
      list: async () => ({ threads: [{ remoteId: threadId, status: "regular" as const }] }),
      fetch: async () => ({ remoteId: threadId, status: "regular" as const }),
    });
  }, [threadId]);

  return useRemoteThreadListRuntime({
    adapter: threadListAdapter,
    threadId,
    allowNesting: true,
    runtimeHook: () => useExternalStoreRuntime(adapter),
  });
}

function isAddMessageCommand(value: unknown): value is UserAddMessageCommand {
  return typeof value === "object" && value !== null && (value as { type?: unknown }).type === "add-message";
}

export function toAddMessageCommand(message: AppendMessage): UserAddMessageCommand {
  const parts: Array<UserAddMessageCommand["message"]["parts"][number]> = [];
  for (const part of message.content) {
    if (part.type === "text") {
      parts.push({ type: "text", text: part.text });
      continue;
    }
    if (part.type === "image") {
      parts.push({ type: "image", image: part.image });
      continue;
    }
    if (part.type === "file") {
      parts.push({
        type: "file",
        data: part.data,
        filename: part.filename ?? "附件",
        mimeType: part.mimeType,
      });
    }
  }

  // assistant-ui keeps uploaded attachments in `message.attachments`; they
  // are not copied into `message.content` by the composer. Images therefore
  // have to cross the transport boundary from the attachment content here,
  // otherwise they are visible in the composer but silently absent from the
  // add-message command and the canonical user snapshot.
  const seenImages = new Set(
    parts
      .filter((part): part is { type: "image"; image: string } => part.type === "image")
      .map((part) => part.image),
  );
  for (const attachment of message.attachments ?? []) {
    for (const attachmentPart of attachment.content ?? []) {
      if (attachmentPart.type !== "image" || seenImages.has(attachmentPart.image)) continue;
      parts.push({ type: "image", image: attachmentPart.image });
      seenImages.add(attachmentPart.image);
    }
  }

  return {
    type: "add-message",
    sourceId: message.sourceId,
    parentId: message.parentId,
    message: { role: "user", parts },
  };
}

function isTransportStateLike(value: unknown): value is TransportState {
  return typeof value === "object" && value !== null && Array.isArray((value as TransportState).runs);
}
