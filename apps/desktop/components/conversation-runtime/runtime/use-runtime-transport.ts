import { useCallback, useMemo, useRef } from "react";

import { getOrCreateTransportCommandId, type UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { recoverRejectedCommands } from "@/lib/assistant/composer/composer-drafts";
import { sendSessionMessage, type SessionMessageRequest } from "@/lib/assistant/session-commands";
import { showErrorDialog } from "@/lib/confirm/confirm-store";
import { notify } from "@/lib/notify";
import { activeRunLaunch } from "@/lib/assistant/run-launch";
import { COMPOSER_LAUNCH_BLOCKER_TEXT, resolveComposerLaunch, sessionLaunchFacts } from "@/lib/assistant/composer/composer-launch";
import { useModelCatalog } from "@/lib/model/catalog";
import { useModelSelectionStore } from "@/lib/model/model-selection-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/conversation-runtime/runtime/runtime-types";
import { useSessionAssistantTransportRuntime } from "@/lib/assistant/transport/use-session-assistant-transport-runtime";
import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { uploadPendingImages } from "@/lib/assistant/attachments/pending-images";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { sessionDisplayTitle } from "@/lib/session-title/session-display-title";

/**
 * 创建 Assistant Transport runtime，并集中承载只属于发送的策略：命令组装（运行期配置锁定）与命令被拒后的
 * 草稿恢复。订阅与重连归连接对象，不在这里。
 */
export function useRuntimeTransport(
  context: RuntimeSessionContext,
): ReturnType<typeof useSessionAssistantTransportRuntime> {
  // 提交时与输入框读同一份结论（`resolveComposerLaunch`），目录取最近一次渲染时的值。
  const catalog = useModelCatalog();
  const catalogRef = useRef(catalog);
  catalogRef.current = catalog;
  const attachmentAdapter = useMemo(
    () => context.workspaceId == null ? null : createAttachmentAdapter(),
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
      notify.error(`发送失败，内容已放回「${sessionDisplayTitle(state.title)}」的输入框`, { error, fallback });
      return;
    }
    void showErrorDialog({ title: "发送失败", message: userErrorMessage(error, fallback) });
  }, [context]);

  return useSessionAssistantTransportRuntime(context.connection, context.store, {
    adapters: attachmentAdapter == null ? undefined : { attachments: attachmentAdapter },
    submitMessage: async (command) => {
      const state = context.store.getSnapshot().state;
      // 输入框在不能发时已禁用发送；这里是最后一道检查（「下一步建议」按钮不经过输入框）。
      const launch = resolveComposerLaunch({
        catalog: catalogRef.current,
        selections: useModelSelectionStore.getState(),
        scope: { sessionId: context.sessionId, workspaceId: context.workspaceId },
        runLaunch: activeRunLaunch(state),
        facts: sessionLaunchFacts(state),
      });
      if (launch.selection === null) {
        throw new UserFacingError(COMPOSER_LAUNCH_BLOCKER_TEXT[launch.blocker]);
      }
      // 正文里的胶囊标签就是机器可读的指针，原样发送；只有待上传图片在这里上传、换成附件 locator。
      const parts = await uploadPendingImages(context.workspaceId, command.message.parts, context.traceId);
      const message = { ...command.message, parts } as SessionMessageRequest["message"];
      const request: SessionMessageRequest = {
        message,
        modelConfigId: launch.selection.modelConfigId,
        reasoningEffort: launch.selection.reasoningEffort,
        queue: command.queue === true,
        clientMessageId: getOrCreateTransportCommandId(command),
      };
      void frontendLog("INFO", "assistant_session_command_prepared", "Session 命令已准备发送", {
        traceId: context.traceId,
        data: {
          sessionId: context.sessionId,
          launchLocked: launch.locked,
          partCount: message.parts.length,
        },
      });
      await sendSessionMessage(context.sessionId, request, { traceId: context.traceId });
    },
    onError: handleSubmitError,
  });
}

