import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ExternalStoreAdapter,
} from "@assistant-ui/react";
import type { ReadonlyJSONValue } from "assistant-stream/utils";
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import type { TransportRunStatus, TransportState } from "@/lib/assistant/contract";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import {
  convertFrameStoreItem,
  TransportFrameStore,
  type FrameStoreItem,
} from "@/lib/assistant/transport-frame-store";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { cancelTask } from "@/lib/assistant/task-commands";
import { TaskTransportSession, type TransportDisconnect } from "@/lib/assistant/task-transport-session";
import type { TraceId } from "@/lib/trace";

export type { TransportDisconnect } from "@/lib/assistant/task-transport-session";

export type TaskAssistantTransportOptions = {
  initialState: TransportState;
  adapters?: ExternalStoreAdapter<FrameStoreItem>["adapters"];
  /** 长订阅的链路 id；基地址由 http client 按当前后端实例解析，后端换实例经 `connectionKey` 重开。 */
  traceId: TraceId;
  /**
   * 提交一条用户消息（`sourceId` 存在即编辑）。宿主负责请求体组装与端点选择；后端处理完这一轮
   * 才返回，被拒时抛错（乐观消息随即收回），输出经长订阅到达；乐观消息保留到 snapshot 里出现
   * 对应消息才清除。只读 runtime 不提供。
   */
  submitMessage?: (command: UserAddMessageCommand) => Promise<void>;
  /** 长订阅建立（attach 2xx）。 */
  onResponse?: (response: Response) => void;
  /** 一轮输出结束：当前活动从活动态进入终态（含重连 full 帧补发）；挂起不算。 */
  onActivitySettled?: (status: TransportRunStatus) => void;
  /**
   * 长订阅意外结束：长订阅只在客户端主动关闭、背压重同步或 Task 删除时结束，任何其他结束
   * （EOF、网络错误、attach 非 2xx）都应由宿主进入有限重连。主动关闭不回调。
   */
  onDisconnect?: (disconnect: TransportDisconnect) => void;
  /** 命令提交被拒或未能发出（HTTP 非 2xx、网络失败、附件失效）；乐观消息已先行收回。 */
  onError?: (error: Error, commands: readonly UserAddMessageCommand[]) => Promise<void> | void;
  onCancel?: (params: { error?: Error; commands: readonly unknown[] }) => void;
  /**
   * 订阅身份：为 null 时不订阅（本机后端不可用）；非 null 时挂载即打开 Task 长订阅，值变化（后端
   * 换了实例）即重开。卸载或变为 null 时关闭。意外断开后的重连不归这里，由宿主经返回的 `open` 发起。
   */
  connectionKey: string | null;
  readonly?: boolean;
  onStateCommit?: (state: TransportState) => void;
  performanceProbe?: AssistantPerformanceProbe | null;
};

/**
 * 为单个 task 的 frame store 创建 assistant-ui external adapter。
 *
 * frame store 是唯一的流式投影持有者，长订阅与命令提交由 `TaskTransportSession` 承担；本 hook
 * 只做 React 装配：把 store 快照交给 assistant-ui、把活动跳变转给宿主回调。它不创建
 * 业务持久化状态。
 */
function useTaskAssistantTransportAdapter(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const store = useMemo(() => new TransportFrameStore(options.initialState, {
    performanceProbe: options.performanceProbe ?? undefined,
  }), [options.initialState, options.performanceProbe, taskId]);
  const view = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const session = useMemo(() => new TaskTransportSession(store, `/tasks/${taskId}/assistant/attach`, () => {
    const current = optionsRef.current;
    return {
      traceId: current.traceId,
      submitMessage: current.submitMessage,
      onResponse: current.onResponse,
      onDisconnect: current.onDisconnect,
      onSubmitError: current.onError,
    };
  }), [store, taskId]);

  useEffect(() => store.subscribeActivity((transition) => {
    const probe = optionsRef.current.performanceProbe;
    if (transition.kind === "started") {
      probe?.beginStream();
      return;
    }
    probe?.finishStream({
      activityRunId: store.getSnapshot().state.activity?.runId ?? null,
      activityStatus: transition.status,
    });
    optionsRef.current.onActivitySettled?.(transition.status);
  }), [store]);

  // 订阅归 runtime 所有：有订阅身份就打开，身份变化或卸载时关闭旧连接再按新身份打开。
  const { connectionKey } = options;
  useEffect(() => {
    if (connectionKey === null) return;
    void session.open();
    return () => session.close();
  }, [connectionKey, session]);
  // 宿主重连打开的连接同样随 session 释放（connectionKey 为 null 期间也可能有）。
  useEffect(() => () => session.close(), [session]);

  useEffect(() => {
    options.onStateCommit?.(view.state);
  }, [options.onStateCommit, view.state]);

  // 编辑与新消息共用提交路径：`sourceId` 随命令进入宿主，由宿主选择编辑端点。
  const onNew = useCallback((message: AppendMessage) => {
    return session.submit(toAddMessageCommand(message));
  }, [session]);
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
  // 重新拉取即重开长订阅：attach 首帧就是完整 canonical state。
  const onRefetchThread = useCallback(() => session.open(), [session]);

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

  return { adapter, open: session.open };
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
  const { adapter, open } = useTaskAssistantTransportAdapter(taskId, options);
  return { runtime: useExternalStoreRuntime(adapter), open };
}

/** 创建不挂载嵌套远程 thread list 的只读 Workbench runtime。 */
export function useTaskAssistantReadonlyTransportRuntime(
  taskId: number,
  options: TaskAssistantTransportOptions,
) {
  const { adapter, open } = useTaskAssistantTransportAdapter(taskId, options);
  return { runtime: useExternalStoreRuntime(adapter), open };
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
  return typeof value === "object" && value !== null && Array.isArray((value as TransportState).messages);
}
