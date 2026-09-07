"use client";

import {
  AssistantRuntimeProvider,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  useCallback,
  useEffect,
  memo,
  useRef,
  useState,
  useSyncExternalStore,
  type MutableRefObject,
} from "react";

import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { TransportStatus, type TransportIssue } from "@/components/assistant/transport-status";
import { apiRequest } from "@/lib/api/client";
import { cancelRun, type CancelRunResult } from "@/lib/assistant/cancel-run";
import { extractUserAddMessageText, toTransportThreadView } from "@/lib/assistant/converter";
import type { TransportState } from "@/lib/assistant/contract";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import { readStoredSelection } from "@/lib/model-selection-storage";
import { newTraceId, setActiveTraceId } from "@/lib/trace";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import { useTaskAssistantTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import {
  getBackendBaseUrlSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";

type AssistantRuntimeProps = {
  taskId: number;
  workspaceId?: number | null;
  initialState: TransportState;
  initialMessage?: string;
};

export function AssistantRuntime({ taskId, workspaceId, initialState, initialMessage }: AssistantRuntimeProps) {
  const [issue, setIssue] = useState<TransportIssue | null>(null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue} />
      <div className="min-h-0 flex-1">
        <RuntimeSession
          taskId={taskId}
          workspaceId={workspaceId}
          initialState={initialState}
          initialMessage={initialMessage}
          setIssue={setIssue}
        />
      </div>
    </div>
  );
}

type RuntimeSessionProps = AssistantRuntimeProps & {
  setIssue: (issue: TransportIssue | null) => void;
};

/**
 * Assistant UI runtime 的进程内会话边界。
 *
 * `useAssistantTransportRuntime` 内部持有 command queue、run manager 和流请求的
 * AbortController。外层工作区刷新、错误提示更新或任务列表刷新不应让这个组件
 * 重新执行 hook；否则 Assistant UI 会把 runtime 资源视为被替换，并主动取消正在
 * 读取的响应体。task 切换由 `Assistant` 的首屏快照生命周期控制，活动请求不会
 * 依赖父组件 key 或普通状态刷新来维持。
 */
const RuntimeSession = memo(function RuntimeSession({ taskId, workspaceId, initialState, setIssue, initialMessage }: RuntimeSessionProps) {
  const backendBaseUrl = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendBaseUrlSnapshot,
    getBackendBaseUrlSnapshot,
  );
  const commandIds = useRef(new WeakMap<object, string>());
  const latestStateRef = useRef(initialState);
  const previousBackendBaseUrlRef = useRef(backendBaseUrl);
  const runtimeControlsRef = useRef<RuntimeControls | null>(null);
  const registerRuntimeControls = useCallback((controls: RuntimeControls | null) => {
    runtimeControlsRef.current = controls;
  }, []);
  const [traceId] = useState(() => newTraceId());
  const composerRestoreRef = useRef<((text: string) => void) | null>(null);
  const initialMessageSentRef = useRef(false);
  const initialStateRef = useRef(initialState);
  const transportUpdateCountRef = useRef(0);
  const finishCountRef = useRef(0);
  const mountedRef = useRef(false);
  const unmountLogTimerRef = useRef<number | null>(null);
  const latestConnectionMetadataRef = useRef<{
    pendingCommandCount: number;
    isSending: boolean;
    commandTypes: string[];
  }>({ pendingCommandCount: 0, isSending: false, commandTypes: [] });

  useEffect(() => {
    mountedRef.current = true;
    if (unmountLogTimerRef.current !== null) {
      window.clearTimeout(unmountLogTimerRef.current);
      unmountLogTimerRef.current = null;
    }
    setActiveTraceId(traceId);
    void frontendLog("INFO", "assistant_runtime_mounted", "Assistant runtime 已挂载", {
      traceId,
      data: {
        taskId,
        workspaceId: workspaceId ?? null,
        initialMessageCount: initialStateRef.current.messages.length,
        initialRunId: initialStateRef.current.run.runId,
        initialRunStatus: initialStateRef.current.run.status,
      },
    });
    return () => {
      mountedRef.current = false;
      // React StrictMode deliberately performs an effect cleanup/setup pair in
      // development. Delay the diagnostic one macrotask so that pair is not
      // mistaken for a real runtime teardown (which would abort SSE).
      unmountLogTimerRef.current = window.setTimeout(() => {
        unmountLogTimerRef.current = null;
        if (mountedRef.current) return;
        void frontendLog("WARNING", "assistant_runtime_unmounted", "Assistant runtime 生命周期已清理", {
          traceId,
          data: {
            taskId,
            workspaceId: workspaceId ?? null,
            lastRunId: latestStateRef.current.run.runId,
            lastRunStatus: latestStateRef.current.run.status,
          },
        });
      }, 0);
      setActiveTraceId(null);
    };
  }, [taskId, traceId, workspaceId]);

  useEffect(() => {
    const previousBaseUrl = previousBackendBaseUrlRef.current;
    previousBackendBaseUrlRef.current = backendBaseUrl;
    if (previousBaseUrl === backendBaseUrl) return;

    void frontendLog("INFO", "assistant_backend_runtime_url_changed", "Assistant 后端地址已变化", {
      traceId,
      data: { taskId, previousBaseUrl, backendBaseUrl },
    });

    const status = latestStateRef.current.run.status;
    if (status === "pending" || status === "running") {
      setIssue({ message: "本机后端已重启，正在恢复当前对话…", retryable: true });
      // The supervisor may replace the backend process and port. Keep this
      // runtime instance alive and let Assistant UI resume the same run with
      // the newly published transport URL.
      runtimeControlsRef.current?.resume();
    }
  }, [backendBaseUrl, setIssue]);

  const registerComposerRestore = useCallback((restore: (text: string) => void) => {
    composerRestoreRef.current = restore;
  }, []);

  const handleSendError = useCallback(async (error: Error, params: { commands: readonly unknown[]; updateState: (updater: (state: TransportState) => TransportState) => void }) => {
    void frontendLog("ERROR", "assistant_transport_stream_error", "Assistant Transport 流发生错误", {
      traceId,
      data: {
        taskId,
        commandCount: params.commands.length,
        lastRunId: latestStateRef.current.run.runId,
        lastRunStatus: latestStateRef.current.run.status,
      },
      error,
    });
    const failedText = [...params.commands].reverse().map(extractUserAddMessageText).find((text) => text.trim().length > 0);
    if (failedText) composerRestoreRef.current?.(failedText);
    setIssue({
      message: safeFrontendErrorMessage(error, "网络异常，请检查本机后端是否正在运行"),
      retryable: true,
    });

    // 重新读取服务端 snapshot 仅用于恢复 runtime 的本地渲染基线，不把任何 UI
    // state 写回后端，也不把本地错误伪装成 canonical message。
    try {
      const snapshot = parseTransportState(await apiRequest<unknown>(`/tasks/${taskId}/assistant/state`));
      params.updateState(() => snapshot);
    } catch {
      // 原始传输错误已经可见，恢复失败不覆盖它。
    }
  }, [taskId, setIssue, traceId]);

  const reconcileAfterTransportFinish = useCallback(async () => {
    try {
      void frontendLog("INFO", "assistant_transport_reconcile_started", "Assistant Transport 流结束后读取最新快照", {
        traceId,
        data: { taskId, lastRunId: latestStateRef.current.run.runId, lastRunStatus: latestStateRef.current.run.status },
      });
      const snapshot = parseTransportState(await apiRequest<unknown>(`/tasks/${taskId}/assistant/state`));
      latestStateRef.current = snapshot;
      void frontendLog("INFO", "assistant_transport_reconcile_completed", "Assistant Transport 最新快照已读取", {
        traceId,
        data: {
          taskId,
          runId: snapshot.run.runId,
          runStatus: snapshot.run.status,
          messageCount: snapshot.messages.length,
        },
      });
      if (snapshot.run.status === "pending" || snapshot.run.status === "running") {
        runtimeControlsRef.current?.resume();
        return;
      }

      // A resume-state 204 means Assistant UI did not import a new state. Push
      // the terminal canonical snapshot into the same runtime so it cannot
      // repeatedly interpret its stale local pending/running state as active.
      runtimeControlsRef.current?.importState(snapshot);
      setIssue(null);
    } catch (error) {
      setIssue({
        message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
        retryable: true,
      });
    }
  }, [taskId, setIssue, traceId]);

  const runtime = useTaskAssistantTransportRuntime(taskId, {
    initialState,
    protocol: "assistant-transport",
    capabilities: { edit: false },
    api: `${backendBaseUrl}/assistant`,
    resumeApi: `${backendBaseUrl}/assistant/resume`,
    resumeStateApi: `${backendBaseUrl}/tasks/${taskId}/assistant/resume-state`,
    headers: async () => ({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Trace-Id": traceId,
    }),
    body: async () => {
      const selection = readStoredSelection(taskId);
      return {
        taskId,
        threadId: `task-${taskId}`,
        ...(workspaceId != null ? { workspaceId } : {}),
        ...(selection?.providerId !== undefined ? { providerId: selection.providerId } : {}),
        ...(selection?.modelName !== undefined ? { modelName: selection.modelName } : {}),
        ...(selection?.reasoningEffort !== undefined ? { reasoningEffort: selection.reasoningEffort } : {}),
      };
    },
    prepareSendCommandsRequest: (body) => {
      // assistant-ui 的 request body 会带上本地 state。后端契约 extra=forbid，
      // 所以只能在这个唯一发送边界剥离它；前端不会把 snapshot 当成事实回传。
      const backendRequest = Object.fromEntries(
        Object.entries(body).filter(([key]) => key !== "state"),
      );
      const commands = body.commands.map((command) => {
        const key = command as object;
        let commandId = commandIds.current.get(key);
        if (!commandId) {
          commandId = crypto.randomUUID();
          commandIds.current.set(key, commandId);
        }
        return command.type === "add-message" ? { ...command, commandId } : command;
      });
      void frontendLog("INFO", "assistant_transport_request_prepared", "Assistant Transport 请求已准备发送", {
        traceId,
        data: {
          taskId,
          threadId: `task-${taskId}`,
          commandCount: commands.length,
          commandTypes: commands.map((command) => command.type),
          stateStripped: Object.prototype.hasOwnProperty.call(body, "state"),
          parentIdPresent: Object.prototype.hasOwnProperty.call(body, "parentId"),
        },
      });
      return {
        ...backendRequest,
        commands,
        taskId,
        threadId: `task-${taskId}`,
        ...(workspaceId != null ? { workspaceId } : {}),
      };
    },
    onResponse: (response) => {
      void frontendLog("INFO", "assistant_transport_response_received", "Assistant Transport 已收到响应头", {
        traceId,
        data: {
          taskId,
          status: response.status,
          contentType: response.headers.get("content-type"),
          taskHeader: response.headers.get("x-cosir-task-id"),
          threadHeader: response.headers.get("x-cosir-thread-id"),
          traceHeader: response.headers.get("x-trace-id"),
        },
      });
      setIssue(null);
    },
    onFinish: () => {
      finishCountRef.current += 1;
      const status = latestStateRef.current.run.status;
      const connectionMetadata = latestConnectionMetadataRef.current;
      void frontendLog("INFO", "assistant_transport_stream_finished", "Assistant Transport 流生命周期结束", {
        traceId,
        data: {
          taskId,
          finishCount: finishCountRef.current,
          runId: latestStateRef.current.run.runId,
          runStatus: status,
          messageCount: latestStateRef.current.messages.length,
          pendingCommandCount: connectionMetadata.pendingCommandCount,
          isSending: connectionMetadata.isSending,
          pendingCommandTypes: connectionMetadata.commandTypes,
        },
      });
      const terminal = status === "completed"
        || status === "failed"
        || status === "cancelled"
        || status === "interrupted";
      if (!terminal) {
        setIssue({ message: "连接暂时中断，正在从本机后端恢复最新状态…", retryable: true });
        // EOF 不是完成信号：保留同一个 runtime，并让 Assistant UI 使用
        // resumeApi/resumeStateApi 重新订阅原 run。不能通过 key 重建 runtime，
        // 否则旧 runtime 的 AbortController 会主动取消当前请求生命周期。
        void reconcileAfterTransportFinish();
      } else {
        setIssue(null);
      }
    },
    onError: handleSendError,
    onCancel: ({ error, commands }) => {
      void frontendLog("WARNING", "assistant_transport_stream_cancelled", "Assistant Transport 请求被取消", {
        traceId,
        data: {
          taskId,
          commandCount: commands?.length ?? 0,
          runId: latestStateRef.current.run.runId,
          runStatus: latestStateRef.current.run.status,
        },
        error,
      });
      if (error) {
        setIssue({
          message: safeFrontendErrorMessage(error, "发送已取消，后端仍在确认运行状态"),
          retryable: true,
        });
      }
    },
    converter: (state, connectionMetadata) => {
      latestStateRef.current = state;
      latestConnectionMetadataRef.current = {
        pendingCommandCount: connectionMetadata.pendingCommands.length,
        isSending: connectionMetadata.isSending,
        commandTypes: connectionMetadata.pendingCommands.map((command) => command.type),
      };
      transportUpdateCountRef.current += 1;
      if (
        transportUpdateCountRef.current <= 3
        || transportUpdateCountRef.current % 20 === 0
        || state.run.status === "completed"
        || state.run.status === "failed"
        || state.run.status === "cancelled"
        || state.run.status === "interrupted"
      ) {
        void frontendLog("DEBUG", "assistant_transport_state_update", "Assistant Transport 状态更新采样记录", {
          traceId,
          data: {
            taskId,
            updateCount: transportUpdateCountRef.current,
            runId: state.run.runId,
            runStatus: state.run.status,
            messageCount: state.messages.length,
            connectionMetadataPresent: connectionMetadata != null,
            pendingCommandCount: connectionMetadata.pendingCommands.length,
            isSending: connectionMetadata.isSending,
            pendingCommandTypes: connectionMetadata.pendingCommands.map((command) => command.type),
          },
        });
      }
      return toTransportThreadView(state, connectionMetadata);
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RuntimeControlBridge
        register={registerRuntimeControls}
        resumeOnMount={initialState.run.status === "pending" || initialState.run.status === "running"}
        taskId={taskId}
      />
      <ComposerRestoreBridge register={registerComposerRestore} />
      <div className="flex h-full min-h-0 flex-col">
        <Thread taskId={taskId} />
      </div>
      <InitialMessageBridge
        text={initialMessage}
        sentRef={initialMessageSentRef}
        initialState={initialState}
        taskId={taskId}
      />
    </AssistantRuntimeProvider>
  );
});

type RuntimeControls = {
  resume: () => void;
  importState: (state: TransportState) => void;
};

function RuntimeControlBridge({
  register,
  resumeOnMount,
  taskId,
}: {
  register: (controls: RuntimeControls | null) => void;
  resumeOnMount: boolean;
  taskId: number;
}) {
  const aui = useAui();
  const remoteThreadId = useAuiState((state) => state.threadListItem.remoteId);
  const initialResumeIssuedRef = useRef(false);

  useEffect(() => {
    register({
      resume: () => aui.thread.resumeRun({ parentId: null }),
      importState: (state) => aui.thread.importExternalState(state),
    });
    return () => register(null);
  }, [aui, register]);

  useEffect(() => {
    if (!resumeOnMount || initialResumeIssuedRef.current || remoteThreadId !== `task-${taskId}`) return;
    // RemoteThreadListRuntime finishes binding the concrete thread runtime in
    // the same commit. Defer the resume to the next macrotask so we never
    // issue it against the transient inert binding returned during mount.
    const timer = window.setTimeout(() => {
      if (initialResumeIssuedRef.current) return;
      initialResumeIssuedRef.current = true;
      void aui.thread.resumeRun({ parentId: null });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [aui, remoteThreadId, resumeOnMount, taskId]);

  return null;
}

function InitialMessageBridge({
  text,
  sentRef,
  initialState,
  taskId,
}: {
  text?: string;
  sentRef: MutableRefObject<boolean>;
  initialState: TransportState;
  taskId: number;
}) {
  const aui = useAui();
  const remoteThreadId = useAuiState((state) => state.threadListItem.remoteId);

  useEffect(() => {
    // The pending text is retained by the task session rather than consumed by
    // WorkspaceShell. If the session is revisited after the task already has a
    // persisted message, the canonical snapshot is the idempotency guard.
    if (
      !text?.trim()
      || sentRef.current
      || remoteThreadId !== `task-${taskId}`
      || initialState.messages.length > 0
      || initialState.run.status !== "idle"
    ) return;
    aui.thread.composer().setText(text);
    const timer = window.setTimeout(() => {
      if (sentRef.current) return;
      sentRef.current = true;
      aui.thread.composer().send();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [aui, initialState.messages.length, initialState.run.status, remoteThreadId, sentRef, taskId, text]);

  return null;
}

function ComposerRestoreBridge({ register }: { register: (restore: (text: string) => void) => void }) {
  const aui = useAui();
  useEffect(() => {
    register((text) => aui.thread.composer().setText(text));
  }, [aui, register]);
  return null;
}

export async function cancelAssistantRun(taskId: number, runId: number): Promise<CancelRunResult> {
  return cancelRun(taskId, runId);
}
