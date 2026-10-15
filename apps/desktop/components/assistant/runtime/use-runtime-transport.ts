import { useCallback, useEffect, useMemo, useRef } from "react";

import {
  extractUserAddMessageAttachments,
  extractUserAddMessageText,
  getOrCreateTransportCommandId,
  getUserAddMessageSourceId,
} from "@/lib/assistant/converter";
import { modelContextToTransportFields, selectionToTransportFields } from "@/lib/assistant/model-request-adapter";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import { HttpError } from "@/lib/http/errors";
import { getModelCatalogSnapshot } from "@/lib/model-catalog";
import { readStoredSelection } from "@/lib/model-selection-storage";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TransportState } from "@/lib/assistant/contract";
import { currentTransportRun, transportMessageCount } from "@/lib/assistant/transport-state-operations";
import type { TransportIssue } from "@/components/assistant/transport-status";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import { resolveTransportFailurePolicy } from "@/components/assistant/runtime/transport-failure-policy";
import {
  useTaskAssistantTransportRuntime,
  type TransportFailure,
} from "@/lib/assistant/use-task-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";
import { userErrorMessage } from "@/lib/errors/user-error-message";

type RuntimeTransport = ReturnType<typeof useTaskAssistantTransportRuntime>;
const RECOVERY_FEEDBACK_DELAY_MS = 350;
const ERROR_SNAPSHOT_TIMEOUT_MS = 15_000;
const TERMINAL_SNAPSHOT_TIMEOUT_MS = 3_000;

function isTerminalRunStatus(status: string | null | undefined): boolean {
  return status === "idle"
    || status === "completed"
    || status === "failed"
    || status === "cancelled"
    || status === "interrupted";
}

function findTransportRun(state: TransportState, runId: number | null) {
  return runId === null ? undefined : state.runs.find((run) => run.runId === runId);
}

