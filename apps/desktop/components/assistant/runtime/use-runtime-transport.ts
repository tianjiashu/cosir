import { useCallback, useEffect, useMemo, useRef } from "react";

import {
  extractUserAddMessageAttachments,
  extractUserAddMessageText,
  getUserAddMessageSourceId,
  type UserAddMessageCommand,
} from "@/lib/assistant/converter";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import {
  editQueuedTaskMessage,
  rerunTaskMessage,
  sendTaskMessage,
  type TaskMessageRequest,
} from "@/lib/assistant/task-commands";
import { isQueuedUserMessage } from "@/lib/assistant/conversation-actions";
import { HttpError } from "@/lib/http/errors";
import { readStoredInputDelivery } from "@/lib/input-delivery-storage";
import { activeRunLaunch } from "@/lib/assistant/run-launch";
import { readStoredSelection } from "@/lib/model-selection-storage";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TransportState } from "@/lib/assistant/contract";
import { currentTransportRun, transportMessageCount } from "@/lib/assistant/transport-state-operations";
import type { TransportIssue } from "@/components/assistant/transport-status";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import { useTaskAssistantTransportRuntime } from "@/lib/assistant/use-task-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";

/** 一次 composer 提交解析出的 Task 命令：发送、原地编辑排队消息或编辑重跑。 */
type PreparedTaskCommand =
  | { kind: "send"; request: TaskMessageRequest }
  | { kind: "edit_queued"; messageId: string }
  | { kind: "rerun"; messageId: string; request: TaskMessageRequest };

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

  const handleSendError = useCallback(async (
    error: Error,
    params: {
      commands: readonly unknown[];
      phase: "request-rejected" | "accepted-stream-failed";
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

    if (params.phase === "request-rejected") {
      params.clearPendingCommands();
    }

    const failedCommands = [...params.commands].reverse();
    const failedEditCommand = failedCommands.find((command) => getUserAddMessageSourceId(command) !== null);
    const failedCommand = failedEditCommand ?? failedCommands.find((command) => {
      return extractUserAddMessageText(command).trim().length > 0
        || extractUserAddMessageAttachments(command).length > 0;
    });
    const failedText = failedCommand ? extractUserAddMessageText(failedCommand) : "";
    const failedAttachments = failedCommand ? extractUserAddMessageAttachments(failedCommand) : [];
    if (params.phase === "request-rejected" && failedCommand && failedEditCommand && (failedText.trim() || failedAttachments.length > 0)) {
      const sourceId = getUserAddMessageSourceId(failedEditCommand);
      const restored = sourceId !== null
        && context.composerRestoreRef.current?.restoreEditMessage(sourceId, failedText, failedAttachments) === true;
      if (!restored) {
        context.setIssue({ message: "编辑重跑失败，原消息仍保留，请重新点击编辑重试。", canResync: true });
      }
    } else if (params.phase === "request-rejected" && failedCommand && (failedText.trim() || failedAttachments.length > 0)) {
      context.composerRestoreRef.current?.restoreNewMessage(failedText, failedAttachments);
    }

    // 按意图而不是按 HTTP 阶段分流：没有命令的请求是 attach / 续订已有 Run，它失败只说明
    // 订阅没建立起来，必须回到 canonical snapshot 对账；只有提交命令被拒才需要弹窗并恢复输入。
    const attachOnly = params.commands.length === 0;
    const commandRejected = params.phase === "request-rejected" && !attachOnly;
    const nextIssue: TransportIssue = {
      message: userErrorMessage(error, "网络异常，请检查本机后端是否正在运行"),
      // HTTP 错误体的 retryable 表示“修正条件后能否重试”，这里映射为能否重新同步。
      canResync: error instanceof HttpError ? error.retryable ?? true : true,
      presentation: commandRejected ? "dialog" : "status",
    };
    context.lastTransportErrorRef.current = nextIssue;
    context.setIssue(nextIssue);

    if (commandRejected) return;

    errorSnapshotAbortControllerRef.current?.abort();
    const generation = ++errorSnapshotGenerationRef.current;
    const controller = new AbortController();
    errorSnapshotAbortControllerRef.current = controller;
    try {
      const outcome = await fetchCanonicalSnapshot(context.taskId, {
        reason: attachOnly ? "transport_attach_failed" : "transport_stream_error",
        timeoutMs: ERROR_SNAPSHOT_TIMEOUT_MS,
        traceId: context.traceId,
        signal: controller.signal,
      });
      if (outcome.kind === "superseded" || generation !== errorSnapshotGenerationRef.current) return;
      const snapshot = outcome.snapshot;
      context.latestStateRef.current = snapshot;
      params.updateState(() => snapshot);
      if (!attachOnly) return;
      // attach 失败后：Run 已结束则快照即最终结果；仍在运行则回到有界 recovery 重新订阅，
      // 不能重新进入 business-resume 的 POST /tasks/{id}/resume 请求。
      const run = currentTransportRun(snapshot);
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
    baseUrl: context.backendBaseUrl,
    headers: async () => ({
      Accept: "text/event-stream",
      "X-Trace-Id": context.traceId,
    }),
    submitMessage: async (command) => {
      errorSnapshotGenerationRef.current += 1;
      errorSnapshotAbortControllerRef.current?.abort();
      const sourceId = getUserAddMessageSourceId(command);
      const state = context.latestStateRef.current;
      // 编辑按消息是否仍在排队分两个命令：排队消息原地改（配置由后端沿用），已并入的重跑出新 Run。
      // 排队状态以后端 snapshot 投影为准；编辑期间恰被并入时后端 409，不会静默转成重跑。
      const queuedId = sourceId !== null && isQueuedUserMessage(state, sourceId) ? sourceId : null;
      // 运行期配置锁定：活动 Run 期间发送用它冻结的配置（composer 也只显示这份）；重跑不受锁定——
      // 它会开新 Run，按用户当前选择。
      const runLaunch = sourceId === null ? activeRunLaunch(state) : null;
      const selection = readStoredSelection({ kind: "task", id: context.taskId });
      const launch = runLaunch ?? {
        modelConfigId: selection?.modelConfigId ?? null,
        reasoningEffort: selection?.reasoningEffort ?? null,
        banTools: [...selectedBanTools],
      };
      // prepareUserCommand 已剔除 composer-only 的 file part，并为普通文件补齐带路径的描述符。
      const prepared = prepareUserCommand(command) as UserAddMessageCommand;
      const message = prepared.message as TaskMessageRequest["message"];
      let taskCommand: PreparedTaskCommand;
      if (queuedId !== null) {
        taskCommand = { kind: "edit_queued", messageId: queuedId };
      } else {
        if (launch.modelConfigId === null) {
          throw new UserFacingError(runLaunch ? "当前运行的模型配置已删除，请停止后重新选择" : "请先选择模型后再发送");
        }
        const request: TaskMessageRequest = {
          message,
          modelConfigId: launch.modelConfigId,
          reasoningEffort: launch.reasoningEffort,
          banTools: launch.banTools,
        };
        taskCommand = sourceId === null
          ? { kind: "send", request }
          : { kind: "rerun", messageId: sourceId, request };
      }
      const delivery = readStoredInputDelivery();
      // 新用户命令开始新的 Transport recovery 预算。
      recovery.resetTransportRecoveryBudget();
      context.lastTransportErrorRef.current = null;
      void frontendLog("INFO", "assistant_task_command_prepared", "Task 命令已准备发送", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          operation: taskCommand.kind,
          delivery: taskCommand.kind === "send" ? delivery : null,
          launchLocked: runLaunch !== null,
          partCount: message.parts.length,
          attachmentCount: message.attachments?.length ?? 0,
        },
      });
      const init = { traceId: context.traceId };
      const accepted = taskCommand.kind === "edit_queued"
        ? await editQueuedTaskMessage(context.taskId, taskCommand.messageId, message, init)
        : taskCommand.kind === "send"
          ? await sendTaskMessage(context.taskId, { ...taskCommand.request, delivery }, init)
          : await rerunTaskMessage(context.taskId, taskCommand.messageId, taskCommand.request, init);
      clearRecoveryIssueTimer();
      context.setIssue(null);
      return accepted;
    },
    onResponse: (response) => {
      void frontendLog("INFO", "assistant_transport_response_received", "Assistant Transport 已收到响应头", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          status: response.status,
          contentType: response.headers.get("content-type"),
          taskHeader: response.headers.get("x-cosir-task-id"),
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
