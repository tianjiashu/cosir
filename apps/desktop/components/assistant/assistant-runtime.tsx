"use client";

import {
  AssistantRuntimeProvider,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  Component,
  useCallback,
  useEffect,
  memo,
  useRef,
  useState,
  useSyncExternalStore,
  type ErrorInfo,
  type MutableRefObject,
  type ReactNode,
} from "react";

import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { TransportStatus, type TransportIssue } from "@/components/assistant/transport-status";
import { requestJson } from "@/lib/http/client";
import { extractUserAddMessageText, getOrCreateTransportCommandId, getUserAddMessageSourceId, toTransportThreadView } from "@/lib/assistant/converter";
import type { TransportState } from "@/lib/assistant/contract";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import {
  modelContextToTransportFields,
  selectionToTransportFields,
} from "@/lib/assistant/model-request-adapter";
import { getModelCatalogSnapshot } from "@/lib/model-catalog";
import { readStoredSelection } from "@/lib/model-selection-storage";
import { parseTransportError } from "@/lib/assistant/transport-error";
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
  forkAvailable?: boolean;
  forkingRunId?: number | null;
  onForkRun?: (runId: number) => void;
  onTaskStateChanged?: () => void;
  onRunStateChange?: (isRunning: boolean) => void;
};

export function AssistantRuntime({ taskId, workspaceId, initialState, initialMessage, forkAvailable, forkingRunId, onForkRun, onTaskStateChanged, onRunStateChange }: AssistantRuntimeProps) {
  const [issue, setIssue] = useState<TransportIssue | null>(null);
  const [runtimeGeneration, setRuntimeGeneration] = useState(0);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue} />
      <div className="min-h-0 flex-1">
        <RuntimeErrorBoundary
          key={runtimeGeneration}
          taskId={taskId}
          onRetry={() => setRuntimeGeneration((generation) => generation + 1)}
        >
          <RuntimeSession
            taskId={taskId}
            workspaceId={workspaceId}
            initialState={initialState}
            initialMessage={initialMessage}
            forkAvailable={forkAvailable}
            forkingRunId={forkingRunId}
            onForkRun={onForkRun}
            onTaskStateChanged={onTaskStateChanged}
            onRunStateChange={onRunStateChange}
            setIssue={setIssue}
          />
        </RuntimeErrorBoundary>
      </div>
    </div>
  );
}

type RuntimeSessionProps = AssistantRuntimeProps & {
  setIssue: (issue: TransportIssue | null) => void;
};

type RuntimeRunDiagnostic = {
  runId?: unknown;
  status?: unknown;
};

function readRuntimeRun(value: unknown): RuntimeRunDiagnostic | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const state = value as { current_run_id?: unknown; runs?: unknown };
  if (!Array.isArray(state.runs) || typeof state.current_run_id !== "number") return undefined;
  const run = state.runs.find((candidate) =>
    typeof candidate === "object" && candidate !== null
      && (candidate as { runId?: unknown }).runId === state.current_run_id,
  );
  return typeof run === "object" && run !== null ? run as RuntimeRunDiagnostic : undefined;
}

function currentTransportRun(state: TransportState): TransportState["runs"][number] | undefined {
  return state.current_run_id === null
    ? undefined
    : state.runs.find((run) => run.runId === state.current_run_id);
}

function transportMessageCount(state: TransportState): number {
  return state.runs.reduce((count, run) => count + run.messages.length, 0);
}

type RuntimeErrorBoundaryProps = {
  taskId: number;
  children: ReactNode;
  onRetry: () => void;
};

type RuntimeErrorBoundaryState = {
  error: Error | null;
};

