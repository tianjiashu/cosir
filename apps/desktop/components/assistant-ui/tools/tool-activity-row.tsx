import type { ReactNode } from "react";
import { SquareArrowOutUpRightIcon } from "lucide-react";
import { DISCLOSURE_ROW_CLASS } from "@/components/assistant-ui/elements/disclosure-tokens";
import { cn } from "@/lib/utils";
import type { TransportToolStatus } from "@/lib/assistant/contract";
import { ToolStatus } from "./tool-status";

export type ToolActivityRowProps = {
  icon: ReactNode;
  title: string;
  meta?: string | null;
  status: TransportToolStatus;
  onOpen?: (() => void) | null;
  openLabel?: string;
  disabled?: boolean;
  action?: ReactNode;
  className?: string;
};

/** Shared keyboard-accessible low-noise row for tool activity projections. */
export function ToolActivityRow({ icon, title, meta, status, onOpen, openLabel, disabled = false, action, className }: ToolActivityRowProps) {
  const canOpen = onOpen !== null && onOpen !== undefined && !disabled;
  const open = () => { if (canOpen) onOpen(); };
  return (
    <div className="flex min-w-0 items-center gap-1">
      <button
        type="button"
        className={cn(DISCLOSURE_ROW_CLASS, "group w-auto flex-1 rounded-md px-2 text-left hover:bg-muted/60 disabled:cursor-default", className)}
        disabled={disabled}
        onClick={open}
        onKeyDown={(event) => { if ((event.key === "Enter" || event.key === " ") && canOpen) { event.preventDefault(); open(); } }}
        aria-label={canOpen ? openLabel ?? `打开 ${title}` : title}
        data-testid="tool-activity-row"
      >
        {icon}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{title}</span>
          {meta && <span className="text-muted-foreground block truncate text-xs">{meta}</span>}
        </span>
        <ToolStatus status={status} />
        {canOpen && <SquareArrowOutUpRightIcon className="text-muted-foreground size-3.5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />}
      </button>
      {action}
    </div>
  );
}
