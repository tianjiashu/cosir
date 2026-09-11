"use client";

import {
  memo,
  useCallback,
  useRef,
  useState,
  type FC,
  type PropsWithChildren,
} from "react";
import { CheckIcon, CircleAlertIcon, LoaderIcon, WrenchIcon, XCircleIcon } from "lucide-react";
import { cva, type VariantProps } from "class-variance-authority";
import { useScrollLock } from "@assistant-ui/react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { DisclosureRow } from "./disclosure-row.aui";
import { DISCLOSURE_CONTENT_CLASS } from "./disclosure-tokens";
import { toolGroupSummaryLabel, type ToolGroupSummary } from "./tool-group-status";

const ANIMATION_DURATION = 200;

const toolGroupVariants = cva("aui-tool-group-root group/tool-group mb-1 w-full", {
  variants: {
    variant: {
      outline: "rounded-lg border py-3",
      ghost: "",
      muted: "border-muted-foreground/30 bg-muted/30 rounded-lg border py-3",
    },
  },
  defaultVariants: { variant: "ghost" },
});

export type ToolGroupRootProps = Omit<
  React.ComponentProps<typeof Collapsible>,
  "open" | "onOpenChange"
> &
  VariantProps<typeof toolGroupVariants> & {
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
    defaultOpen?: boolean;
  };

function ToolGroupRoot({
  className,
  variant,
  open: controlledOpen,
  onOpenChange: controlledOnOpenChange,
  defaultOpen = false,
  children,
  ...props
}: ToolGroupRootProps) {
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const lockScroll = useScrollLock(collapsibleRef, ANIMATION_DURATION);

  const isControlled = controlledOpen !== undefined;
  const isOpen = isControlled ? controlledOpen : uncontrolledOpen;

  const handleOpenChange = useCallback(
    (open: boolean) => {
      lockScroll();
      if (!isControlled) {
        setUncontrolledOpen(open);
      }
      controlledOnOpenChange?.(open);
    },
    [lockScroll, isControlled, controlledOnOpenChange],
  );

  return (
    <Collapsible
      ref={collapsibleRef}
      data-slot="tool-group-root"
      data-variant={variant ?? "ghost"}
      open={isOpen}
      onOpenChange={handleOpenChange}
      className={cn(
        toolGroupVariants({ variant }),
        "group/tool-group-root",
        className,
      )}
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
  count,
  active = false,
  summary,
  className,
  ...props
}: React.ComponentProps<typeof CollapsibleTrigger> & {
  count: number;
  active?: boolean;
  summary?: ToolGroupSummary;
}) {
  const isActive = summary?.phase === "running" || (summary === undefined && active);
  const label = summary === undefined ? `${count} 个工具调用` : toolGroupSummaryLabel(summary);
  const statusIcon = summary?.phase === "failed"
    ? <CircleAlertIcon className="aui-tool-group-trigger-status text-destructive size-3.5" aria-label={summary.failed === summary.total ? "全部工具执行失败" : "部分工具执行失败"} />
    : summary?.phase === "cancelled"
      ? <XCircleIcon className="aui-tool-group-trigger-status text-amber-600 size-3.5" aria-label={summary.cancelled === summary.total ? "全部工具已取消" : "部分工具已取消"} />
      : summary?.phase === "unknown"
        ? <CircleAlertIcon className="aui-tool-group-trigger-status text-amber-600 size-3.5" aria-label="工具状态待确认" />
        : summary?.phase === "completed"
          ? <CheckIcon className="aui-tool-group-trigger-status text-emerald-600 size-3.5" aria-label="全部工具已完成" />
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
            "aui-tool-group-trigger-label-wrapper inline-block text-start text-sm leading-none font-medium",
            "group-data-[variant=ghost]/tool-group-root:font-normal",
            isActive && "shimmer motion-reduce:animate-none",
          )}
        >
          {label}
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
        "aui-tool-group-trigger origin-left",
        "group-data-[variant=ghost]/tool-group-root:text-muted-foreground group-data-[variant=ghost]/tool-group-root:hover:text-foreground",
        "group-data-[variant=outline]/tool-group-root:w-full group-data-[variant=outline]/tool-group-root:px-4",
        "group-data-[variant=muted]/tool-group-root:w-full group-data-[variant=muted]/tool-group-root:px-4",
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
          DISCLOSURE_CONTENT_CLASS,
          "group-data-[variant=outline]/tool-group-root:mt-3 group-data-[variant=outline]/tool-group-root:border-t group-data-[variant=outline]/tool-group-root:px-4 group-data-[variant=outline]/tool-group-root:pt-3",
          "group-data-[variant=muted]/tool-group-root:mt-3 group-data-[variant=muted]/tool-group-root:border-t group-data-[variant=muted]/tool-group-root:px-4 group-data-[variant=muted]/tool-group-root:pt-3",
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

type ToolGroupComponent = FC<
  PropsWithChildren<{ startIndex: number; endIndex: number }>
> & {
  Root: typeof ToolGroupRoot;
  Trigger: typeof ToolGroupTrigger;
  Content: typeof ToolGroupContent;
};

const ToolGroupImpl: FC<
  PropsWithChildren<{ startIndex: number; endIndex: number }>
> = ({ children, startIndex, endIndex }) => {
  const toolCount = endIndex - startIndex + 1;

  return (
    <ToolGroupRoot>
      <ToolGroupTrigger count={toolCount} />
      <ToolGroupContent>{children}</ToolGroupContent>
    </ToolGroupRoot>
  );
};

/**
 * @deprecated This wrapper targets the legacy `components.ToolGroup` prop
 * on `<MessagePrimitive.Parts>`. Use `<MessagePrimitive.GroupedParts>` with
 * a `groupBy` returning `"group-tool"` and compose `ToolGroupRoot` /
 * `ToolGroupTrigger` / `ToolGroupContent` directly. See `thread.tsx`.
 */
const ToolGroup = memo(ToolGroupImpl) as unknown as ToolGroupComponent;

ToolGroup.displayName = "ToolGroup";
ToolGroup.Root = ToolGroupRoot;
ToolGroup.Trigger = ToolGroupTrigger;
ToolGroup.Content = ToolGroupContent;

export {
  ToolGroup,
  ToolGroupRoot,
  ToolGroupTrigger,
  ToolGroupContent,
  toolGroupVariants,
};
