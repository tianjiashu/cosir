import { WifiOffIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type TransportIssue = {
  message: string;
  retryable: boolean;
};

export function TransportStatus({ issue, onRetry }: {
  issue: TransportIssue | null;
  onRetry?: () => void;
}) {
  if (!issue) return null;
  return (
    <div role="status" className={cn("border-b px-4 py-2 text-xs", issue.retryable ? "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300" : "border-destructive/30 bg-destructive/10 text-destructive")}>
      <div className="mx-auto flex max-w-3xl items-center gap-2">
        <WifiOffIcon className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1">{issue.message}</span>
        {issue.retryable && onRetry ? (
          <button
            type="button"
            className="shrink-0 font-medium underline underline-offset-2 hover:no-underline"
            onClick={onRetry}
          >
            重新同步
          </button>
        ) : null}
      </div>
    </div>
  );
}
