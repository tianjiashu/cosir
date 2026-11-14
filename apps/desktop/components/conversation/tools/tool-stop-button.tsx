import { LoaderCircleIcon, SquareIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

import { commandPending, useCommandRequest, type CommandTarget } from "@/lib/assistant/command-request-store";

/** 停止命令的展示状态；失败用 toast 说明。按钮只在目标存活时渲染，所以不用对账。 */
export function useStopRequest(sessionId: number, target: CommandTarget) {
  const request = useCommandRequest(sessionId, target);
  const stop = async (send: () => Promise<string | null>): Promise<boolean> => {
    const error = await send();
    if (error) notify.error(error);
    return error === null;
  };
  return { pending: commandPending(request), error: request?.error ?? null, stop };
}

/**
 * 卡片里的纯图标停止按钮（实心方块，不会被看成复选框）；文案只在 aria-label / title 中，给命令或标题留出宽度。
 * 红色只落在悬停时的图标上、底色保持中性：停止是会中断的操作，提醒即可，不制造紧张。
 */
export function ToolStopButton({ label, pending, onClick, className }: {
  /** 停止对象的可读描述，如「终端命令」「子 Agent：审查代码」。 */
  label: string;
  pending: boolean;
  onClick: () => void;
  className?: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      aria-label={pending ? `正在停止${label}` : `停止${label}`}
      title={pending ? "正在停止…" : "停止"}
      disabled={pending}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className={cn("text-muted-foreground hover:text-destructive", className)}
    >
      {pending
        ? <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden="true" />
        : <SquareIcon className="size-3 fill-current" aria-hidden="true" />}
    </Button>
  );
}
