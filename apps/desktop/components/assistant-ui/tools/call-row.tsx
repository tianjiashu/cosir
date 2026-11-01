import { memo, useRef, type ReactNode } from "react";
import { useScrollLock } from "@assistant-ui/react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { DISCLOSURE_ANIMATION_MS, DISCLOSURE_CONTENT_CLASS } from "@/components/assistant-ui/turn/turn-tokens";
import { callKey, useDisclosure } from "@/lib/assistant/disclosure-store";
import type { ToolArtifact } from "@/lib/assistant/tool-artifact";
import { cn } from "@/lib/utils";
import { CallStatus } from "./call-status";
import type { CallViewContext } from "./call-view";
import { resolveCallView } from "./call-view-registry";
import { ForegroundWaitAction } from "./foreground-wait-action";
import { ToolIcon } from "./tool-icons";

export type CallRowProps = {
  /** 拥有该调用的任务：工具取消、决定与面板都按 Task 寻址，开合状态也按 Task 分桶。 */
  taskId: number;
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  artifact: ToolArtifact;
  /** 只读展示面（子 Agent 阅读器、后台看板）不提供写入交互。 */
  interactive?: boolean;
  /** 用户已请求停止整个 Task 的当前活动，工具级停止入口随之收起。 */
  taskCancelling?: boolean;
};

/** 标题规则与 `turn-layout.callTitle` 同源：`heading.title` → `presentation.verb` → 工具名。 */
function rowTitle({ heading, presentation }: ToolArtifact, toolName: string): string {
  return heading?.title ?? presentation.verb ?? toolName;
}

const ROW_CLASS = "flex min-h-7 w-full min-w-0 items-center gap-2 rounded-md px-1.5 -mx-1.5 py-0.5 text-left text-2sm";

/**
 * 统一调用行：图标 · 标题（意图） · 目标 · 元信息 · 状态位，所有工具调用从排队到终态都是这一行，只有内容在变。
 *
 * 元信息与展开体由 kind 视图注册表按 `display_data.kind` 给出（没有 kind 时只有标题与状态）；失败与取消把
 * 元信息换成后端收口的短提示且不可展开——展开体只会重复同一句提示，失败态展示数据也不带成功态结果。
 * 两个附加态不是工具生命周期：`background`（在后台，未结束时标「后台运行中」）与 `awaitingUser`（等你操作）。
 * 可展开时整行是折叠按钮，视图给的行尾动作渲染在按钮之外（避免按钮嵌套）；开合记在 disclosure store，
 * 虚拟列表卸载再挂载不丢，默认值由视图按状态给（命令行运行中展开、结束收起）。
 */
export const CallRow = memo(function CallRow({ taskId, toolCallId, toolName, args, artifact, interactive = true, taskCancelling = false }: CallRowProps) {
  const status = artifact.backendStatus;
  const unfinished = status === "pending" || status === "running";
  const failed = status === "failed";
  const cancelled = status === "cancelled";
  const context: CallViewContext = { taskId, toolCallId, artifact, args, interactive, taskCancelling };
  const resolved = failed || cancelled ? null : resolveCallView(artifact);
  const body = resolved?.view.body?.(resolved.data, context) ?? null;
  const expandable = body !== null && body !== undefined && body !== false;
  const defaultOpen = expandable && (resolved?.view.defaultOpen?.(resolved.data, context) ?? false);
  const [open, setOpen] = useDisclosure(taskId, callKey(toolCallId), defaultOpen);
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const lockScroll = useScrollLock(collapsibleRef, DISCLOSURE_ANIMATION_MS);

  const title = rowTitle(artifact, toolName);
  const target = artifact.heading?.target ?? null;
  const kindMeta = resolved?.view.meta?.(resolved.data, context);
  const stateMeta: ReactNode = failed
    ? <span className="text-destructive">{artifact.error ?? "执行失败"}</span>
    : cancelled
      ? artifact.error ?? "已取消"
      : artifact.awaitingUser
        ? <span className="text-info">等你操作</span>
        : artifact.background && unfinished
          ? <span className="text-info">后台运行中</span>
          : null;
  const actions = resolved?.view.actions?.(resolved.data, context);

  const contents = (
    <>
      <ToolIcon name={artifact.presentation.icon} className="text-muted-foreground size-3.5" aria-hidden="true" />
      <span
        data-slot="call-title"
        className={cn(
          "shrink-0 truncate",
          failed ? "text-destructive" : cancelled ? "text-muted-foreground line-through" : unfinished ? "text-foreground" : "text-muted-foreground",
          status === "pending" && artifact.heading === null && "shimmer motion-reduce:animate-none",
        )}
        title={title}
      >
        {title}
      </span>
      <span data-slot="call-target" className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-xs" title={target ?? undefined}>
        {target}
      </span>
      {(kindMeta || stateMeta) && (
        <span data-slot="call-meta" className="text-muted-foreground flex max-w-[45%] shrink-0 items-center gap-1.5 truncate text-xs">
          {kindMeta}
          {kindMeta && stateMeta && <span aria-hidden="true">·</span>}
          {stateMeta}
        </span>
      )}
      <CallStatus status={status} />
    </>
  );

  const row = expandable ? (
    <CollapsibleTrigger
      data-slot="call-row"
      className={cn(ROW_CLASS, "group/call-row hover:bg-muted cursor-pointer")}
    >
      {contents}
    </CollapsibleTrigger>
  ) : (
    <div data-slot="call-row" className={ROW_CLASS}>{contents}</div>
  );

  const line = (
    <div className="flex min-w-0 items-center gap-1">
      <div className="min-w-0 flex-1">{row}</div>
      {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
    </div>
  );

  return (
    <div data-tool-call-id={toolCallId} data-tool-status={status} className="min-w-0">
      {expandable ? (
        <Collapsible
          ref={collapsibleRef}
          open={open}
          onOpenChange={(next) => {
            lockScroll();
            setOpen(next);
          }}
          style={{ "--animation-duration": `${DISCLOSURE_ANIMATION_MS}ms` } as React.CSSProperties}
        >
          {line}
          <CollapsibleContent className={DISCLOSURE_CONTENT_CLASS}>
            <div className="mt-1 mb-1.5 ml-[22px] min-w-0 text-sm">{body}</div>
          </CollapsibleContent>
        </Collapsible>
      ) : line}
      {interactive && <ForegroundWaitAction taskId={taskId} toolCallId={toolCallId} />}
    </div>
  );
});
