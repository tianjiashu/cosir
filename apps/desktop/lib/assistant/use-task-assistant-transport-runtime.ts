import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ExternalStoreAdapter,
} from "@assistant-ui/react";
import type { ReadonlyJSONValue } from "assistant-stream/utils";
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import {
  convertFrameStoreItem,
  parseTransportFrame,
  TransportFrameStore,
  type FrameStoreItem,
} from "@/lib/assistant/transport-frame-store";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { cancelTask } from "@/lib/assistant/cancel-task";
import type { TaskCommandAccepted } from "@/lib/assistant/task-commands";
import { parseHttpError } from "@/lib/http/errors";

export type TaskAssistantTransportOptions = {
  initialState: TransportState;
  adapters?: ExternalStoreAdapter<FrameStoreItem>["adapters"];
  /** 本机后端基地址；attach / state 端点按 taskId 拼接。 */
  baseUrl: string;
  headers: () => Promise<Record<string, string>> | Record<string, string>;
  /**
   * 提交一条用户消息（`sourceId` 存在即编辑重跑），返回后端回执。宿主负责请求体组装与
   * 端点选择；本 hook 只负责 pending 展示、回执 snapshot 导入与后续 attach。只读 runtime 不提供。
   */
  submitMessage?: (command: UserAddMessageCommand) => Promise<TaskCommandAccepted>;
  onResponse?: (response: Response) => void;
  onFinish?: (status: { targetRunId: number | null; targetRunStatus: string | null }) => void;
  onError?: (
    error: Error,
    params: {
      commands: readonly unknown[];
      phase: "request-rejected" | "accepted-stream-failed";
      updateState: (updater: (state: TransportState) => TransportState) => void;
      clearPendingCommands: () => void;
    },
  ) => Promise<void>;
  onCancel?: (params: { error?: Error; commands: readonly unknown[] }) => void;
  onAttachReady?: (attach: (() => Promise<void>) | null) => void;
  readonly?: boolean;
  onStateCommit?: (state: TransportState) => void;
  performanceProbe?: AssistantPerformanceProbe | null;
};

type StreamCallbacks = {
  commands: readonly unknown[];
  controller: AbortController;
  performanceStarted: boolean;
};

/**
 * 为单个 task 的 frame store 创建 assistant-ui external adapter。
 *
 * frame store 是唯一的流式投影持有者；本 hook 负责请求与连接生命周期，通过被替换的
 * AbortController 和对象身份检查隔离旧 SSE 连接，不引入协议版本字段。它不创建业务
 * 持久化状态，导出的 runtime hook 再选择合适的 assistant-ui 宿主。
 */
