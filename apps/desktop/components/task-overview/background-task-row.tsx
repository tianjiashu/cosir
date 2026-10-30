import { useState } from "react";
import { BotIcon, CheckIcon, ChevronRightIcon, LocateFixedIcon, SquareIcon, SquareTerminalIcon, XCircleIcon, type LucideIcon } from "lucide-react";

import { ToolPart } from "@/components/assistant-ui/tools/tool-part";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import type { BackgroundTaskEntry, TransportToolCallPart } from "@/lib/assistant/contract";
import { toToolCallPart } from "@/lib/assistant/converter";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import { useBackgroundTaskPart } from "@/lib/task-overview/background-tasks-store";
import { cn } from "@/lib/utils";
import { revealToolCall } from "@/lib/assistant/thread-navigation";

/** 新增延迟结果种类时补一项（见设计文档「新增工具如何接入」）。 */
const KIND_ICONS: Record<BackgroundTaskEntry["kind"], LucideIcon> = {
  terminal_command: SquareTerminalIcon,
  child_agent: BotIcon,
};

/**
 * 看板的一行：所有种类共用的外壳（种类图标、标题、耗时、停止、在对话中定位），展开后就是对话里
 * 同一张工具卡片。
 *
 * 外壳只读 snapshot `background_tasks`；卡片读同一个 snapshot 工具 part（按 `toolCallId` 镜像，见
 * `useBackgroundTaskPart`），由 renderer 注册表按 `display_data.kind` 决定长什么样，这里不按种类分支。
 * 卡片以只读方式渲染：同一调用只留外壳上这一个停止入口（`cancelToolCall`，对所有种类语义一致）。
 * 对应 part 已不在对话里（如编辑重跑移出那一轮）时只剩外壳。运行中的条目默认展开。
 * 操作只发命令，结果等 snapshot 回推。
 */
export function BackgroundTaskRow({ taskId, entry, now }: { taskId: number; entry: BackgroundTaskEntry; now: number }) {
  const [busy, setBusy] = useState(false);
  const part = useBackgroundTaskPart(taskId, entry.toolCallId);
  const running = entry.status === "running";
  const [expanded, setExpanded] = useState(running);
  const Icon = KIND_ICONS[entry.kind];
  const elapsed = formatElapsed(Date.parse(entry.startedAt), entry.endedAt === null ? now : Date.parse(entry.endedAt));
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
        <span className="text-muted-foreground shrink-0 text-xs tabular-nums">{elapsed}</span>
        <TooltipIconButton tooltip="在对话中定位" className="size-6" onClick={() => void locate(taskId, entry.toolCallId)}>
          <LocateFixedIcon aria-hidden="true" />
        </TooltipIconButton>
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
      </div>
      {open && <BackgroundTaskCard taskId={taskId} part={part} />}
    </li>
  );
}

/** 对话里同一张工具卡片的只读渲染；renderer 出错只影响这一条，不拖垮看板。 */
function BackgroundTaskCard({ taskId, part }: { taskId: number; part: TransportToolCallPart }) {
  const converted = toToolCallPart(part);
  if (converted.type !== "tool-call") return null;
  const complete = part.status === "completed" || part.status === "failed" || part.status === "cancelled";
  return (
    <div className="mt-1 mb-2 pl-6">
      <RenderIsolationBoundary
        scope="part"
        resetKey={part.toolCallId}
        metadata={{ partId: part.toolCallId, partType: "tool-call", renderer: "后台任务卡片" }}
        fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={part.toolName} onRetry={onRetry} />}
      >
        <ToolPart
          {...converted}
          // 后端驱动的工具不经前端回填结果；assistant-ui 的回调在只读面上没有意义。
          status={complete ? { type: "complete" } : { type: "running" }}
          addResult={noop}
          resume={noop}
          respondToApproval={noop}
          taskId={taskId}
          interactive={false}
        />
      </RenderIsolationBoundary>
    </div>
  );
}

function noop(): void {}

/** 对话可能只加载了最近一段历史：交给对话列表按需往上翻页再滚过去。 */
async function locate(taskId: number, toolCallId: string): Promise<void> {
  if (!(await revealToolCall(taskId, toolCallId))) notify.error("这张卡片已不在当前对话里");
}

/** 耗时：不足一分钟显示秒，之后显示「分 秒」「时 分」。 */
export function formatElapsed(startedAt: number, endedAt: number): string {
  const seconds = Math.max(0, Math.floor((endedAt - startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