class RuntimeErrorBoundary extends Component<RuntimeErrorBoundaryProps, RuntimeErrorBoundaryState> {
  state: RuntimeErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RuntimeErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    void frontendLog("ERROR", "assistant_runtime_render_failed", "Assistant 对话界面渲染失败", {
      data: { taskId: this.props.taskId, componentStack: info.componentStack ?? "" },
      error,
    });
  }

  private handleRetry = (): void => {
    this.setState({ error: null });
    this.props.onRetry();
  };

  render(): ReactNode {
    if (this.state.error === null) return this.props.children;
    return (
      <section className="flex h-full min-h-0 flex-col items-center justify-center gap-3 p-6 text-center">
        <h2 className="text-sm font-medium">对话界面渲染失败</h2>
        <p className="text-muted-foreground max-w-md text-xs">对话数据仍保存在本机，可以重试恢复界面。</p>
        <button type="button" className="text-sm underline underline-offset-4" onClick={this.handleRetry}>重试</button>
      </section>
    );
  }
}

/**
 * Assistant UI runtime 的进程内会话边界。
 *
 * `useAssistantTransportRuntime` 内部持有 command queue、run manager 和流请求的
 * AbortController。外层工作区刷新、错误提示更新或任务列表刷新不应让这个组件
 * 重新执行 hook；否则 Assistant UI 会把 runtime 资源视为被替换，并主动取消正在
 * 读取的响应体。task 切换由 `Assistant` 的首屏快照生命周期控制，活动请求不会
 * 依赖父组件 key 或普通状态刷新来维持。
 */
