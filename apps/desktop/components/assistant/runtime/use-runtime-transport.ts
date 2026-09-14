import { useCallback, useEffect, useMemo, useRef } from "react";

import { requestJson } from "@/lib/http/client";
import {
  extractUserAddMessageAttachments,
  extractUserAddMessageText,
  getOrCreateTransportCommandId,
  getUserAddMessageSourceId,
} from "@/lib/assistant/converter";
import {
  createTransportViewConverter,
  type TransportViewConverter,
} from "@/lib/assistant/transport-view-converter";
import { modelContextToTransportFields, selectionToTransportFields } from "@/lib/assistant/model-request-adapter";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import { parseTransportError } from "@/lib/assistant/transport-error";
import { getModelCatalogSnapshot } from "@/lib/model-catalog";
import { readStoredSelection } from "@/lib/model-selection-storage";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import type { TransportState } from "@/lib/assistant/contract";
import { currentTransportRun, transportMessageCount } from "@/lib/assistant/transport-state-operations";
import type { TransportIssue } from "@/components/assistant/transport-status";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import { useTaskAssistantTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";

type RuntimeTransport = ReturnType<typeof useTaskAssistantTransportRuntime>;
const RECOVERY_FEEDBACK_DELAY_MS = 350;
const ERROR_SNAPSHOT_TIMEOUT_MS = 15_000;
const TERMINAL_SNAPSHOT_TIMEOUT_MS = 3_000;

function isTerminalRunStatus(status: string | undefined): boolean {
  return status === "idle"
    || status === "completed"
    || status === "failed"
    || status === "cancelled"
    || status === "interrupted";
}

/** Build the Assistant Transport runtime and keep transport-only policy here. */
export function useRuntimeTransport(
  context: RuntimeSessionContext,
  recovery: RuntimeRecovery,
): RuntimeTransport {
  const attachmentAdapter = useMemo(
    () => createAttachmentAdapter(context.taskId),
    [context.taskId],
  );
  const finishCountRef = useRef(0);
  const recoveryIssueTimerRef = useRef<number | null>(null);
  const errorSnapshotAbortControllerRef = useRef<AbortController | null>(null);
  const errorSnapshotGenerationRef = useRef(0);
  const transportViewConverterRef = useRef<{
    taskId: number;
    converter: TransportViewConverter;
  } | null>(null);
  if (transportViewConverterRef.current?.taskId !== context.taskId) {
    transportViewConverterRef.current = {
      taskId: context.taskId,
      converter: createTransportViewConverter(),
    };
  }
  const transportViewConverter = transportViewConverterRef.current.converter;

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
        context.setIssue({ message: "连接暂时中断，正在从本机后端恢复最新状态…", retryable: true });
      }
    }, RECOVERY_FEEDBACK_DELAY_MS);
  }, [clearRecoveryIssueTimer, context]);

  useEffect(() => () => {
    clearRecoveryIssueTimer();
    errorSnapshotGenerationRef.current += 1;
    errorSnapshotAbortControllerRef.current?.abort();
  }, [clearRecoveryIssueTimer]);

  const handleSendError = useCallback(async (
    error: Error,
    params: {
      commands: readonly unknown[];
      updateState: (updater: (state: TransportState) => TransportState) => void;
    },
  ) => {
    const currentRunId = context.latestStateRef.current.current_run_id;
    if (context.cancelRequestedRunIdRef.current === currentRunId && currentRunId !== null) {
      context.lastTransportErrorRef.current = null;
      context.setIssue(null);
      return;
    }

    void frontendLog("ERROR", "assistant_transport_stream_error", "Assistant Transport 流发生错误", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        commandCount: params.commands.length,
        lastRunId: context.latestStateRef.current.current_run_id,
        lastRunStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
      },
      error,
    });

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
        context.setIssue({ message: "编辑重跑失败，原消息仍保留，请重新点击编辑重试。", retryable: true });
      }
    } else if (failedCommand && (failedText.trim() || failedAttachments.length > 0)) {
      context.composerRestoreRef.current?.restoreNewMessage(failedText, failedAttachments);
    }

    const transportError = parseTransportError(error);
    const nextIssue: TransportIssue = {
      message: transportError?.message ?? safeFrontendErrorMessage(error, "网络异常，请检查本机后端是否正在运行"),
      retryable: transportError?.retryable ?? true,
    };
    context.lastTransportErrorRef.current = nextIssue;
    context.setIssue(nextIssue);

    errorSnapshotAbortControllerRef.current?.abort();
    const controller = new AbortController();
    const generation = ++errorSnapshotGenerationRef.current;
    const timeout = globalThis.setTimeout(() => controller.abort(), ERROR_SNAPSHOT_TIMEOUT_MS);
    errorSnapshotAbortControllerRef.current = controller;
    try {
      const snapshot = parseTransportState(await requestJson<unknown>(`/tasks/${context.taskId}/assistant/state`, {
        signal: controller.signal,
        traceId: context.traceId,
      }));
      if (controller.signal.aborted || generation !== errorSnapshotGenerationRef.current) return;
      params.updateState(() => snapshot);
    } catch {
      // Preserve the original transport issue when recovery also fails.
    } finally {
      globalThis.clearTimeout(timeout);
      if (errorSnapshotAbortControllerRef.current === controller) {
        errorSnapshotAbortControllerRef.current = null;
      }
    }
  }, [context]);

  const reconcileTerminalSnapshot = useCallback(async (expectedRunId: number): Promise<boolean> => {
    const currentRun = currentTransportRun(context.latestStateRef.current);
    if (currentRun?.runId !== expectedRunId || isTerminalRunStatus(currentRun.status)) return false;

    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), TERMINAL_SNAPSHOT_TIMEOUT_MS);
    try {
      const snapshot = parseTransportState(await requestJson<unknown>(`/tasks/${context.taskId}/assistant/state`, {
        signal: controller.signal,
        traceId: context.traceId,
      }));
      const snapshotRun = currentTransportRun(snapshot);
      const latestRun = currentTransportRun(context.latestStateRef.current);
      if (
        controller.signal.aborted
        || snapshotRun?.runId !== expectedRunId
        || latestRun?.runId !== expectedRunId
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
      return false;
    } finally {
      globalThis.clearTimeout(timeout);
    }
  }, [context]);

  return useTaskAssistantTransportRuntime(context.taskId, {
    initialState: context.initialState,
    protocol: "assistant-transport",
    capabilities: { edit: true },
    adapters: { attachments: attachmentAdapter },
    api: `${context.backendBaseUrl}/assistant`,
    resumeApi: `${context.backendBaseUrl}/tasks/${context.taskId}/assistant/attach`,
    headers: async () => ({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Trace-Id": context.traceId,
    }),
    body: async () => {
      const selection = readStoredSelection({ kind: "task", id: context.taskId });
      const completeSelection = selection?.providerId !== undefined && selection.modelName !== undefined
        ? {
            providerId: selection.providerId,
            modelName: selection.modelName,
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
        delete backendRequest.providerId;
        delete backendRequest.modelName;
        delete backendRequest.reasoningEffort;
        Object.assign(backendRequest, contextModel);
      }

      if (body.commands.length === 0 && context.latestStateRef.current.current_run_id !== null) {
        backendRequest.runId = context.latestStateRef.current.current_run_id;
      }

      const commands = body.commands.map((command) => {
        const key = command as object;
        const commandId = getOrCreateTransportCommandId(key);
        const prepared = prepareUserCommand(command);
        return prepared && typeof prepared === "object" && "type" in prepared && prepared.type === "add-message"
          ? { ...prepared, commandId }
          : prepared;
      });
      // A new user command starts a fresh transport recovery budget. Empty
      // command batches are attach/resume operations and must not reset the
      // bounded EOF protection.
      if (commands.length > 0) recovery.resetRecovery();
      const hasEditCommand = commands.some((command) => getUserAddMessageSourceId(command) !== null);
      const requestRunId = commands.length === 0 || hasEditCommand
        ? context.latestStateRef.current.current_run_id
        : null;

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
    },
    onFinish: () => {
      void (async () => {
        finishCountRef.current += 1;
        const initialRunId = context.latestStateRef.current.current_run_id;
        const initialStatus = currentTransportRun(context.latestStateRef.current)?.status;
        const reconciled = initialRunId !== null
          && !isTerminalRunStatus(initialStatus)
          ? await reconcileTerminalSnapshot(initialRunId)
          : false;
        const runId = context.latestStateRef.current.current_run_id;
        const status = currentTransportRun(context.latestStateRef.current)?.status;
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
          if (status === "pending" || status === "running") void recovery.reconcileAfterTransportFinish();
          return;
        }

        if (!isTerminalRunStatus(status)) {
          scheduleRecoveryIssue();
          void recovery.reconcileAfterTransportFinish();
        } else {
          clearRecoveryIssueTimer();
          context.setIssue(null);
          context.onTaskStateChanged?.();
        }
      })();
    },
    onError: handleSendError,
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
          message: safeFrontendErrorMessage(error, "发送已取消，后端仍在确认运行状态"),
          retryable: true,
        });
      }
    },
    converter: transportViewConverter,
  });
}
