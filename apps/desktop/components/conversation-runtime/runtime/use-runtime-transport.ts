import type { ToolCalls } from "@/lib/assistant/tools/tool-call-index";
import { useCallback, useEffect, useMemo } from "react";

import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { recoverRejectedCommands } from "@/lib/assistant/composer/composer-drafts";
import { markTaskRequestsUnsynced } from "@/lib/assistant/command-request-store";
import { sendTaskMessage, type TaskMessageRequest } from "@/lib/assistant/task-commands";
import { showErrorDialog } from "@/lib/confirm/confirm-store";
import { notify } from "@/lib/notify";
import { activeRunLaunch } from "@/lib/assistant/run-launch";
import { readStoredSelection } from "@/lib/model/selection-storage";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TransportRunStatus, TransportState } from "@/lib/assistant/contract";
import { transportMessageCount } from "@/lib/assistant/transport/transport-state-operations";
import type { RuntimeRecovery } from "@/components/conversation-runtime/runtime/use-runtime-recovery";
import type { RuntimeSessionContext } from "@/components/conversation-runtime/runtime/runtime-types";
import {
  useTaskAssistantTransportRuntime,
  type TransportDisconnect,
} from "@/lib/assistant/transport/use-task-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { prepareUserCommand } from "@/lib/assistant/composer/prepare-user-command";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { useBackendRuntime } from "@/lib/backend/use-backend-runtime";

type RuntimeTransport = Omit<ReturnType<typeof useTaskAssistantTransportRuntime>, "open">;

/**
 * 创建 Assistant Transport runtime，并集中承载只属于 Transport 的策略：命令组装、命令被拒的
 * 草稿恢复、一轮结束的通知与订阅断开后的重连。
 */
export function useRuntimeTransport(
  context: RuntimeSessionContext,
  recovery: RuntimeRecovery,
  onStateCommit: (state: TransportState, toolCalls: ToolCalls) => void,
  selectedToolGroups: readonly string[],
): RuntimeTransport {
  const attachmentAdapter = useMemo(
    () => context.workspaceId == null ? null : createAttachmentAdapter(context.workspaceId),
    [context.workspaceId],
  );
  // 订阅身份跟随本机后端实例：不可用时不订阅，换实例（generation 变化）时 runtime 自行重开。
  const backend = useBackendRuntime();
  const connectionKey = backend.available ? String(backend.generation) : null;

  /**
   * 命令提交被拒或未能发出（HTTP 非 2xx、网络失败、附件失效）：内容回到用户手里并说明原因。
   * 用户还在这里时用弹窗打断；已切走时不抢占别的页面，用 toast 说明内容回到了哪个 Task。
   * 网络失败时后端可能已收下，文案要提醒先看对话再重发，避免重复。
   */
  const handleSubmitError = useCallback(async (error: Error, commands: readonly UserAddMessageCommand[]) => {
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
    const fallback = "网络异常，消息可能已送达，请先查看对话再决定是否重发";
    if (await recoverRejectedCommands(context.taskId, commands) === "away") {
      notify.error(`发送失败，内容已放回「${context.latestStateRef.current.title}」的输入框`, { error, fallback });
      return;
    }
    void showErrorDialog({ title: "发送失败", message: userErrorMessage(error, fallback) });
  }, [context]);

  const handleActivitySettled = useCallback((status: TransportRunStatus) => {
    void frontendLog("INFO", "assistant_activity_settled", "Task 当前活动已结束", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        activityTurn: context.latestStateRef.current.activity?.turn ?? null,
        status,
        messageCount: transportMessageCount(context.latestStateRef.current),
      },
    });
    context.onTaskStateChanged?.();
  }, [context]);

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
    // 断开期间可能错过「停下、又开出新运行」：重连拿到的完整快照分不出来，停止结果改为待确认。
    markTaskRequestsUnsynced(context.taskId);
    recovery.reconnect(resync ? "resync_required" : "disconnected");
  }, [context, recovery]);

  const { runtime, open, loadEarlierHistory } = useTaskAssistantTransportRuntime(context.taskId, {
    initialState: context.initialState,
    connectionKey,
    adapters: attachmentAdapter == null ? undefined : { attachments: attachmentAdapter },
    traceId: context.traceId,
    submitMessage: async (command) => {
      const state = context.latestStateRef.current;
      // 运行期配置锁定：活动期间发送用它冻结的配置（composer 也只显示这份）。
      const runLaunch = activeRunLaunch(state);
      const selection = readStoredSelection({ kind: "task", id: context.taskId });
      const launch = runLaunch ?? {
        modelConfigId: selection?.modelConfigId ?? state.fork?.initialLaunch.modelConfigId ?? null,
        reasoningEffort: selection ? selection.reasoningEffort ?? null : state.fork?.initialLaunch.reasoningEffort ?? null,
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
      void frontendLog("INFO", "assistant_task_command_prepared", "Task 命令已准备发送", {
        traceId: context.traceId,
        data: {
          taskId: context.taskId,
          launchLocked: runLaunch !== null,
          partCount: message.parts.length,
          attachmentCount: message.attachments?.length ?? 0,
        },
      });
      await sendTaskMessage(context.taskId, request, { traceId: context.traceId });
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
    onStateCommit,
    performanceProbe: context.performanceProbe,
  });
  // 有限重连与「重新同步」在会话层（runtime 创建之前就已存在），经这个 ref 拿到重开入口。
  const { attachTransportRef } = context;
  useEffect(() => {
    attachTransportRef.current = open;
    return () => { attachTransportRef.current = null; };
  }, [attachTransportRef, open]);
  return { runtime, loadEarlierHistory };
}
