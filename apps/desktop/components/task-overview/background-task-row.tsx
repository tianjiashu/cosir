import { useState } from "react";
import { BotIcon, CheckIcon, SquareIcon, SquareTerminalIcon, XCircleIcon, type LucideIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import type { BackgroundTaskEntry } from "@/lib/assistant/contract";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";

/** 新增延迟结果种类时补一项（见设计文档「新增工具如何接入」）。 */
const KIND_ICONS: Record<BackgroundTaskEntry["kind"], LucideIcon> = {
  terminal_command: SquareTerminalIcon,
  child_agent: BotIcon,
};

/**
 * 看板的一行：种类图标、单行截断的标题、耗时与操作。点击标题滚动到对话里对应的工具卡片（按
 * `data-tool-call-id` 定位，卡片不在当前对话里时无动作）。操作只发命令，结果等 snapshot 回推。
 */
export function BackgroundTaskRow({ taskId, entry, now }: { taskId: number; entry: BackgroundTaskEntry; now: number }) {
  const [busy, setBusy] = useState(false);
  const Icon = KIND_ICONS[entry.kind];
  const running = entry.status === "running";
  const elapsed = formatElapsed(Date.parse(entry.startedAt), entry.endedAt === null ? now : Date.parse(entry.endedAt));

  const run = async (action: () => Promise<string | null>) => {
    setBusy(true);
    try {
      const failure = await action();
      if (failure !== null) notify.error(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="hover:bg-muted/60 flex h-8 items-center gap-2 rounded-md px-1" data-background-task={entry.toolCallId}>
      <Icon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      <button
        type="button"
        className="min-w-0 flex-1 truncate text-left outline-none"
        title={entry.label}
        onClick={() => revealToolCard(entry.toolCallId)}
      >
        {entry.label}
      </button>
      <span className="text-muted-foreground shrink-0 text-xs tabular-nums">{elapsed}</span>
      {running ? (
        <TooltipIconButton
          tooltip="停止"
          className="size-6"
          disabled={busy}
          onClick={() => run(async () => {
            const result = await cancelToolCall(taskId, entry.toolCallId);
            return result.kind === "failed" ? result.message : null;
          })}
        >
          <SquareIcon aria-hidden="true" />
        </TooltipIconButton>
      ) : entry.status === "completed" ? (
        <CheckIcon className="text-muted-foreground size-3.5 shrink-0" aria-label="已完成" />
      ) : (
        <XCircleIcon className="text-muted-foreground size-3.5 shrink-0" aria-label="已取消" />
      )}
    </li>
  );
}

function revealToolCard(toolCallId: string): void {
  const card = document.querySelector(`[data-tool-call-id="${CSS.escape(toolCallId)}"]`);
  card?.scrollIntoView({ block: "center", behavior: "smooth" });
}

/** 耗时：不足一分钟显示秒，之后显示「分 秒」「时 分」。 */
export function formatElapsed(startedAt: number, endedAt: number): string {
  const seconds = Math.max(0, Math.floor((endedAt - startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
