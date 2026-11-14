import { useCallback, useMemo, useRef } from "react";

import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { recoverRejectedCommands } from "@/lib/assistant/composer/composer-drafts";
import { sendSessionMessage, type SessionMessageRequest } from "@/lib/assistant/session-commands";
import { showErrorDialog } from "@/lib/confirm/confirm-store";
import { notify } from "@/lib/notify";
import { activeRunLaunch } from "@/lib/assistant/run-launch";
import { useModelCatalog } from "@/lib/model/catalog";
import { resolveModelSelection, useModelSelectionStore } from "@/lib/model/model-selection-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/conversation-runtime/runtime/runtime-types";
import { useSessionAssistantTransportRuntime } from "@/lib/assistant/transport/use-session-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";

/**
 * 创建 Assistant Transport runtime，并集中承载只属于发送的策略：命令组装（运行期配置锁定）与命令被拒后的
 * 草稿恢复。订阅与重连归连接对象，不在这里。
 */
export function useRuntimeTransport(
  context: RuntimeSessionContext,
): ReturnType<typeof useSessionAssistantTransportRuntime> {
  // 发送时按与选择器相同的回落链取模型，目录取最近一次渲染时的值。
  const catalog = useModelCatalog().catalog;
  const catalogRef = useRef(catalog);
  catalogRef.current = catalog;
  const attachmentAdapter = useMemo(
    () => context.workspaceId == null ? null : createAttachmentAdapter(context.workspaceId),
    [context.workspaceId],
  );

  /**
   * 命令提交被拒或未能发出（HTTP 非 2xx、网络失败、附件失效）：内容回到用户手里并说明原因。
   * 用户还在这里时用弹窗打断；已切走时不抢占别的页面，用 toast 说明内容回到了哪个 Session。
   * 网络失败时后端可能已收下，文案要提醒先看对话再重发，避免重复。
   */
  const handleSubmitError = useCallback(async (error: Error, commands: readonly UserAddMessageCommand[]) => {
    const state = context.store.getSnapshot().state;
    void frontendLog("ERROR", "assistant_session_command_rejected", "Session 命令提交被拒", {
      traceId: context.traceId,
      data: {
        sessionId: context.sessionId,
        commandCount: commands.length,
        source: "http",
        activityStatus: state.activity?.status ?? null,
      },
      error,
    });
    const fallback = "网络异常，消息可能已送达，请先查看对话再决定是否重发";
    if (await recoverRejectedCommands(context.sessionId, commands) === "away") {
      notify.error(`发送失败，内容已放回「${state.title}」的输入框`, { error, fallback });
      return;
    }
    void showErrorDialog({ title: "发送失败", message: userErrorMessage(error, fallback) });
  }, [context]);

  return useSessionAssistantTransportRuntime(context.connection, context.store, {
    adapters: attachmentAdapter == null ? undefined : { attachments: attachmentAdapter },
    submitMessage: async (command) => {
      const state = context.store.getSnapshot().state;
      // 运行期配置锁定：活动期间发送用它冻结的配置（composer 也只显示这份）。
      const runLaunch = activeRunLaunch(state);
      const preferred = resolveModelSelection(
        catalogRef.current?.models ?? [],
        useModelSelectionStore.getState(),
        { sessionId: context.sessionId, workspaceId: context.workspaceId },
        { lastLaunch: state.activity?.launch ?? null, inheritedLaunch: state.fork?.initialLaunch ?? null },
      );
      const launch = runLaunch ?? {
        modelConfigId: preferred?.modelConfigId ?? null,
        reasoningEffort: preferred?.reasoningEffort ?? null,
      };
      // 正文里的胶囊标签就是机器可读的指针，命令原样发送，不在前端改写 parts。
      const message = command.message as SessionMessageRequest["message"];
      if (launch.modelConfigId === null) {
        throw new UserFacingError(runLaunch ? "当前运行的模型配置已删除，请停止后重新选择" : "请先选择模型后再发送");
      }
      const request: SessionMessageRequest = {
        message,
        modelConfigId: launch.modelConfigId,
        reasoningEffort: launch.reasoningEffort,
      };
      void frontendLog("INFO", "assistant_session_command_prepared", "Session 命令已准备发送", {
        traceId: context.traceId,
        data: {
          sessionId: context.sessionId,
          launchLocked: runLaunch !== null,
          partCount: message.parts.length,
        },
      });
      await sendSessionMessage(context.sessionId, request, { traceId: context.traceId });
    },
    onError: handleSubmitError,
  });
}
