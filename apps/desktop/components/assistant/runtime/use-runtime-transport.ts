import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";

import {
  extractUserAddMessageAttachments,
  extractUserAddMessageText,
  getUserAddMessageSourceId,
  type UserAddMessageCommand,
} from "@/lib/assistant/converter";
import {
  rerunTaskMessage,
  sendTaskMessage,
  type TaskMessageRequest,
} from "@/lib/assistant/task-commands";
import { showErrorDialog } from "@/lib/confirm/confirm-store";
import { activeRunLaunch } from "@/lib/assistant/run-launch";
import { readStoredSelection } from "@/lib/model-selection-storage";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TransportRunStatus, TransportState } from "@/lib/assistant/contract";
import { transportMessageCount } from "@/lib/assistant/transport-state-operations";
import type { RuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import type { RuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import {
  useTaskAssistantTransportRuntime,
  type TransportDisconnect,
} from "@/lib/assistant/use-task-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

/** 一次 composer 提交解析出的 Task 命令：发送新消息，或编辑重跑（带 `sourceId`）。 */
type PreparedTaskCommand =
  | { kind: "send"; request: TaskMessageRequest }
  | { kind: "rerun"; messageId: string; request: TaskMessageRequest };

type RuntimeTransport = ReturnType<typeof useTaskAssistantTransportRuntime>["runtime"];

/**
 * 被拒命令的草稿恢复：编辑命令回到编辑态，新消息回到输入框。HTTP 拒绝与 actor 拒绝（顶层
 * `error`）共用这一份规则。
 */
function restoreRejectedDraft(context: RuntimeSessionContext, commands: readonly unknown[]): void {
  const failedCommands = [...commands].reverse();
  const failedEditCommand = failedCommands.find((command) => getUserAddMessageSourceId(command) !== null);
  const failedCommand = failedEditCommand ?? failedCommands.find((command) => (
    extractUserAddMessageText(command).trim().length > 0
      || extractUserAddMessageAttachments(command).length > 0
  ));
  if (!failedCommand) return;
  const failedText = extractUserAddMessageText(failedCommand);
  const failedAttachments = extractUserAddMessageAttachments(failedCommand);
  if (!failedText.trim() && failedAttachments.length === 0) return;
  if (failedEditCommand) {
    const sourceId = getUserAddMessageSourceId(failedEditCommand);
    const restored = sourceId !== null
      && context.composerRestoreRef.current?.restoreEditMessage(sourceId, failedText, failedAttachments) === true;
    if (!restored) {
      context.setIssue({ message: "编辑失败，原消息仍保留，请重新点击编辑重试。", canResync: true });
    }
    return;
  }
  context.composerRestoreRef.current?.restoreNewMessage(failedText, failedAttachments);
}

/**
 * 创建 Assistant Transport runtime，并集中承载只属于 Transport 的策略：命令组装、命令被拒的
 * 草稿恢复、一轮结束的通知与订阅断开后的重连。
 */
export function useRuntimeTransport(
  context: RuntimeSessionContext,
  recovery: RuntimeRecovery,
  cancellation: Pick<RuntimeCancellation, "onSettled">,
  onStateCommit: (state: TransportState) => void,
  selectedToolGroups: readonly string[],
): RuntimeTransport {
  const attachmentAdapter = useMemo(
    () => context.workspaceId == null ? null : createAttachmentAdapter(context.workspaceId),
    [context.workspaceId],
  );
  // 订阅身份跟随本机后端实例：不可用时不订阅，换实例（generation 变化）时 runtime 自行重开。
  const backend = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const connectionKey = backend.available ? String(backend.generation) : null;

  /** 命令提交被拒或未能发出（HTTP 非 2xx、网络失败、附件失效）：弹窗提示并恢复草稿。 */
  const handleSubmitError = useCallback((error: Error, commands: readonly UserAddMessageCommand[]) => {
    void frontendLog("ERROR", "assistant_task_command_rejected", "Task 命令提交被拒", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        commandCount: commands.length,
        source: "http",
        activityStatus: context.latestStateRef.current.activity?.status ?? null,
      },
      error,
    });
    restoreRejectedDraft(context, commands);
    // 命令被拒要打断用户：草稿已恢复，用弹窗说明原因；连接类问题才进对话区顶部的状态条。
    void showErrorDialog({
      title: "发送失败",
      message: userErrorMessage(error, "网络异常，请检查本机后端是否正在运行"),
    });
  }, [context]);

  /** actor 拒绝命令：顶层 `error` 已在对话区显示，这里只恢复草稿并记日志。 */
  const handleActivitySettled = useCallback((status: TransportRunStatus) => {
    void frontendLog("INFO", "assistant_activity_settled", "Task 当前活动已结束", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        runId: context.latestStateRef.current.activity?.runId ?? null,
        status,
        cancelRequested: context.cancelRequestedRef.current,
        messageCount: transportMessageCount(context.latestStateRef.current),
      },
    });
    cancellation.onSettled();
    context.onTaskStateChanged?.();
  }, [cancellation, context]);

  const handleDisconnect = useCallback(({ error, resync }: TransportDisconnect) => {
    void frontendLog(error ? "WARNING" : "INFO", "assistant_transport_subscription_closed", "Task 长订阅意外结束", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        resync,
        activityStatus: context.latestStateRef.current.activity?.status ?? null,
      },
      error: error ?? undefined,
    });
    recovery.reconnect(resync ? "resync_required" : "disconnected");
  }, [context, recovery]);

  const { runtime, open } = useTaskAssistantTransportRuntime(context.taskId, {
    initialState: context.initialState,
    connectionKey,
    adapters: attachmentAdapter == null ? undefined : { attachments: attachmentAdapter },
    traceId: context.traceId,
    submitMessage: async (command) => {
      const sourceId = getUserAddMessageSourceId(command);
      const state = context.latestStateRef.current;
      // 运行期配置锁定：活动期间发送用它冻结的配置（composer 也只显示这份）；重跑不受锁定——
      // 它会开新 Run，按用户当前选择。
      const runLaunch = sourceId === null ? activeRunLaunch(state) : null;
      const selection = readStoredSelection({ kind: "task", id: context.taskId });
      const launch = runLaunch ?? {
        modelConfigId: selection?.modelConfigId ?? null,
        reasoningEffort: selection?.reasoningEffort ?? null,
        disabledToolGroups: [...selectedToolGroups],
      };
      // prepareUserCommand 已剔除 composer-only 的 file part，并为普通文件补齐带路径的描述符。
      const prepared = prepareUserCommand(command) as UserAddMessageCommand;
      const message = prepared.message as TaskMessageRequest["message"];
      if (launch.modelConfigId === null) {
        throw new UserFacingError(runLaunch ? "当前运行的模型配置已删除，请停止后重新选择" : "请先选择模型后再发送");
      }
      const request: TaskMessageRequest = {
        message,
        modelConfigId: launch.modelConfigId,
        reasoningEffort: launch.reasoningEffort,
        disabledToolGroups: launch.disabledToolGroups,
      };
      const taskCommand: PreparedTaskCommand = sourceId === null
        ? { kind: "send", request }
        : { kind: "rerun", messageId: sourceId, request };
      void frontendLog("INFO", "assistant_task_command_prepared", "Task 命令已准备发送", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          operation: taskCommand.kind,
          launchLocked: runLaunch !== null,
          partCount: message.parts.length,
          attachmentCount: message.attachments?.length ?? 0,
        },
      });
      const init = { traceId: context.traceId };
      if (taskCommand.kind === "send") {
        await sendTaskMessage(context.taskId, taskCommand.request, init);
      } else {
        await rerunTaskMessage(context.taskId, taskCommand.messageId, taskCommand.request, init);
      }
      context.setIssue(null);
    },
    onResponse: (response) => {
      void frontendLog("INFO", "assistant_transport_subscription_opened", "Task 长订阅已建立", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          status: response.status,
          contentType: response.headers.get("content-type"),
          taskHeader: response.headers.get("x-cosir-task-id"),
          traceHeader: response.headers.get("x-trace-id"),
        },
      });
      // 连接已建立：断流提示随之撤下；预算按「连续失败」计数，由 recovery 判断连接是否稳定。
      recovery.markConnected();
      context.setIssue(null);
    },
    onActivitySettled: handleActivitySettled,
    onDisconnect: handleDisconnect,
    onError: handleSubmitError,
    onCancel: ({ error, commands }) => {
      void frontendLog("WARNING", "assistant_transport_cancel_requested", "Assistant Transport 停止请求已处理", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          commandCount: commands.length,
          activityStatus: context.latestStateRef.current.activity?.status ?? null,
        },
        error,
      });
      if (error) {
        context.setIssue({ message: userErrorMessage(error, "停止请求失败，请重试"), canResync: true });
      }
    },
    onStateCommit,
    performanceProbe: context.performanceProbe,
  });
  // 有限重连与「重新同步」在会话层（runtime 创建之前就已存在），经这个 ref 拿到重开入口。
  const { attachTransportRef } = context;
  useEffect(() => {
    attachTransportRef.current = open;
    return () => { attachTransportRef.current = null; };
  }, [attachTransportRef, open]);
  return runtime;
}
