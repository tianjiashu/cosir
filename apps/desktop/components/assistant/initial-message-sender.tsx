"use client";

import { useAui, useAuiState } from "@assistant-ui/react";
import { useEffect } from "react";

const pendingMessageKey = (taskId: number) =>
  `cosir:pending-initial-message:${taskId}`;

/**
 * 首次进入对话时把可选初始消息投递到 composer 的副作用组件。
 *
 * 通过 task-scoped sessionStorage 保存待发送草稿，并以服务端 canonical messages
 * 判断首条消息是否已经成功落库。发送失败时草稿不会被删除，重新进入任务即可重试；
 * 仅负责副作用，不渲染任何可见 UI。
 *
 * @param taskId - 当前任务 id，用于 sessionStorage 去重 key。
 * @param message - 可选初始消息；为空则不投递。
 * @returns 恒为 null（纯副作用组件）。
 * @sideEffects 在挂载后下一个宏任务内向 composer 设置并发送初始消息；只有服务端
 *   state 确认包含该用户消息后才清理待发送草稿；组件卸载时清除定时器。
 */
export function InitialMessageSender({
  taskId,
}: {
  taskId: number;
}) {
  const aui = useAui();
  const pendingMessage =
    typeof window === "undefined"
      ? undefined
      : window.sessionStorage.getItem(pendingMessageKey(taskId)) ?? undefined;
  // Do not read transport state during the first render. The transport
  // extras are installed by AssistantRuntimeProvider's internal render
  // component effect, so useAssistantTransportState is intentionally not
  // safe in this sibling during provider initialization. The thread state is
  // the stable UI-facing projection and is sufficient for this side effect.
  const messages = useAuiState((state) => state.thread.messages);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const hasCanonicalMessage = messages.some(
    (item) =>
      item.role === "user" &&
      item.parts.some(
        (part) =>
          part.type === "text" &&
          part.text.trim() === pendingMessage?.trim(),
      ),
  );

  useEffect(() => {
    const key = pendingMessageKey(taskId);
    if (hasCanonicalMessage) {
      window.sessionStorage.removeItem(key);
      window.sessionStorage.removeItem(`cosir:initial-command-id:${taskId}`);
      return;
    }
    if (!pendingMessage) return;
    const timer = window.setTimeout(() => {
      aui.composer.setText(pendingMessage);
      aui.composer.send();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [aui, hasCanonicalMessage, pendingMessage, taskId]);

  if (!pendingMessage || hasCanonicalMessage || isRunning) return null;

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-20 flex justify-center px-4">
      <button
        type="button"
        className="pointer-events-auto rounded-full border bg-background px-4 py-2 text-sm shadow-lg hover:bg-muted"
        onClick={() => {
          aui.composer.setText(pendingMessage);
          aui.composer.send();
        }}
      >
        首条消息未发送，点击重试
      </button>
    </div>
  );
}
