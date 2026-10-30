import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { ArrowUpRightIcon, BotIcon } from "lucide-react";
import { TaskRunStatusDot, taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { Badge } from "@/components/ui/badge";
import { cancelTask } from "@/lib/assistant/task-commands";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import { ToolStatus } from "./tool-status";
import { ToolStopButton, useStopRequest } from "./tool-stop-button";
import { readDelegationDisplay } from "./child-agent-display";
import { readToolArtifact } from "./types";

type DelegationToolRowProps = ToolCallMessagePartProps & {
  /** 只有可写主 Thread 提供停止子 Agent 的入口。 */
  interactive?: boolean;
  /** 父 Task 的当前活动已在停止中。 */
  taskCancelling?: boolean;
};

/**
 * 委派卡片：子 Agent 的身份、子 Task 最新 Run 的状态，以及打开工作台 / 停止两个入口。
 *
 * 刻意用浅色卡片而不是终端的深色岛：两者语义无关，样式相同会让人把委派误读成命令执行。
 * 卡片不可展开（与静态声明 `expandable: false` 一致）——子 Agent 的过程与结论都在工作台里，
 * 内联详情只会重复状态。整卡是打开工作台的点击区（标题按钮以伪元素铺满卡片），停止按钮叠在
 * 其上方，避免按钮嵌套按钮。
 */
export function DelegationToolRow({ artifact: rawArtifact, interactive = false, taskCancelling = false }: DelegationToolRowProps) {
  const artifact = readToolArtifact(rawArtifact);
  const display = readDelegationDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const title = display?.title ?? "子 Agent";
  const role = artifact.agent_role ?? display?.role ?? null;
  const childTaskId = artifact.child_task_id ?? display?.childTaskId;
  // 后端把子 Task 最新 Run 的状态实时投影进 display.status；挂起（等审批 / 等消息）仍可停止。
  const status = display?.status ?? artifact.backendStatus;
  const isActive = status === "pending" || status === "running" || status === "suspended";
  const canOpen = childTaskId !== undefined && actions !== null;
  const canCancel = childTaskId !== undefined
    && isActive
    && interactive
    && !taskCancelling;
  const cancellation = useStopRequest(isActive);

  const open = () => {
    if (canOpen && childTaskId !== undefined) {
      void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", {
        data: { childTaskId },
      });
      actions?.openAgent({ taskId: childTaskId, title });
    }
  };
  const requestCancellation = () => {
    if (!canCancel || childTaskId === undefined) return;
    void frontendLog("INFO", "delegation_child_run_cancel_requested", "用户请求停止子 Agent", {
      data: { child_task_id: childTaskId },
    });
    void cancellation.stop(async () => {
      const result = await cancelTask(childTaskId);
      if (result.accepted) {
        // 停止信号只投递；子 Task 的终态经父委派工具 part 的 canonical 投影到达，不在这里乐观改状态。
        void frontendLog("INFO", "delegation_child_run_cancel_signalled", "子 Agent 停止信号已发出", {
          data: { child_task_id: childTaskId },
        });
        return null;
      }
      void frontendLog("WARNING", "delegation_child_run_cancel_failed", "停止子 Agent 请求失败", {
        data: { child_task_id: childTaskId, reason: result.reason },
      });
      return result.message;
    });
  };

  // 定位字段（child_task_id 等）只服务打开与停止，不在卡片上重复展示。
  return (
    <div
      data-testid="tool-activity-row"
      className={cn(
        "bg-card relative flex min-w-0 items-center gap-3 rounded-xl border px-3 py-2 shadow-sm transition-colors",
        canOpen && "hover:bg-muted/50 focus-within:ring-ring/50 focus-within:ring-2",
        status === "failed" && "border-destructive/30",
      )}
    >
      <span
        className={cn(
          "bg-info/10 text-info relative flex size-8 shrink-0 items-center justify-center rounded-full",
          status === "running" && "ring-info/40 animate-pulse ring-2",
        )}
        aria-hidden="true"
      >
        <BotIcon className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        {canOpen ? (
          <button
            type="button"
            className="block max-w-full truncate text-left text-sm font-medium outline-none after:absolute after:inset-0 after:rounded-[inherit]"
            aria-label={`打开子 Agent工作台：${title}`}
            title={title}
            onClick={open}
          >
            {title}
          </button>
        ) : (
          <p className="truncate text-sm font-medium" title={title}>{title}</p>
        )}
        <p className="text-muted-foreground truncate text-xs">
          {role ? `${role} · 子 Agent` : "子 Agent"}
        </p>
        {cancellation.error && <p className="text-warning truncate text-xs" role="alert">{cancellation.error}</p>}
      </div>
      <DelegationStatus status={status} />
      {canOpen && <ArrowUpRightIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />}
      {canCancel && (
        <ToolStopButton
          label={`子 Agent：${title}`}
          pending={cancellation.pending}
          onClick={requestCancellation}
          className="relative z-10"
        />
      )}
    </div>
  );
}

/**
 * 卡片展示的是子 Task 最新 Run 的状态，统一用 Run 状态文案（与侧栏、工作台标签一致）；
 * 只有 Run 状态表里没有的值（如 `unknown`）回落到工具状态。
 */
function DelegationStatus({ status }: { status: string }) {
  const presentation = taskRunStatusPresentation(status, true);
  if (!presentation) return <ToolStatus status="unknown" />;
  return (
    <Badge tone={presentation.tone} variant="tint" size="sm" className="shrink-0 gap-1.5">
      <TaskRunStatusDot status={status} className="size-1.5" />
      {presentation.label}
    </Badge>
  );
}
