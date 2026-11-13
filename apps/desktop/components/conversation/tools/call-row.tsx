import { DeferredDetail } from "./deferred-detail";
import { projectCallStatus } from "@/lib/assistant/tools/call-status";
import { toolResultSignal } from "@/lib/assistant/tools/tool-result-signals";
import { memo, useRef, type ReactNode } from "react";
import { useScrollLock } from "@assistant-ui/react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { DISCLOSURE_ANIMATION_MS, DISCLOSURE_CONTENT_CLASS } from "@/components/conversation/turn/turn-tokens";
import { callKey, useDisclosure } from "@/lib/assistant/turn/disclosure-store";
import { toolHeading, toolIconName } from "@/lib/assistant/tools/tool-catalog";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";
import { cn } from "@/lib/utils";
import { useBackgroundTasksStore } from "@/lib/background-tasks/background-tasks-store";
import { useConversationAsideStore } from "@/lib/conversation-aside/conversation-aside-store";
import { ThinkingLabel } from "../turn/thinking-label";
import { CallStatus } from "./call-status-badge";
import { detailsLiveInRail, type CallViewContext } from "./call-view";
import { resolveCallView } from "./call-view-registry";
import { CallElapsed } from "./call-elapsed";
import { ToolIcon } from "./tool-icons";