const RuntimeSession = memo(function RuntimeSession({ taskId, workspaceId, initialState, setIssue, initialMessage, forkAvailable, forkingRunId, onForkRun, onTaskStateChanged, onRunStateChange }: RuntimeSessionProps) {
  const backendBaseUrl = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendBaseUrlSnapshot,
    getBackendBaseUrlSnapshot,
  );
  const latestStateRef = useRef(initialState);
  const previousBackendBaseUrlRef = useRef(backendBaseUrl);
  const runtimeControlsRef = useRef<RuntimeControls | null>(null);
  const registerRuntimeControls = useCallback((controls: RuntimeControls | null) => {
    runtimeControlsRef.current = controls;
  }, []);
  const cancelRequestedRunIdRef = useRef<number | null>(null);
  const lastTransportErrorRef = useRef<TransportIssue | null>(null);
  const commitTransportState = useCallback((state: TransportState) => {
    latestStateRef.current = state;
    const run = currentTransportRun(state);
    if (
      cancelRequestedRunIdRef.current === (run?.runId ?? null)
      && run?.status === "cancelled"
    ) {
      cancelRequestedRunIdRef.current = null;
      lastTransportErrorRef.current = null;
    }
  }, []);
  const [traceId] = useState(() => newTraceId());
  const composerRestoreRef = useRef<ComposerRestore | null>(null);
  const initialMessageSentRef = useRef(false);
  const initialStateRef = useRef(initialState);
  const finishCountRef = useRef(0);
  const mountedRef = useRef(false);
  const unmountLogTimerRef = useRef<number | null>(null);

  const handleCancelRequested = useCallback((runId: number) => {
    if (latestStateRef.current.current_run_id !== runId) return;
    cancelRequestedRunIdRef.current = runId;
    // A user cancellation supersedes a stale transport error from the same
    // request. The terminal cancelled snapshot will clear the intent below.
    lastTransportErrorRef.current = null;
  }, []);

  const handleCancelResult = useCallback((runId: number, accepted: boolean) => {
    if (accepted) {
      if (latestStateRef.current.current_run_id !== runId) return;
      lastTransportErrorRef.current = null;
      // The backend ACK is authoritative for the run status. Import a small
      // local terminal projection as a fallback for the race where the client
      // aborts the stream before it can consume the pushed terminal snapshot.
      // A later backend snapshot can still replace this projection with the
      // complete canonical message/tool state.
      const cancelledState = markTransportStateCancelled(latestStateRef.current, runId);
      latestStateRef.current = cancelledState;
      runtimeControlsRef.current?.importState(cancelledState);
      cancelRequestedRunIdRef.current = null;
      onTaskStateChanged?.();
      return;
    }
    if (cancelRequestedRunIdRef.current !== runId) return;
    // A rejected cancellation must not suppress the normal active-run
    // recovery path if the stream later ends unexpectedly.
    cancelRequestedRunIdRef.current = null;
  }, [onTaskStateChanged]);

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
        initialMessageCount: transportMessageCount(initialStateRef.current),
        initialRunId: initialStateRef.current.current_run_id,
        initialRunStatus: currentTransportRun(initialStateRef.current)?.status ?? null,
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
            lastRunId: latestStateRef.current.current_run_id,
            lastRunStatus: currentTransportRun(latestStateRef.current)?.status ?? null,
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

    const status = currentTransportRun(latestStateRef.current)?.status;
    if (status === "pending" || status === "running") {
      setIssue({ message: "本机后端已重启，正在恢复当前对话…", retryable: true });
      // The supervisor may replace the backend process and port. Keep this
      // runtime instance alive and let Assistant UI resume the same run with
      // the newly published transport URL.
      runtimeControlsRef.current?.resume();
    }
  }, [backendBaseUrl, setIssue]);

  const registerComposerRestore = useCallback((restore: ComposerRestore) => {
    composerRestoreRef.current = restore;
  }, []);

  const handleSendError = useCallback(async (error: Error, params: { commands: readonly unknown[]; updateState: (updater: (state: TransportState) => TransportState) => void }) => {
    const currentRunId = latestStateRef.current.current_run_id;
    if (cancelRequestedRunIdRef.current === currentRunId && currentRunId !== null) {
      // Assistant UI can report the client-side abort as a transport error
      // after the backend has already accepted the explicit cancellation.
      // It is an expected lifecycle event, not a recoverable transport fault.
      lastTransportErrorRef.current = null;
      setIssue(null);
      return;
    }
    void frontendLog("ERROR", "assistant_transport_stream_error", "Assistant Transport 流发生错误", {
      traceId,
      data: {
        taskId,
        commandCount: params.commands.length,
        lastRunId: latestStateRef.current.current_run_id,
        lastRunStatus: currentTransportRun(latestStateRef.current)?.status ?? null,
      },
      error,
    });
    const failedCommands = [...params.commands].reverse();
    const failedText = failedCommands.map(extractUserAddMessageText).find((text) => text.trim().length > 0);
    const failedEditCommand = failedCommands.find((command) => getUserAddMessageSourceId(command) !== null);
    if (failedText && failedEditCommand) {
      const sourceId = getUserAddMessageSourceId(failedEditCommand);
      const restored = sourceId !== null
        && composerRestoreRef.current?.restoreEditMessage(sourceId, failedText) === true;
      if (!restored) {
        setIssue({ message: "编辑重跑失败，原消息仍保留，请重新点击编辑重试。", retryable: true });
      }
    } else if (failedText) {
      composerRestoreRef.current?.restoreNewMessage(failedText);
    }
    const transportError = parseTransportError(error);
    const nextIssue: TransportIssue = {
      message: transportError?.message ?? safeFrontendErrorMessage(error, "网络异常，请检查本机后端是否正在运行"),
      retryable: transportError?.retryable ?? true,
    };
    lastTransportErrorRef.current = nextIssue;
    setIssue(nextIssue);

    // 重新读取服务端 snapshot 仅用于恢复 runtime 的本地渲染基线，不把任何 UI
    // state 写回后端，也不把本地错误伪装成 canonical message。
    try {
      const snapshot = parseTransportState(await requestJson<unknown>(`/tasks/${taskId}/assistant/state`));
      params.updateState(() => snapshot);
    } catch {
      // 原始传输错误已经可见，恢复失败不覆盖它。
    }
  }, [taskId, setIssue, traceId]);

  const reconcileAfterTransportFinish = useCallback(async () => {
    try {
      void frontendLog("INFO", "assistant_transport_reconcile_started", "Assistant Transport 流结束后读取最新快照", {
        traceId,
        data: { taskId, lastRunId: latestStateRef.current.current_run_id, lastRunStatus: currentTransportRun(latestStateRef.current)?.status ?? null },
      });
      const snapshot = parseTransportState(await requestJson<unknown>(`/tasks/${taskId}/assistant/state`));
      latestStateRef.current = snapshot;
      void frontendLog("INFO", "assistant_transport_reconcile_completed", "Assistant Transport 最新快照已读取", {
        traceId,
        data: {
          taskId,
          runId: snapshot.current_run_id,
          runStatus: currentTransportRun(snapshot)?.status ?? null,
          messageCount: transportMessageCount(snapshot),
        },
      });
      if (currentTransportRun(snapshot)?.status === "pending" || currentTransportRun(snapshot)?.status === "running") {
        // resumeApi is the transport-only attach endpoint. It must never be
        // confused with the user-triggered business resume below.
        runtimeControlsRef.current?.resume();
        return;
      }

      // Rebase terminal canonical state into the runtime so it cannot repeatedly
      // interpret stale local pending/running state as active.
      runtimeControlsRef.current?.importState(snapshot);
      setIssue(null);
    } catch (error) {
      setIssue({
        message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
        retryable: true,
      });
    }
  }, [taskId, setIssue, traceId]);

  const resumeBusinessRun = useCallback(async () => {
    const runId = latestStateRef.current.current_run_id;
    if (runId == null) throw new Error("当前没有可恢复的运行");
    const response = await fetch(`${backendBaseUrl}/assistant`, {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Trace-Id": traceId,
      },
      body: JSON.stringify({
        commands: [],
        taskId,
        threadId: `task-${taskId}`,
        runId,
        ...(workspaceId != null ? { workspaceId } : {}),
      }),
    });
    if (!response.ok) {
      throw new Error(`继续运行失败（HTTP ${response.status}）`);
    }
    // The business resume request starts the backend executor. Its stream is
    // deliberately discarded; the Assistant runtime attaches through the
    // transport-only endpoint below, so business resume and UI subscription
    // have independent lifecycles.
    await response.body?.cancel();
    runtimeControlsRef.current?.resume();
  }, [backendBaseUrl, taskId, traceId, workspaceId]);

  const runtime = useTaskAssistantTransportRuntime(taskId, {
    initialState,
    protocol: "assistant-transport",
    capabilities: { edit: true },
    api: `${backendBaseUrl}/assistant`,
    resumeApi: `${backendBaseUrl}/tasks/${taskId}/assistant/attach`,
    headers: async () => ({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Trace-Id": traceId,
    }),
    body: async () => {
      const selection = readStoredSelection({ kind: "task", id: taskId });
      const completeSelection = selection?.providerId !== undefined && selection.modelName !== undefined
        ? {
            providerId: selection.providerId,
            modelName: selection.modelName,
            reasoningEffort: selection.reasoningEffort ?? null,
          }
        : null;
      return {
        taskId,
        threadId: `task-${taskId}`,
        ...(workspaceId != null ? { workspaceId } : {}),
        ...selectionToTransportFields(completeSelection),
      };
    },
    prepareSendCommandsRequest: (body) => {
      // assistant-ui 的 request body 会带上本地 state。后端契约 extra=forbid，
      // 所以只能在这个唯一发送边界剥离它；前端不会把 snapshot 当成事实回传。
      const backendRequest = Object.fromEntries(
        Object.entries(body).filter(([key]) => key !== "state"),
      );
      const contextModel = modelContextToTransportFields(
        body.config,
        getModelCatalogSnapshot().catalog,
      );
      if (contextModel) {
        delete backendRequest.config;
        delete backendRequest.providerId;
        delete backendRequest.modelName;
        delete backendRequest.reasoningEffort;
        Object.assign(backendRequest, contextModel);
      }
      // assistant-ui only adds runId automatically when resumeStateApi is used.
      // This project keeps the canonical state endpoint separate, so the
      // transport-only resumeApi must receive the current run identity here.
      // An empty command batch is reserved for runtime resume; user-triggered
      // business resume uses the explicit request below instead.
      if (body.commands.length === 0 && latestStateRef.current.current_run_id !== null) {
        backendRequest.runId = latestStateRef.current.current_run_id;
      }
      lastTransportErrorRef.current = null;
      const commands = body.commands.map((command) => {
        const key = command as object;
        const commandId = getOrCreateTransportCommandId(key);
        return command.type === "add-message" ? { ...command, commandId } : command;
      });
      const hasEditCommand = commands.some((command) => getUserAddMessageSourceId(command) !== null);
      const requestRunId = commands.length === 0 || hasEditCommand
        ? latestStateRef.current.current_run_id
        : null;
      void frontendLog("INFO", "assistant_transport_request_prepared", "Assistant Transport 请求已准备发送", {
        traceId,
        data: {
          taskId,
          threadId: `task-${taskId}`,
          commandCount: commands.length,
          runId: requestRunId,
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
        ...(requestRunId != null ? { runId: requestRunId } : {}),
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
      lastTransportErrorRef.current = null;
      setIssue(null);
    },
    onFinish: () => {
      finishCountRef.current += 1;
      const runId = latestStateRef.current.current_run_id;
      const status = currentTransportRun(latestStateRef.current)?.status;
      const cancellationRequested = runId !== null && cancelRequestedRunIdRef.current === runId;
      void frontendLog("INFO", "assistant_transport_stream_finished", "Assistant Transport 流生命周期结束", {
        traceId,
        data: {
          taskId,
          finishCount: finishCountRef.current,
          runId,
          runStatus: status,
          messageCount: transportMessageCount(latestStateRef.current),
          pendingCommandCount: 0,
          isSending: false,
          pendingCommandTypes: [],
        },
      });
      if (cancellationRequested) {
        // Explicit cancellation is completed by the backend's pushed terminal
        // snapshot. Do not treat the client-side stream close as an EOF that
        // needs a second /assistant/state read.
        lastTransportErrorRef.current = null;
        setIssue(null);
        onTaskStateChanged?.();
        return;
      }
      if (lastTransportErrorRef.current) {
        // Assistant UI also invokes onFinish after a failed HTTP response.
        // Preserve the structured backend error, but still re-read and attach
        // when the backend says the run remains active. Transport failure is
        // not business cancellation, so this path never calls /assistant's
        // cancelled-only resume operation.
        setIssue(lastTransportErrorRef.current);
        onTaskStateChanged?.();
        if (currentTransportRun(latestStateRef.current)?.status === "pending" || currentTransportRun(latestStateRef.current)?.status === "running") {
          void reconcileAfterTransportFinish();
        }
        return;
      }
      const terminal = status === "completed"
        || status === "failed"
        || status === "cancelled"
        || status === "interrupted";
      if (!terminal) {
        setIssue({ message: "连接暂时中断，正在从本机后端恢复最新状态…", retryable: true });
        // EOF 不是完成信号：保留同一个 runtime，并让 Assistant UI 使用
        // transport-only resumeApi 重新订阅原 run。不能通过 key 重建 runtime，
        // 否则旧 runtime 的 AbortController 会主动取消当前请求生命周期。
        void reconcileAfterTransportFinish();
      } else {
        setIssue(null);
        onTaskStateChanged?.();
      }
    },
    onError: handleSendError,
    onCancel: ({ error, commands }) => {
      void frontendLog("WARNING", "assistant_transport_stream_cancelled", "Assistant Transport 请求被取消", {
        traceId,
        data: {
          taskId,
          commandCount: commands?.length ?? 0,
          runId: latestStateRef.current.current_run_id,
          runStatus: currentTransportRun(latestStateRef.current)?.status ?? null,
        },
        error,
      });
      if (error) {
        const currentRunId = latestStateRef.current.current_run_id;
        if (cancelRequestedRunIdRef.current === currentRunId && currentRunId !== null) {
          return;
        }
        setIssue({
          message: safeFrontendErrorMessage(error, "发送已取消，后端仍在确认运行状态"),
          retryable: true,
        });
      }
    },
    converter: (state, connectionMetadata) => {
      return toTransportThreadView(state, connectionMetadata);
    },
  });

  return (
      <AssistantRuntimeProvider runtime={runtime}>
      <RuntimeControlBridge
        register={registerRuntimeControls}
        resumeOnMount={currentTransportRun(initialState)?.status === "pending" || currentTransportRun(initialState)?.status === "running"}
        taskId={taskId}
      />
      <TransportStateCommitBridge initialState={initialState} onCommit={commitTransportState} />
      <ComposerRestoreBridge register={registerComposerRestore} />
      <TaskStateBridge
        onRunStateChange={onRunStateChange}
      />
      <RuntimeRenderDiagnostics taskId={taskId} />
      <div className="flex h-full min-h-0 flex-col">
        <Thread
          taskId={taskId}
          forkAvailable={forkAvailable}
          forkingRunId={forkingRunId}
          onForkRun={onForkRun}
          onResumeBusiness={resumeBusinessRun}
          onCancelRequested={handleCancelRequested}
          onCancelResult={handleCancelResult}
        />
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

type ComposerRestore = {
  restoreNewMessage: (text: string) => void;
  restoreEditMessage: (sourceId: string, text: string) => boolean;
};

function TransportStateCommitBridge({ initialState, onCommit }: { initialState: TransportState; onCommit: (state: TransportState) => void }) {
  const state = useAuiState((runtimeState) => runtimeState.thread.state);

  useEffect(() => {
    onCommit(isTransportState(state) ? state : initialState);
  }, [initialState, onCommit, state]);

  return null;
}

function isTransportState(value: unknown): value is TransportState {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { runs?: unknown; current_run_id?: unknown };
  return Array.isArray(candidate.runs)
    && (candidate.current_run_id === null || typeof candidate.current_run_id === "number");
}

function markTransportStateCancelled(state: TransportState, runId: number): TransportState {
  const runIndex = state.runs.findIndex((run) => run.runId === runId);
  if (runIndex < 0) return state;
  const runs = state.runs.map((candidate, index) => index === runIndex
    ? {
      ...candidate,
      status: "cancelled",
      endReason: "user_cancelled",
      messages: candidate.messages.map((message) => ({
        ...message,
        parts: message.parts.map((part) => (
          part.type === "tool-call" && (part.status === "pending" || part.status === "running")
            ? { ...part, status: "cancelled", error: "已取消", isError: false }
            : part
        )),
      })),
    }
    : candidate);
  return {
    ...state,
    runs,
    current_run_id: runId,
  };
}

function TaskStateBridge({
  onRunStateChange,
}: Pick<AssistantRuntimeProps, "onRunStateChange">) {
  const isRunning = useAuiState((state) => state.thread.isRunning);

  useEffect(() => {
    onRunStateChange?.(isRunning);
  }, [isRunning, onRunStateChange]);

  return null;
}

function RuntimeRenderDiagnostics({ taskId }: { taskId: number }) {
  const messageCount = useAuiState((state) => state.thread.messages.length);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const draftLength = useAuiState((state) => state.composer.text.length);
  const runId = useAuiState((state) => {
    const run = readRuntimeRun(state.thread.state);
    return typeof run?.runId === "number" ? run.runId : null;
  });
  const runStatus = useAuiState((state) => {
    const run = readRuntimeRun(state.thread.state);
    return typeof run?.status === "string" ? run.status : "unknown";
  });

  useEffect(() => {
    void frontendLog("DEBUG", "assistant_ui_render_state", "Assistant UI 渲染状态发生变化", {
      data: { taskId, messageCount, runId, runStatus, isRunning, draftLength },
    });
  }, [draftLength, isRunning, messageCount, runId, runStatus, taskId]);

  return null;
}

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
      || transportMessageCount(initialState) > 0
      || (currentTransportRun(initialState)?.status ?? "idle") !== "idle"
    ) return;
    aui.thread.composer().setText(text);
    const timer = window.setTimeout(() => {
      if (sentRef.current) return;
      sentRef.current = true;
      aui.thread.composer().send();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [aui, initialState, remoteThreadId, sentRef, taskId, text]);

  return null;
}

function ComposerRestoreBridge({ register }: { register: (restore: ComposerRestore) => void }) {
  const aui = useAui();
  useEffect(() => {
    register({
      restoreNewMessage: (text) => aui.thread.composer().setText(text),
      restoreEditMessage: (sourceId, text) => {
        try {
          const composer = aui.thread.message({ id: sourceId }).composer();
          if (!composer.getState().isEditing) composer.beginEdit();
          composer.setText(text);
          return true;
        } catch {
          return false;
        }
      },
    });
  }, [aui, register]);
  return null;
}