/** 创建 Assistant Transport runtime，并集中承载只属于 Transport 的策略。 */
export function useRuntimeTransport(
  context: RuntimeSessionContext,
  recovery: RuntimeRecovery,
  onStateCommit: (state: TransportState) => void,
  selectedBanTools: readonly string[],
): RuntimeTransport {
  const attachmentAdapter = useMemo(
    () => context.workspaceId == null ? null : createAttachmentAdapter(context.workspaceId),
    [context.workspaceId],
  );
  const finishCountRef = useRef(0);
  const recoveryIssueTimerRef = useRef<number | null>(null);
  const errorSnapshotAbortControllerRef = useRef<AbortController | null>(null);
  const errorSnapshotGenerationRef = useRef(0);
  const onAttachReady = useCallback((attach: (() => Promise<void>) | null) => {
    context.attachTransportRef.current = attach;
  }, [context.attachTransportRef]);
  const clearRecoveryIssueTimer = useCallback(() => {
    if (recoveryIssueTimerRef.current === null) return;
    window.clearTimeout(recoveryIssueTimerRef.current);
    recoveryIssueTimerRef.current = null;
  }, []);

  const scheduleRecoveryIssue = useCallback(() => {
    clearRecoveryIssueTimer();
    recoveryIssueTimerRef.current = window.setTimeout(() => {
      recoveryIssueTimerRef.current = null;
      const status = currentTransportRun(context.latestStateRef.current)?.status;
      if (status !== "completed" && status !== "failed" && status !== "cancelled" && status !== "interrupted") {
        context.setIssue({ message: "连接暂时中断，正在从本机后端恢复最新状态…", canResync: true });
      }
    }, RECOVERY_FEEDBACK_DELAY_MS);
  }, [clearRecoveryIssueTimer, context]);

  useEffect(() => () => {
    clearRecoveryIssueTimer();
    errorSnapshotGenerationRef.current += 1;
    errorSnapshotAbortControllerRef.current?.abort();
  }, [clearRecoveryIssueTimer]);

  /**
   * 统一处理提交命令与 attach 的 Transport 失败，处置规则由 resolveTransportFailurePolicy 决定。
   *
   * 用户主动停止的 Run 不报错：中止 SSE 引起的失败属于预期路径，由取消确认负责收敛。
   * 读取快照有代号与 AbortController 保护，新请求准备发送或组件卸载时作废进行中的读取。
   */
  const handleTransportError = useCallback(async (
    error: Error,
    params: {
      commands: readonly unknown[];
      failure: TransportFailure;
      updateState: (updater: (state: TransportState) => TransportState) => void;
      clearPendingCommands: () => void;
    },
  ) => {
    const currentRunId = context.latestStateRef.current.current_run_id;
    if (context.cancelRequestedRunIdRef.current === currentRunId && currentRunId !== null) {
      context.lastTransportErrorRef.current = null;
      context.setIssue(null);
      return;
    }

    const policy = resolveTransportFailurePolicy(params.failure);
    void frontendLog("ERROR", "assistant_transport_stream_error", "Assistant Transport 流发生错误", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        request: params.failure.request,
        phase: params.failure.phase,
        commandCount: params.commands.length,
        lastRunId: currentRunId,
        lastRunStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
        presentation: policy.presentation,
        snapshot: policy.snapshot,
      },
      error,
    });

    if (policy.restoreComposer) {
      params.clearPendingCommands();
      const failedCommands = [...params.commands].reverse();
      const failedEditCommand = failedCommands.find((command) => getUserAddMessageSourceId(command) !== null);
      const failedCommand = failedEditCommand ?? failedCommands.find((command) => {
        return extractUserAddMessageText(command).trim().length > 0
          || extractUserAddMessageAttachments(command).length > 0;
      });
      const failedText = failedCommand ? extractUserAddMessageText(failedCommand) : "";
      const failedAttachments = failedCommand ? extractUserAddMessageAttachments(failedCommand) : [];
      if (failedCommand && failedEditCommand && (failedText.trim() || failedAttachments.length > 0)) {
        const sourceId = getUserAddMessageSourceId(failedEditCommand);
        const restored = sourceId !== null
          && context.composerRestoreRef.current?.restoreEditMessage(sourceId, failedText, failedAttachments) === true;
        if (!restored) {
          context.setIssue({ message: "编辑重跑失败，原消息仍保留，请重新点击编辑重试。", canResync: true });
        }
      } else if (failedCommand && (failedText.trim() || failedAttachments.length > 0)) {
        context.composerRestoreRef.current?.restoreNewMessage(failedText, failedAttachments);
      }
    }

    const nextIssue: TransportIssue = {
      message: userErrorMessage(error, "网络异常，请检查本机后端是否正在运行"),
      // HTTP 错误体的 retryable 表示“修正条件后能否重试”，这里映射为能否重新同步。
      canResync: error instanceof HttpError ? error.retryable ?? true : true,
      presentation: policy.presentation,
    };
    context.lastTransportErrorRef.current = nextIssue;
    context.setIssue(nextIssue);

    if (policy.snapshot === "none") return;

    errorSnapshotAbortControllerRef.current?.abort();
    const generation = ++errorSnapshotGenerationRef.current;
    const controller = new AbortController();
    errorSnapshotAbortControllerRef.current = controller;
    try {
      const outcome = await fetchCanonicalSnapshot(context.taskId, {
        reason: params.failure.request === "attach" ? "transport_attach_failed" : "transport_stream_error",
        timeoutMs: ERROR_SNAPSHOT_TIMEOUT_MS,
        traceId: context.traceId,
        signal: controller.signal,
      });
      if (outcome.kind === "superseded" || generation !== errorSnapshotGenerationRef.current) return;
      const snapshot = outcome.snapshot;
      context.latestStateRef.current = snapshot;
      params.updateState(() => snapshot);
      if (policy.snapshot !== "import-then-recover") return;
      // attach 失败后：Run 已结束则快照即最终结果；仍在运行则回到有界 recovery 重新订阅，
      // 不能重新进入 business-resume 的 POST /assistant 请求。
      const run = currentTransportRun(snapshot);
      void frontendLog("INFO", "assistant_transport_attach_failure_reconciled", "attach 失败后已导入 canonical snapshot", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          runId: run?.runId ?? null,
          runStatus: run?.status ?? null,
          recovering: run !== undefined && !isTerminalRunStatus(run.status),
        },
      });
      if (run && !isTerminalRunStatus(run.status)) {
        void recovery.reconcileAfterTransportFinish({ runId: run.runId, status: run.status });
      } else {
        context.lastTransportErrorRef.current = null;
        context.setIssue(null);
      }
    } catch {
      // 读取失败已由 fetchCanonicalSnapshot 记录；保留原始 Transport issue 供用户重新同步。
    } finally {
      if (errorSnapshotAbortControllerRef.current === controller) {
        errorSnapshotAbortControllerRef.current = null;
      }
    }
  }, [context, recovery]);

  const reconcileTerminalSnapshot = useCallback(async (expectedRunId: number): Promise<boolean> => {
    const currentRun = findTransportRun(context.latestStateRef.current, expectedRunId);
    if (
      currentRun?.runId !== expectedRunId
      || isTerminalRunStatus(currentRun.status)
      || context.cancelRequestedRunIdRef.current === expectedRunId
    ) return false;

    try {
      const outcome = await fetchCanonicalSnapshot(context.taskId, {
        reason: "transport_terminal_check",
        timeoutMs: TERMINAL_SNAPSHOT_TIMEOUT_MS,
        traceId: context.traceId,
      });
      if (outcome.kind === "superseded") return false;
      const snapshot = outcome.snapshot;
      const snapshotRun = findTransportRun(snapshot, expectedRunId);
      const latestRun = findTransportRun(context.latestStateRef.current, expectedRunId);
      if (
        snapshotRun?.runId !== expectedRunId
        || latestRun?.runId !== expectedRunId
        || context.cancelRequestedRunIdRef.current === expectedRunId
        || !isTerminalRunStatus(snapshotRun.status)
      ) return false;

      context.latestStateRef.current = snapshot;
      context.runtimeControlsRef.current?.importState(snapshot);
      void frontendLog("INFO", "assistant_transport_terminal_snapshot_reconciled", "Assistant Transport 终态快照已补偿提交", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          runId: expectedRunId,
          runStatus: snapshotRun.status,
        },
      });
      return true;
    } catch {
      // 终态补偿只是快路径；失败时交给 onFinish 后续的有界恢复。
      return false;
    }
  }, [context]);

  return useTaskAssistantTransportRuntime(context.taskId, {
    initialState: context.initialState,
    adapters: attachmentAdapter == null ? undefined : { attachments: attachmentAdapter },
    api: `${context.backendBaseUrl}/assistant`,
    resumeApi: `${context.backendBaseUrl}/tasks/${context.taskId}/assistant/attach`,
    headers: async () => ({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Trace-Id": context.traceId,
    }),
    body: async () => {
      const selection = readStoredSelection({ kind: "task", id: context.taskId });
      const completeSelection = selection?.modelConfigId !== undefined
        ? {
            modelConfigId: selection.modelConfigId,
            reasoningEffort: selection.reasoningEffort ?? null,
          }
        : null;
      return {
        taskId: context.taskId,
        threadId: `task-${context.taskId}`,
        ...(context.workspaceId != null ? { workspaceId: context.workspaceId } : {}),
        ...selectionToTransportFields(completeSelection),
      };
    },
    prepareSendCommandsRequest: (body) => {
      errorSnapshotGenerationRef.current += 1;
      errorSnapshotAbortControllerRef.current?.abort();
      const backendRequest = Object.fromEntries(
        Object.entries(body).filter(([key]) => key !== "state"),
      );
      const contextModel = modelContextToTransportFields(
        body.config,
        getModelCatalogSnapshot().catalog,
      );
      if (contextModel) {
        delete backendRequest.config;
        delete backendRequest.modelConfigId;
        delete backendRequest.reasoningEffort;
        Object.assign(backendRequest, contextModel);
      }

      const commands = body.commands.map((command) => {
        const key = command as object;
        const commandId = getOrCreateTransportCommandId(key);
        const prepared = prepareUserCommand(command);
        return prepared && typeof prepared === "object" && "type" in prepared && prepared.type === "add-message"
          ? { ...prepared, commandId }
          : prepared;
      });
      const addMessage = commands.find((command) => (
        typeof command === "object" && command !== null && "type" in command
        && command.type === "add-message"
      )) as { commandId?: string } | undefined;
      if (addMessage?.commandId) {
        commands.push({
          type: "custom",
          name: "ban-tools",
          commandId: `${addMessage.commandId}-ban-tools`,
          payload: { ban_tools: [...selectedBanTools] },
        });
      }
      // 本回调只服务提交用户命令（attach 走 resumeApi，不经过这里），新命令开始新的
      // Transport recovery 预算；编辑重跑需要指明被编辑的 Run，新消息由后端创建 Run。
      recovery.resetTransportRecoveryBudget();
      const hasEditCommand = commands.some((command) => getUserAddMessageSourceId(command) !== null);
      const requestRunId = hasEditCommand ? context.latestStateRef.current.current_run_id : null;

      context.lastTransportErrorRef.current = null;
      void frontendLog("INFO", "assistant_transport_request_prepared", "Assistant Transport 请求已准备发送", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          threadId: `task-${context.taskId}`,
          commandCount: commands.length,
          runId: requestRunId,
          commandTypes: commands.map((command) => (
            typeof command === "object" && command !== null && "type" in command
              ? command.type
              : "unknown"
          )),
          stateStripped: Object.prototype.hasOwnProperty.call(body, "state"),
          parentIdPresent: Object.prototype.hasOwnProperty.call(body, "parentId"),
        },
    });

      return {
        ...backendRequest,
        commands,
        taskId: context.taskId,
        threadId: `task-${context.taskId}`,
        ...(requestRunId != null ? { runId: requestRunId } : {}),
        ...(context.workspaceId != null ? { workspaceId: context.workspaceId } : {}),
      };
    },
    onResponse: (response) => {
      void frontendLog("INFO", "assistant_transport_response_received", "Assistant Transport 已收到响应头", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          status: response.status,
          contentType: response.headers.get("content-type"),
          taskHeader: response.headers.get("x-cosir-task-id"),
          threadHeader: response.headers.get("x-cosir-thread-id"),
          traceHeader: response.headers.get("x-trace-id"),
        },
      });
      context.lastTransportErrorRef.current = null;
      clearRecoveryIssueTimer();
      context.setIssue(null);
      // 只在 2xx 时回调：连接已建立，恢复预算按「连续失败」重新计数。
      recovery.resetTransportRecoveryBudget();
    },
    onFinish: ({ targetRunId, targetRunStatus }) => {
      void (async () => {
        finishCountRef.current += 1;
        const initialRunId = targetRunId;
        const initialStatus = targetRunStatus;
        const reconciled = initialRunId !== null
          && !isTerminalRunStatus(initialStatus)
          ? await reconcileTerminalSnapshot(initialRunId)
          : false;
        const runId = targetRunId;
        const status = runId === null
          ? null
          : findTransportRun(context.latestStateRef.current, runId)?.status ?? targetRunStatus;
        const cancellationRequested = runId !== null && context.cancelRequestedRunIdRef.current === runId;
        void frontendLog("INFO", "assistant_transport_stream_finished", "Assistant Transport 流生命周期结束", {
          traceId: context.traceId,
          data: {
            taskId: context.taskId,
            finishCount: finishCountRef.current,
            runId,
            runStatus: status,
            initialRunStatus: initialStatus,
            terminalSnapshotReconciled: reconciled,
            messageCount: transportMessageCount(context.latestStateRef.current),
            pendingCommandCount: 0,
            isSending: false,
            pendingCommandTypes: [],
          },
        });

        if (cancellationRequested) {
          context.lastTransportErrorRef.current = null;
          clearRecoveryIssueTimer();
          context.setIssue(null);
          context.onTaskStateChanged?.();
          return;
        }
        if (context.lastTransportErrorRef.current) {
          context.setIssue(context.lastTransportErrorRef.current);
          context.onTaskStateChanged?.();
          if (status === "pending" || status === "running") {
            void recovery.reconcileAfterTransportFinish({ runId, status });
          }
          return;
        }

        if (!isTerminalRunStatus(status)) {
          scheduleRecoveryIssue();
          void recovery.reconcileAfterTransportFinish({ runId, status });
        } else {
          clearRecoveryIssueTimer();
          context.setIssue(null);
          context.onTaskStateChanged?.();
        }
      })();
    },
    onError: handleTransportError,
    onCancel: ({ error, commands }) => {
      void frontendLog("WARNING", "assistant_transport_stream_cancelled", "Assistant Transport 请求被取消", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          commandCount: commands?.length ?? 0,
          runId: context.latestStateRef.current.current_run_id,
          runStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
        },
        error,
      });
      if (error) {
        const currentRunId = context.latestStateRef.current.current_run_id;
        if (context.cancelRequestedRunIdRef.current === currentRunId && currentRunId !== null) return;
        context.setIssue({
          message: userErrorMessage(error, "发送已取消，后端仍在确认运行状态"),
          canResync: true,
        });
      }
    },
    onAttachReady,
    onStateCommit,
    performanceProbe: context.performanceProbe,
  });
}
