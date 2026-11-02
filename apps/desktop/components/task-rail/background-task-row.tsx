import { useState } from "react";
import { BotIcon, CheckIcon, ChevronRightIcon, MessageCircleQuestionIcon, SquareIcon, SquareTerminalIcon, XCircleIcon, type LucideIcon } from "lucide-react";

import { CallRow } from "@/components/assistant-ui/tools/call-row";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { BackgroundTaskEntry, TransportToolCallPart } from "@/lib/assistant/contract";
import { toToolCallPart } from "@/lib/assistant/converter";
import { readToolArtifact } from "@/lib/assistant/tool-artifact";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import { useBackgroundTaskPart } from "@/lib/background-tasks/store";
import { cn } from "@/lib/utils";

/** 新增后台结果种类时补一项（种类的事实源是后端 `BackgroundKind`）。 */
const KIND_ICONS: Record<BackgroundTaskEntry["kind"], LucideIcon> = {
  terminal_command: SquareTerminalIcon,
  child_agent: BotIcon,
  agent_reply: MessageCircleQuestionIcon,
};

/**
 * 看板的一行：所有种类共用的外壳（种类图标、标题、耗时、停止），展开后就是对话里
 * 同一条调用行（`CallRow`，含它的展开体）。
 *
 * 外壳只读 snapshot `background_tasks`；调用行读同一个 snapshot 工具 part（按 `toolCallId` 镜像，见
 * `useBackgroundTaskPart`），由 kind 视图注册表按 `display_data.kind` 决定长什么样，这里不按种类分支。
 * 调用行以只读方式渲染：同一调用只留外壳上这一个停止入口（`cancelToolCall`，对所有种类语义一致）。
 * 对应 part 已不在对话里（如编辑重跑移出那一轮）时只剩外壳。运行中的条目默认展开。收尾中（工作已结束、
 * 对话里的占位还没落定）不计时、不能停止，只标「收尾中」，等 snapshot 回推完成或取消。
 * 操作只发命令，结果等 snapshot 回推。
 */
export function BackgroundTaskRow({ taskId, entry, now }: { taskId: number; entry: BackgroundTaskEntry; now: number }) {
  const [busy, setBusy] = useState(false);
  const part = useBackgroundTaskPart(taskId, entry.toolCallId);
  const running = entry.status === "running";
  const [expanded, setExpanded] = useState(running);
  const Icon = KIND_ICONS[entry.kind];
  const elapsed = entry.status === "settling"
    ? null
    : formatElapsed(Date.parse(entry.startedAt), entry.endedAt === null ? now : Date.parse(entry.endedAt));
  const open = expanded && part !== null;

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
    <li className="flex flex-col" data-background-task={entry.toolCallId}>
      <div className="hover:bg-muted/60 flex h-8 items-center gap-2 rounded-md px-1">
        <Icon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
        {part !== null ? (
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-1 text-left outline-none"
            title={entry.label}
            aria-expanded={open}
            onClick={() => setExpanded((value) => !value)}
          >
            <ChevronRightIcon className={cn("text-muted-foreground size-3.5 shrink-0 transition-transform", open && "rotate-90")} aria-hidden="true" />
            <span className="truncate">{entry.label}</span>
          </button>
        ) : (
          <span className="min-w-0 flex-1 truncate" title={entry.label}>{entry.label}</span>
        )}
        {elapsed !== null && <span className="text-muted-foreground shrink-0 text-xs tabular-nums">{elapsed}</span>}
        {entry.status === "settling" ? (
          <span className="text-muted-foreground shrink-0 text-xs">收尾中</span>
        ) : running ? (
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
      </div>
      {open && <BackgroundTaskCard taskId={taskId} part={part} />}
    </li>
  );
}

/** 对话里同一条调用行的只读渲染；视图出错只影响这一条，不拖垮看板。 */
function BackgroundTaskCard({ taskId, part }: { taskId: number; part: TransportToolCallPart }) {
  const converted = toToolCallPart(part);
  if (converted.type !== "tool-call") return null;
  return (
    <div className="mt-1 mb-2 pl-6">
      <RenderIsolationBoundary
        scope="part"
        resetKey={part.toolCallId}
        metadata={{ partId: part.toolCallId, partType: "tool-call", renderer: "后台任务调用行" }}
        fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={part.toolName} onRetry={onRetry} />}
      >
        <CallRow
          taskId={taskId}
          toolCallId={part.toolCallId}
          toolName={part.toolName}
          args={part.args}
          artifact={readToolArtifact(converted.artifact)}
          interactive={false}
        />
      </RenderIsolationBoundary>
    </div>
  );
}

/** 耗时：不足一分钟显示秒，之后显示「分 秒」「时 分」。 */
export function formatElapsed(startedAt: number, endedAt: number): string {
  const seconds = Math.max(0, Math.floor((endedAt - startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