export type CallRowProps = {
  /** 拥有该调用的任务：工具取消、决定与面板都按 Task 寻址，开合状态也按 Task 分桶。 */
  /** 上层过程/分组是否可见，不影响独立弹层。 */
  visible?: boolean;
  inherited?: boolean;
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

const ROW_CLASS = "flex min-h-7 w-full min-w-0 items-center gap-2 rounded-md px-1.5 -mx-1.5 py-0.5 text-left text-2sm";
const CLICKABLE_ROW_CLASS = "hover:bg-muted cursor-pointer";

/**
 * 统一调用行：图标 · 标题（意图） · 运行时长 · 目标 · 元信息 · 状态位，所有工具调用从排队到终态都是这一行，只有内容在变。
 * 行只说「在做什么」和「出了问题」：运行中标题扫光（`projectCallStatus`），跑过 3 秒标题后出现时长，成功不说话，异常按工具失败、取消、
 * 等你操作、实体异常（视图的 `status`）的优先级出现在元信息。
 *
 * 元信息与展开体由 kind 视图注册表按 `display_data.kind` 给出（没有 kind 时只有标题与状态）；已有结构化部分
 * 结果仍可展开。归侧栏的后台工作没有详情与操作，整行是定位到看板的按钮。
 * 可展开时整行是折叠按钮，视图给的行尾动作渲染在按钮之外（避免按钮嵌套），悬停范围因此放在外层；开合记在
 * disclosure store，虚拟列表卸载再挂载不丢，时间线详情默认折叠，显式开合跨状态保留。
 */
export const CallRow = memo(function CallRow(props: CallRowProps) {
  return props.inherited ? <CallRowContent {...props} interactive={false} /> : <OwnedCallRow {...props} />;
});

/** 活调用才订阅后台执行；继承记录复用同一个行视图与折叠状态。 */
function OwnedCallRow(props: CallRowProps) {
  const workStatus = useBackgroundTasksStore(state => state.tasks[props.taskId]?.find(entry => entry.toolCallId === props.toolCallId)?.status);
  return <CallRowContent {...props} workStatus={workStatus} />;
}

function CallRowContent({ taskId, toolCallId, toolName, args, artifact, interactive = true, inherited = false, taskCancelling = false, visible = true, workStatus }: CallRowProps & Pick<CallViewContext, "workStatus">) {
  const status = artifact.backendStatus;
  const unfinished = status === "pending" || status === "running";
  const failed = status === "failed";
  const cancelled = status === "cancelled";
  const context: CallViewContext = { taskId, toolCallId, artifact, args, interactive, inherited, taskCancelling, workStatus };
  const resolved = resolveCallView(context);
  const detail = resolved?.detail;
  const expandable = detail !== undefined;
  const defaultOpen = detail?.defaultOpen ?? false;
  const [open, setOpen] = useDisclosure(taskId, callKey(toolCallId), defaultOpen);
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const lockScroll = useScrollLock(collapsibleRef, DISCLOSURE_ANIMATION_MS);

  // 标题与 `turn-layout.callTitle` 同源：都按工具目录从参数算出。
  const { title, target } = toolHeading(toolName, args, artifact.intent);
  const feedback = projectCallStatus(status, artifact.awaitingUser, artifact.background, workStatus);
  const kindMeta = resolved?.meta;
  const signal = failed || cancelled ? null : toolResultSignal(artifact.display_data);
  const stateMeta: ReactNode = failed
    ? <span className="text-destructive">{artifact.error ?? "执行失败"}</span>
    : cancelled
      ? artifact.error ?? "已取消"
      : artifact.awaitingUser
        ? <span className="text-info">等你操作</span>
        : resolved?.status;
  const actions = resolved?.actions;

  const contents = (
    <>
      <ToolIcon name={toolIconName(toolName)} className="text-muted-foreground size-3.5" aria-hidden="true" />
      <ThinkingLabel data-slot="call-title" active={feedback.animated && !inherited} className="min-w-0 truncate" title={title}>{title}</ThinkingLabel>
      {!inherited && <CallElapsed startedAt={artifact.startedAt} />}
      <span data-slot="call-target" className="text-muted-foreground min-w-0 truncate font-mono text-xs" title={target ?? undefined}>
        {resolved?.target ?? target}
      </span>
      {expandable && <DisclosureChevron open={open} reveal className="text-muted-foreground" />}
      <span className="flex-1" aria-hidden="true" />
      {(kindMeta || stateMeta || signal) && (
        <span data-slot="call-meta" className="text-muted-foreground flex max-w-[45%] shrink-0 items-center gap-1.5 truncate text-xs">
          {kindMeta}
          {kindMeta && stateMeta && <span aria-hidden="true">·</span>}
          {stateMeta}
          {signal && <span className="text-warning">{kindMeta || stateMeta ? " · " : ""}{signal}</span>}
        </span>
      )}
      {inherited && unfinished ? <span className="text-xs text-muted-foreground">继承时未结束</span> : <CallStatus status={status} awaitingUser={artifact.awaitingUser} label={feedback.label} />}
    </>
  );

  const row = detailsLiveInRail(context) ? (
    <button
      type="button"
      data-slot="call-row"
      className={cn(ROW_CLASS, CLICKABLE_ROW_CLASS)}
      onClick={() => useConversationAsideStore.getState().revealBackground(taskId, toolCallId)}
    >
      <span className="sr-only">在后台任务中查看：</span>
      {contents}
    </button>
  ) : expandable ? (
    <CollapsibleTrigger data-slot="call-row" className={cn(ROW_CLASS, CLICKABLE_ROW_CLASS)}>
      {contents}
    </CollapsibleTrigger>
  ) : (
    <div data-slot="call-row" className={ROW_CLASS}>{contents}</div>
  );

  const line = (
    <div className="group/disclosure flex min-w-0 items-center gap-1">
      <div className="min-w-0 flex-1">{row}</div>
      {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
    </div>
  );

  return (
    <div data-tool-call-id={toolCallId} data-tool-status={status} className={cn("min-w-0", artifact.awaitingUser && "rounded-xl border border-border/60 px-3 py-2")}>
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
          <CollapsibleContent keepMounted className={DISCLOSURE_CONTENT_CLASS}>
            <div className="mt-1 mb-1.5 ml-[22px] min-w-0 text-xs"><DeferredDetail visible={visible && open} render={detail!.render} /></div>
          </CollapsibleContent>
        </Collapsible>
      ) : line}
      {resolved?.overlay}
    </div>
  );
}