function useTaskAssistantTransportAdapter(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const store = useMemo(() => new TransportFrameStore(options.initialState, {
    performanceProbe: options.performanceProbe ?? undefined,
  }), [options.initialState, options.performanceProbe, taskId]);
  const view = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const activeStreamRef = useRef<StreamCallbacks | null>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const closeActiveStream = useCallback(() => {
    const callbacks = activeStreamRef.current;
    if (callbacks?.performanceStarted) {
      const snapshot = store.getSnapshot();
      optionsRef.current.performanceProbe?.finishStream({
        targetRunId: snapshot.targetRunId,
        targetRunStatus: snapshot.targetRunStatus,
      });
      callbacks.performanceStarted = false;
    }
    callbacks?.controller.abort();
    activeStreamRef.current = null;
  }, [store]);

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
    // SSE EOF 可能早于宿主 WebView 的下一次动画帧；先提交最后一批 active row 内容，再让完成回调
    // 读取最新的 Transport state，避免终态收敛基于上一个动画帧的旧快照。
    if (activeStreamRef.current === callbacks) store.flushScheduledPublication();
  }, [store]);

  const attachApi = `${options.baseUrl}/tasks/${taskId}/assistant/attach`;
  const stateApi = `${options.baseUrl}/tasks/${taskId}/assistant/state`;

  /** attach 只订阅 Task 当前 Run，不携带请求体，也不触发业务执行。 */
  const openStream = useCallback(async (commands: readonly unknown[]) => {
    closeActiveStream();
    const controller = new AbortController();
    const callbacks: StreamCallbacks = { commands, controller, performanceStarted: false };
    activeStreamRef.current = callbacks;
    let accepted = false;
    try {
      const response = await fetch(attachApi, {
        cache: "no-store",
        method: "POST",
        headers: await optionsRef.current.headers(),
        signal: controller.signal,
      });
      if (!response.ok) {
        // 结构化错误（code / message / retryable）由 parseHttpError 解析为 HttpError；
        // 响应体原文不进入错误消息，界面经 userErrorMessage 只展示后端给出的可读原因。
        throw await parseHttpError(response);
      }
      if (activeStreamRef.current !== callbacks) return;
      accepted = true;
      callbacks.performanceStarted = true;
      optionsRef.current.performanceProbe?.beginStream();
      optionsRef.current.onResponse?.(response);
      await readSse(response, callbacks);
      if (activeStreamRef.current === callbacks) {
        activeStreamRef.current = null;
        const snapshot = store.getSnapshot();
        optionsRef.current.performanceProbe?.finishStream({
          targetRunId: snapshot.targetRunId,
          targetRunStatus: snapshot.targetRunStatus,
        });
        callbacks.performanceStarted = false;
        optionsRef.current.onFinish?.({
          targetRunId: snapshot.targetRunId,
          targetRunStatus: snapshot.targetRunStatus,
        });
      }
    } catch (error) {
      if (controller.signal.aborted || activeStreamRef.current !== callbacks) return;
      activeStreamRef.current = null;
      const normalized = error instanceof Error ? error : new Error(String(error));
      if (accepted) {
        const snapshot = store.getSnapshot();
        optionsRef.current.performanceProbe?.finishStream({
          targetRunId: snapshot.targetRunId,
          targetRunStatus: snapshot.targetRunStatus,
        });
        callbacks.performanceStarted = false;
      }
      await optionsRef.current.onError?.(normalized, {
        commands,
        phase: accepted ? "accepted-stream-failed" : "request-rejected",
        updateState: (updater) => store.importState(updater(store.getSnapshot().state)),
        clearPendingCommands: () => store.clearPendingCommands(commands as UserAddMessageCommand[]),
      });
    }
  }, [attachApi, closeActiveStream, readSse, store]);

  const sendMessage = useCallback(async (command: UserAddMessageCommand) => {
    const submit = optionsRef.current.submitMessage;
    if (!submit) throw new Error("只读 Transport runtime 不接受消息提交");
    store.setPendingCommands([command]);
    try {
      const accepted = await submit(command);
      store.clearPendingCommands([command]);
      // 已有订阅接收提交事件，不能用较早的 HTTP 快照覆盖正在到达的流式帧。
      if (!activeStreamRef.current || !accepted.queued) {
        if (!activeStreamRef.current) store.importState(accepted.state);
        const run = accepted.state.runs.find((candidate) => candidate.runId === accepted.state.current_run_id);
        if (run && ["pending", "running", "waiting_for_input"].includes(run.status)) void openStream([]);
      }
    } catch (error) {
      // 请求未发出（如附件已失效）与后端拒绝同属 request-rejected，交给同一个 onError 恢复草稿并提示。
      store.clearPendingCommands([command]);
      await optionsRef.current.onError?.(error instanceof Error ? error : new Error(String(error)), {
        commands: [command],
        phase: "request-rejected",
        updateState: (updater) => store.importState(updater(store.getSnapshot().state)),
        clearPendingCommands: () => store.clearPendingCommands([command]),
      });
    }
  }, [openStream, store]);

  const attach = useCallback(async () => {
    const state = store.getSnapshot();
    if ((state.targetRunId ?? state.state.current_run_id) === null) return;
    await openStream([]);
  }, [openStream, store]);

  useEffect(() => {
    options.onAttachReady?.(attach);
    return () => options.onAttachReady?.(null);
  }, [attach, options.onAttachReady]);

  useEffect(() => {
    options.onStateCommit?.(view.state);
  }, [options.onStateCommit, view.state]);

  useEffect(() => () => closeActiveStream(), [closeActiveStream]);

  // 编辑与新消息共用提交路径：`sourceId` 随命令进入宿主，由宿主选择编辑端点。
  const onNew = useCallback((message: AppendMessage) => {
    return sendMessage(toAddMessageCommand(message));
  }, [sendMessage]);
  const onCancel = useCallback(async () => {
    const commands = store.getSnapshot().pendingCommands;
    const result = await cancelTask(taskId);
    if (result.accepted) {
      optionsRef.current.onCancel?.({ commands });
      return;
    }
    const error = new Error(result.message);
    optionsRef.current.onCancel?.({ error, commands });
    throw error;
  }, [store, taskId]);
  const onRefetchThread = useCallback(async () => {
    const currentSnapshot = store.getSnapshot();
    if (currentSnapshot.targetRunStatus === "pending" || currentSnapshot.targetRunStatus === "running" || currentSnapshot.targetRunStatus === "waiting_for_input") {
      await attach();
      return;
    }
    const response = await fetch(stateApi, {
      cache: "no-store",
      headers: await optionsRef.current.headers(),
    });
    if (!response.ok) throw await parseHttpError(response);
    store.importState(await response.json() as TransportState);
  }, [attach, stateApi, store]);

  const adapter = useMemo<ExternalStoreAdapter<FrameStoreItem>>(() => {
    const base = {
      messages: view.items,
      isRunning: view.isRunning,
      isDisabled: options.readonly,
      isSendDisabled: options.readonly,
      state: view.state as unknown as ReadonlyJSONValue,
      ...(options.readonly ? {} : { onNew, onEdit: onNew, onCancel }),
      onRefetchThread,
      onLoadExternalState: (state: unknown) => {
        if (isTransportStateLike(state)) store.importState(state);
      },
      convertMessage: convertFrameStoreItem,
      adapters: options.adapters,
    };
    // 库类型把 onNew 建模为 writable runtime 的必填项；readonly runtime 有意在运行时省略，
    // 使 Assistant UI 不暴露写入处理器。
    return base as unknown as ExternalStoreAdapter<FrameStoreItem>;
  }, [onCancel, onNew, onRefetchThread, options.adapters, options.readonly, store, view]);

  return adapter;
}

/**
 * 创建 task 级可写 Transport runtime。
 *
 * task 导航和持久化身份由 Workbench 负责，本 runtime 只拥有当前 task 的
 * TransportFrameStore；因此直接使用 external-store runtime，避免引入远程
 * thread-list 的异步绑定阶段。该函数不创建额外线程列表，也不写入后端事实。
 */
export function useTaskAssistantTransportRuntime(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const adapter = useTaskAssistantTransportAdapter(taskId, options);
  return useExternalStoreRuntime(adapter);
}

/** 创建不挂载嵌套远程 thread list 的只读 Workbench runtime。 */
export function useTaskAssistantReadonlyTransportRuntime(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const adapter = useTaskAssistantTransportAdapter(taskId, options);
  return useExternalStoreRuntime(adapter);
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

    // assistant-ui 将上传附件保存在 `message.attachments`，composer 不会把它们复制到
    // `message.content`。因此图片必须在这里从附件内容跨过 Transport 边界，否则输入框
    // 看得到图片，但 add-message command 和 canonical user snapshot 会静默缺少它。
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
