import { backgroundTarget, commandPending, useCommandRequest } from "@/lib/assistant/command-request-store";
import { ArrowDownToLineIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { backgroundToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import { useForegroundWaits } from "@/lib/background-tasks/background-tasks-store";

/**
 * 前台等待的「转到后台」，由 `ToolPart` 对所有工具统一挂载，不按工具名分支。
 *
 * 只在 snapshot `foreground_waits` 列出该调用时渲染：前台等待不进看板，卡片是唯一入口。
 * 转到后台之后这里不再渲染：停止只在后台看板，避免同一调用出现语义不同的多个停止入口。
 * 按钮只发命令，请求期间禁用，不做乐观更新。
 */
export function ForegroundWaitAction({ taskId, toolCallId }: { taskId: number; toolCallId: string }) {
  const foregroundWaits = useForegroundWaits(taskId);
  const request = useCommandRequest(taskId, backgroundTarget(toolCallId));
  const busy = commandPending(request);
  if (!foregroundWaits.includes(toolCallId)) return null;

  const detach = async () => {
    const failure = await backgroundToolCall(taskId, toolCallId);
    if (failure) notify.error(failure);
  };

  return (
    <div className="text-muted-foreground mt-1 flex items-center text-xs" data-foreground-wait={toolCallId}>
      <Button variant="ghost" size="xs" disabled={busy} onClick={detach}>
        <ArrowDownToLineIcon aria-hidden="true" />
        {busy ? "正在转到后台…" : request?.error ? "重试转到后台" : "转到后台"}
      </Button>
    </div>
  );
}
