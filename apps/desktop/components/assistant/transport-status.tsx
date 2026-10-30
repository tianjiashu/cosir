import { WifiOffIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type TransportIssue = {
  message: string;
  /** 该连接问题是否可以由用户点击“重新同步”恢复；与 Agent 的工具 retryable 无关。 */
  canResync: boolean;
};

export function TransportStatus({ issue, onRetry }: {
  issue: TransportIssue | null;
  onRetry?: () => void;
}) {
  if (!issue) return null;
  return (
    <div role="status" className={cn("border-b px-4 py-2 text-xs", issue.canResync ? "border-warning/30 bg-warning/10 text-warning" : "border-destructive/30 bg-destructive/10 text-destructive")}>
      <div className="mx-auto flex max-w-conversation items-center gap-2">
        <WifiOffIcon className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1">{issue.message}</span>
        {issue.canResync && onRetry ? (
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto shrink-0 px-0 text-current underline underline-offset-2 hover:no-underline"
            onClick={onRetry}
          >
            重新同步
          </Button>
        ) : null}
      </div>
    </div>
  );
}
