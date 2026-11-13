import { backgroundTarget, commandPending, useCommandRequest } from "@/lib/assistant/command-request-store";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { backgroundToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import { useForegroundWaits } from "@/lib/background-tasks/background-tasks-store";

/**
 * 前台等待的「转到后台」：放在拥有实时内容的卡片底部状态条（目前只有终端卡片），看着输出决定不再等它。
 *
 * 只认 snapshot `foreground_waits` 这个通用事实，不按工具名判断；哪天别的 kind 也有前台等待，在它自己的视图里
 * 放这个组件即可。前台等待不进看板，卡片是唯一入口；转到后台之后不再渲染，停止只在后台看板，避免同一调用
 * 出现语义不同的多个停止入口。按钮只发命令，不做乐观更新。
 *
 * 样式是一段安静的文字：不打断命令，是等待时的主操作，但不必有按钮外形。请求期间只变淡、文字不变，免得宽度
 * 跳动挤开状态；失败由 toast 说明，再点即重试。
 */
export function ForegroundWaitAction({ taskId, toolCallId }: { taskId: number; toolCallId: string }) {
  const foregroundWaits = useForegroundWaits(taskId);
  const busy = commandPending(useCommandRequest(taskId, backgroundTarget(toolCallId)));
  if (!foregroundWaits.includes(toolCallId)) return null;

  const detach = async () => {
    const failure = await backgroundToolCall(taskId, toolCallId);
    if (failure) notify.error(failure);
  };

  return (
    <Tooltip>
      <TooltipTrigger
        render={<Button variant="ghost" size="xs" className="text-muted-foreground hover:text-foreground px-1.5 hover:bg-transparent dark:hover:bg-transparent"
          disabled={busy} aria-busy={busy} onClick={detach} data-foreground-wait={toolCallId} />}
      >
        转到后台
      </TooltipTrigger>
      <TooltipContent side="bottom">不再等待，命令在后台继续</TooltipContent>
    </Tooltip>
  );
}
