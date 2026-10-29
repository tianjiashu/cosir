import { useState } from "react";
import { ArrowDownToLineIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { backgroundToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import { useForegroundWaits } from "@/lib/task-overview/background-tasks-store";

/**
 * 前台等待的「转到后台」，由 `ToolPart` 对所有工具统一挂载，不按工具名分支。
 *
 * 只在 snapshot `foreground_waits` 列出该调用时渲染：前台等待不进看板，卡片是唯一入口。
 * 后台占位不在这里另起状态行：状态由工具卡片自身展示，取消走卡片自己的停止或后台看板，
 * 避免同一调用出现语义不同的多个停止入口。按钮只发命令，请求期间禁用，不做乐观更新。
 */
export function ForegroundWaitAction({ taskId, toolCallId }: { taskId: number; toolCallId: string }) {
  const foregroundWaits = useForegroundWaits(taskId);
  const [busy, setBusy] = useState(false);
  if (!foregroundWaits.includes(toolCallId)) return null;

  const detach = async () => {
    setBusy(true);
    try {
      const result = await backgroundToolCall(taskId, toolCallId);
      if (result.kind === "failed") notify.error(result.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="text-muted-foreground mt-1 flex items-center text-xs" data-foreground-wait={toolCallId}>
      <Button variant="ghost" size="xs" disabled={busy} onClick={detach}>
        <ArrowDownToLineIcon aria-hidden="true" />
        转到后台
      </Button>
    </div>
  );
}
