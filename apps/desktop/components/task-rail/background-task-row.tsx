import { commandPending, stopTarget, useCommandRequest } from "@/lib/assistant/command-request-store";
import { useEffect, useRef } from "react";
import { BotIcon, CircleMinusIcon, SquareIcon, SquareTerminalIcon, UsersIcon, XCircleIcon, type LucideIcon } from "lucide-react";

import { useTaskRailStore } from "@/lib/task-rail/store";
import { resolveCallView } from "@/components/assistant-ui/tools/call-view-registry";
import { DeferredDetail } from "@/components/assistant-ui/tools/deferred-detail";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { BackgroundTaskEntry, TransportToolCallPart } from "@/lib/assistant/contract";
import { toToolCallPart } from "@/lib/assistant/converter";
import { callKey, useDisclosure } from "@/lib/assistant/disclosure-store";
import { readToolArtifact } from "@/lib/assistant/tool-artifact";
import { cancelToolCall } from "@/lib/assistant/task-commands";
import { notify } from "@/lib/notify";
import { useBackgroundTaskPart } from "@/lib/background-tasks/store";

/** 新增后台结果种类时补一项（种类的事实源是后端 `BackgroundKind`）。 */
const KIND_ICONS: Record<BackgroundTaskEntry["kind"], LucideIcon> = {
  terminal_command: SquareTerminalIcon,
  child_agent: BotIcon,
  agent_team: UsersIcon,
};

/**
 * 看板的一行：所有种类共用的外壳（种类图标、标题、耗时、停止），展开后是这次调用的详情。
 *
 * 外壳只读 snapshot `background_tasks`；详情读同一个 snapshot 工具 part（按 `toolCallId` 镜像，见
 * `useBackgroundTaskPart`），由 kind 视图注册表按 `display_data.kind` 决定长什么样，这里不按种类分支。
 * 不再套一条调用行：标题已在外壳上。视图以只读方式解析，同一调用只留外壳上这一个停止入口
 * （`cancelToolCall`，对所有种类语义一致）。
 * 对应 part 已不在对话里（如编辑重跑移出那一轮）时只剩外壳。条目默认收起，开合与调用行共用 `call:` 键：
 * 时间线的后台行没有展开体，这个键只归看板，运行中转到已结束的列表重挂载也不丢。收尾中（工作已结束、
 * 对话里的占位还没落定）不计时、不能停止，只标「收尾中」，等 snapshot 回推完成或取消。
 * 操作只发命令，结果等 snapshot 回推。
 */
export function BackgroundTaskRow({ taskId, entry, now }: { taskId: number; entry: BackgroundTaskEntry; now: number }) {
  const request = useCommandRequest(taskId, stopTarget(entry.toolCallId));
  const busy = commandPending(request);
  const part = useBackgroundTaskPart(taskId, entry.toolCallId);
  const running = entry.status === "running";
  const [expanded, setExpanded] = useDisclosure(taskId, callKey(entry.toolCallId), false);
  const Icon = KIND_ICONS[entry.kind];
  const elapsed = entry.status === "settling"
    ? null
    : formatElapsed(Date.parse(entry.startedAt), entry.endedAt === null ? now : Date.parse(entry.endedAt));
  const requested = useTaskRailStore((state) => state.revealCall);
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (requested?.taskId === taskId && requested.callId === entry.toolCallId) {
      ref.current?.scrollIntoView({ block: "nearest" });
      useTaskRailStore.getState().consumeReveal();
    }
  }, [requested, taskId, entry.toolCallId]);
  const view = part && backgroundCallView(taskId, part, entry.status);
  const open = expanded && view?.detail !== undefined;

  const run = async (action: () => Promise<string | null>) => {
    const failure = await action();
    if (failure !== null) notify.error(failure);
  };

  return (
    <li ref={ref} className="flex flex-col" data-background-task={entry.toolCallId}>
      <div className="group/disclosure hover:bg-muted/60 flex h-8 items-center gap-2 rounded-md px-1">
        <Icon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
        {view?.detail ? (
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-1 text-left outline-none"
            title={entry.label}
            aria-expanded={open}
            onClick={() => setExpanded(!expanded)}
          >
            <span className="truncate">{entry.label}</span>
            <DisclosureChevron open={open} reveal className="text-muted-foreground" />
          </button>
        ) : (
          <span className="min-w-0 flex-1 truncate" title={entry.label}>{entry.label}</span>
        )}
        {(view?.meta || view?.status) && <span className="text-muted-foreground shrink-0 text-xs">{view.meta}{view.status}</span>}
        {view?.actions}
        {elapsed !== null && <span className="text-muted-foreground shrink-0 text-xs tabular-nums">{elapsed}</span>}
        {entry.status === "settling" ? (
          <span className="text-muted-foreground shrink-0 text-xs">收尾中</span>
        ) : running ? (
          <TooltipIconButton
            tooltip={busy ? "正在停止" : request?.error ? "重试停止" : "停止"}
            className="size-6"
            disabled={busy}
            onClick={() => run(() => cancelToolCall(taskId, entry.toolCallId))}
          >
            <SquareIcon aria-hidden="true" />
          </TooltipIconButton>
        ) : entry.status === "completed" ? (
          <CircleMinusIcon className="text-muted-foreground size-3.5 shrink-0" aria-label="已结束" />
        ) : (
          <XCircleIcon className="text-muted-foreground size-3.5 shrink-0" aria-label="已取消" />
        )}
      </div>
      {part && view && (
        <RenderIsolationBoundary
          scope="part"
          resetKey={part.toolCallId}
          metadata={{ partId: part.toolCallId, partType: "tool-call", renderer: "后台任务详情" }}
          fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={part.toolName} onRetry={onRetry} />}
        >
          {open && <div className="mt-1 mb-2"><DeferredDetail visible render={view.detail!.render} /></div>}
          {view.overlay}
        </RenderIsolationBoundary>
      )}
    </li>
  );
}

function backgroundCallView(taskId: number, part: TransportToolCallPart, workStatus: BackgroundTaskEntry["status"]) {
  const converted = toToolCallPart(part);
  if (converted.type !== "tool-call") return null;
  const artifact = readToolArtifact(converted.artifact);
  return resolveCallView({ taskId, toolCallId: part.toolCallId, artifact, args: part.args ?? {}, interactive: false, taskCancelling: false, surface: "details", workStatus });
}

/** 耗时：不足一分钟显示秒，之后显示「分 秒」「时 分」。 */
export function formatElapsed(startedAt: number, endedAt: number): string {
  const seconds = Math.max(0, Math.floor((endedAt - startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
