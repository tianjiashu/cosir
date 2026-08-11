import { Children, type ReactNode, memo, useState } from "react";
import {
  Ban,
  Bot,
  CheckCircle2,
  ChevronRight,
  Clock,
  Loader2,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import type { TimelineDelegationStatus } from "@/services/timeline/projector";

/** Props for rendering a parent-turn delegation lifecycle entry. */
interface DelegationTimelineEntryProps {
  /** Child AgentProfile id selected for this delegation. */
  childAgentId: string;
  /** Current delegation lifecycle status. */
  status: TimelineDelegationStatus;
  /** Child turn id once the backend has created the child run. */
  childTurnId?: string;
  /** Display-only delegation category. */
  delegationType: string;
  /** Successful terminal summary. */
  summary?: string;
  /** Failed or cancelled terminal reason. */
  error?: string;
  /** Render-ready expanded child timeline events. */
  childEntries?: ReactNode;
}

const STATUS_CONFIG: Record<
  TimelineDelegationStatus,
  {
    variant: "outline" | "success" | "destructive" | "warning" | "secondary";
    Icon: typeof Clock;
  }
> = {
  pending: { variant: "outline", Icon: Clock },
  running: { variant: "secondary", Icon: Loader2 },
  completed: { variant: "success", Icon: CheckCircle2 },
  failed: { variant: "destructive", Icon: XCircle },
  cancelled: { variant: "outline", Icon: Ban },
};

/**
 * Renders a delegation lifecycle row inside the parent turn timeline.
 *
 * @param props - Delegation metadata and optional expanded child timeline content.
 * @returns A shrink-safe timeline row with status, child identifiers, terminal summary/error,
 *   and optional child entries.
 *
 * @throws Does not throw.
 *
 * @sideeffect Maintains local expand/collapse state for child entries.
 */
export const DelegationTimelineEntry = memo(function DelegationTimelineEntry({
  childAgentId,
  status,
  childTurnId,
  delegationType,
  summary,
  error,
  childEntries,
}: DelegationTimelineEntryProps) {
  const [isOpen, setIsOpen] = useState(false);
  const hasChildEntries = Children.count(childEntries) > 0;
  const { variant, Icon } = STATUS_CONFIG[status];
  const detail = status === "failed" || status === "cancelled" ? error : summary;

  return (
    <div className="w-full min-w-0 border-l border-border pl-3 py-1">
      <div className="flex w-full min-w-0 items-start gap-2">
        <Bot className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            {hasChildEntries && (
              <button
                type="button"
                onClick={() => setIsOpen((prev) => !prev)}
                className="inline-flex shrink-0 items-center rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
                aria-label={isOpen ? "Collapse delegated child events" : "Expand delegated child events"}
              >
                <ChevronRight
                  className={cn("h-3.5 w-3.5 transition-transform", isOpen && "rotate-90")}
                />
              </button>
            )}
            <Badge variant={variant} className="shrink-0 gap-1 px-2 py-0">
              <Icon className={cn("h-3 w-3", status === "running" && "animate-spin")} />
              {status}
            </Badge>
            <span className={cn(Caption.mono, "min-w-0 break-all text-muted-foreground")}>
              {childAgentId}
            </span>
            <span className={cn(Caption.xs, "shrink-0 text-muted-foreground")}>
              {delegationType}
            </span>
          </div>
          {childTurnId && (
            <div className={cn(Caption.mono, "flex min-w-0 flex-wrap gap-1 text-muted-foreground")}>
              <span className="shrink-0">child turn:</span>
              <span className="min-w-0 break-all">{childTurnId}</span>
            </div>
          )}
          {detail && (
            <p
              className={cn(
                "min-w-0 whitespace-pre-wrap break-words text-sm",
                status === "failed" || status === "cancelled"
                  ? "text-destructive"
                  : "text-muted-foreground",
              )}
            >
              {detail}
            </p>
          )}
          {hasChildEntries && isOpen && (
            <div className="min-w-0 space-y-2 border-l border-border pl-3">
              {childEntries}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
