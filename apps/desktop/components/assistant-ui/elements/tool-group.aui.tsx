"use client";

import {
  useCallback,
  useRef,
  useState,
} from "react";
import { CheckIcon, CircleAlertIcon, LoaderIcon, WrenchIcon, XCircleIcon } from "lucide-react";
import { useScrollLock } from "@assistant-ui/react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { DisclosureRow } from "./disclosure-row.aui";
import { TOOL_TRACE_CONTENT_CLASS } from "./tool-layout-tokens";
import { toolGroupSummaryLabel, type ToolGroupSummary } from "./tool-group-status";

const ANIMATION_DURATION = 200;

/** 工具轨迹折叠组：只有无边框（ghost）一种外观，开合由组件自身持有，展开动画期间锁定视口滚动。 */
function ToolGroupRoot({
  className,
  children,
  ...props
}: Omit<React.ComponentProps<typeof Collapsible>, "open" | "onOpenChange" | "defaultOpen">) {
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const lockScroll = useScrollLock(collapsibleRef, ANIMATION_DURATION);

  const handleOpenChange = useCallback(
    (next: boolean) => {
      lockScroll();
      setOpen(next);
    },
    [lockScroll],
  );

  return (
    <Collapsible
      ref={collapsibleRef}
      data-slot="tool-group-root"
      open={open}
      onOpenChange={handleOpenChange}
      className={cn("aui-tool-group-root w-full", className)}
      style={
        {
          "--animation-duration": `${ANIMATION_DURATION}ms`,
        } as React.CSSProperties
      }
      {...props}
    >
      {children}
    </Collapsible>
  );
}

function ToolGroupTrigger({
  summary,
  className,
  ...props
}: React.ComponentProps<typeof CollapsibleTrigger> & {
  summary: ToolGroupSummary;
}) {
  const isActive = summary.phase === "running";
  const statusIcon = summary.phase === "failed"
    ? <CircleAlertIcon className="aui-tool-group-trigger-status text-destructive size-3.5" aria-label={summary.failed === summary.total ? "全部工具执行失败" : "部分工具执行失败"} />
    : summary.phase === "cancelled"
      ? <XCircleIcon className="aui-tool-group-trigger-status text-warning size-3.5" aria-label={summary.cancelled === summary.total ? "全部工具已取消" : "部分工具已取消"} />
      : summary.phase === "unknown"
        ? <CircleAlertIcon className="aui-tool-group-trigger-status text-warning size-3.5" aria-label="工具状态待确认" />
        : summary.phase === "completed"
          ? <CheckIcon className="aui-tool-group-trigger-status text-success size-3.5" aria-label="全部工具已完成" />
          : undefined;

  return (
    <DisclosureRow
      data-slot="tool-group-trigger"
      leading={
        <WrenchIcon
          data-slot="tool-group-trigger-icon"
          className="aui-tool-group-trigger-icon size-4 shrink-0"
        />
      }
      label={
        <span
          data-slot="tool-group-trigger-label"
          className={cn(
            "aui-tool-group-trigger-label-wrapper inline-block text-start text-sm leading-none font-normal",
            isActive && "shimmer motion-reduce:animate-none",
          )}
        >
          {toolGroupSummaryLabel(summary)}
        </span>
      }
      trailing={isActive ? (
        <LoaderIcon
          data-slot="tool-group-trigger-loader"
          className="aui-tool-group-trigger-loader size-3.5 animate-spin [animation-duration:0.6s]"
          aria-label="工具执行中"
        />
      ) : statusIcon}
      className={cn(
        "aui-tool-group-trigger origin-left text-muted-foreground hover:text-foreground",
        className,
      )}
      {...props}
    />
  );
}

function ToolGroupContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof CollapsibleContent>) {
  return (
    <CollapsibleContent
      data-slot="tool-group-content"
      className={cn(
        "aui-tool-group-content relative overflow-hidden text-sm outline-none",
        "group/collapsible-content ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:animate-none",
        "data-closed:animate-collapsible-up",
        "data-open:animate-collapsible-down",
        "data-closed:fill-mode-forwards",
        "data-closed:pointer-events-none",
        "[--tw-duration:var(--animation-duration)]",
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          TOOL_TRACE_CONTENT_CLASS,
          "[&>*]:animate-in [&>*]:fade-in-0 [&>*]:blur-in-[2px] [&>*]:slide-in-from-top-1 [&>*]:animation-duration-(--animation-duration) [&>*]:ease-[cubic-bezier(0.32,0.72,0,1)]",
          "[&>*]:motion-reduce:animate-none",
          "[&>*:nth-child(2)]:[animation-delay:40ms]",
          "[&>*:nth-child(3)]:[animation-delay:80ms]",
          "[&>*:nth-child(4)]:[animation-delay:120ms]",
          "[&>*:nth-child(n+5)]:[animation-delay:160ms]",
        )}
      >
        {children}
      </div>
    </CollapsibleContent>
  );
}

export {
  ToolGroupRoot,
  ToolGroupTrigger,
  ToolGroupContent,
};
