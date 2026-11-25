import { ActionBarPrimitive, useAuiState } from "@assistant-ui/react";
import { CornerDownLeftIcon, Loader2Icon, PencilIcon, XIcon } from "lucide-react";
import { useState, type FC } from "react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { moveMessageToComposer } from "@/lib/assistant/composer/move-to-composer";
import { deleteQueuedSessionMessage, sendQueuedSessionMessageNow } from "@/lib/assistant/session-commands";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

type QueuedCommand = "send_now" | "edit" | "delete";

const QUEUED_COMMAND_FAILURE: Record<QueuedCommand, string> = {
  send_now: "立即发送失败",
  edit: "编辑失败",
  delete: "删除失败",
};

/**
 * 还没并入的用户输入（snapshot 的 `queued_messages`）的操作：立即发送 / 编辑 / 删除。排队条的一行与对话流
 * 末尾的插话气泡共用这一套——两者是同一种消息，只是摆放不同；停止或失败后留下的插话也因此能改能删。
 *
 * 立即发送 = 让它现在就被处理（后端一条命令，见 `sendQueuedSessionMessageNow`：排队的改为插话，并停下当前执行
 * 中的工具调用；Run 已结束时开新 Run）；模型已从配置删除时后端拒绝，提示随错误给出。编辑 = 删除这一条 + 把
 * 正文与附件放回输入框（`moveMessageToComposer`）。命令只投递，结果以 snapshot 的 `queued_messages` 为准；
 * 消息恰好已被并入时后端 409，消费投影随即把它移出。正在提交、还没有后端身份的消息三个操作都不可用。
 */
export const QueuedMessageActions: FC<{ sessionId: number }> = ({ sessionId }) => {
  const message = useAuiState((state) => state.message);
  const messageId = message.id;
  const canonical = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.queued_messages
    ?.find((item) => item.id === messageId));
  const [busy, setBusy] = useState<QueuedCommand | null>(null);

  const run = async (command: QueuedCommand) => {
    // 只渲染在用户消息上；角色判断只为收窄类型。
    if (busy !== null || !canonical || message.role !== "user") return;
    setBusy(command);
    try {
      if (command === "send_now") await sendQueuedSessionMessageNow(sessionId, messageId);
      else if (command === "edit") await moveMessageToComposer({
        sessionId,
        message,
        canonical,
        remove: () => deleteQueuedSessionMessage(sessionId, messageId),
      });
      else await deleteQueuedSessionMessage(sessionId, messageId);
      void frontendLog("INFO", "queued_message_command_accepted", "排队消息命令已受理", {
        data: { sessionId, messageId, command, delivery: canonical.delivery },
      });
    } catch (error) {
      notify.error(QUEUED_COMMAND_FAILURE[command], { error, fallback: "这条消息可能已开始处理，请以对话内容为准" });
      void frontendLog("WARNING", "queued_message_command_failed", "排队消息命令失败", {
        data: { sessionId, messageId, command, delivery: canonical.delivery },
        error,
      });
    } finally {
      setBusy(null);
    }
  };

  const disabled = busy !== null || !canonical;
  return (
    <ActionBarPrimitive.Root className="flex shrink-0 gap-0.5">
      <TooltipIconButton tooltip="立即发送" aria-label="立即发送" size="sm" disabled={disabled} onClick={() => void run("send_now")}>
        {busy === "send_now" ? <Loader2Icon className="animate-spin" /> : <CornerDownLeftIcon />}
      </TooltipIconButton>
      <TooltipIconButton tooltip="编辑（放回输入框）" aria-label="编辑" size="sm" disabled={disabled} onClick={() => void run("edit")}>
        {busy === "edit" ? <Loader2Icon className="animate-spin" /> : <PencilIcon />}
      </TooltipIconButton>
      <TooltipIconButton tooltip="删除" aria-label="删除" size="sm" disabled={disabled} onClick={() => void run("delete")}>
        {busy === "delete" ? <Loader2Icon className="animate-spin" /> : <XIcon />}
      </TooltipIconButton>
    </ActionBarPrimitive.Root>
  );
};
