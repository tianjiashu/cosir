import { ArrowRightIcon } from "lucide-react";
import type { TransportToolStatus } from "@/lib/assistant/contract";
import { cn } from "@/lib/utils";
import { ToolIcon } from "./tool-icons";
import { ToolStatus } from "./tool-status";

export type FileChange = {
  path: string;
  new_path?: string | null;
  status?: string;
  patch?: string | null;
  truncated?: boolean;
  insertions?: number;
  deletions?: number;
};

type FileOperationCardProps = {
  title: string;
  icon?: string;
  changes: FileChange[];
  totalFiles: number;
  status: TransportToolStatus;
  error?: string | null;
};

/**
 * Render completed file moves/deletions as a compact, non-expandable result card.
 *
 * This component only presents file-operation metadata already projected by the
 * backend. It does not read files, calculate diffs, or provide an interaction
 * for expanding the card. Content changes continue to use `DiffTool`.
 */
export function FileOperationCard({
  title,
  icon,
  changes,
  totalFiles,
  status,
  error,
}: FileOperationCardProps) {
  const visibleChanges = changes.slice(0, 3);
  const hiddenCount = Math.max(0, totalFiles - visibleChanges.length);
  const isTerminalState = status === "failed" || status === "cancelled";

  return (
    <section
      aria-label={`${title}，${totalFiles} 个文件`}
      className={cn(
        "group/file-operation overflow-hidden rounded-lg border bg-background",
        isTerminalState && "border-destructive/25",
      )}
    >
      <header className="flex min-w-0 items-center gap-2.5 border-b bg-muted/20 px-3 py-2.5">
        <ToolIcon name={icon} aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {title}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <span className="text-xs text-muted-foreground">{totalFiles} 个文件</span>
          <ToolStatus status={status} />
        </span>
      </header>

      <div className={cn("space-y-2 px-3 py-2.5", isTerminalState && "bg-destructive/5")}>
        {isTerminalState ? (
          <p className="text-xs text-destructive">{error ?? "执行失败"}</p>
        ) : (
          <>
            {visibleChanges.map((change, index) => (
              <FileOperationRow key={`${change.path}:${change.new_path ?? ""}:${index}`} change={change} />
            ))}
            {hiddenCount > 0 && (
              <p className="text-xs text-muted-foreground">另有 {hiddenCount} 个文件</p>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function FileOperationRow({ change }: { change: FileChange }) {
  const isMoved = change.status === "moved";
  const destination = change.new_path ?? "目标路径不可用";

  return (
    <div className="flex min-w-0 items-start gap-2">
      <span className={fileChangeStatusBadgeClass(change.status)}>
        {fileChangeStatusLabel(change.status)}
      </span>
      {isMoved ? (
        <div className="flex min-w-0 flex-1 items-center gap-1.5 text-xs">
          <span className="min-w-0 truncate font-mono text-foreground" title={change.path}>
            {change.path}
          </span>
          <ArrowRightIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 truncate font-mono text-foreground" title={destination}>
            {destination}
          </span>
        </div>
      ) : (
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground" title={change.path}>
          {change.path}
        </span>
      )}
    </div>
  );
}

export function fileChangeStatusLabel(status: string | undefined): string {
  switch (status) {
    case "deleted": return "已删除";
    case "moved": return "已移动";
    case "added": return "新增";
    case "modified": return "修改";
    default: return "文件变更";
  }
}

export function fileChangeStatusBadgeClass(status: string | undefined): string {
  const base = "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium";
  switch (status) {
    case "deleted": return `${base} bg-destructive/10 text-destructive`;
    case "moved": return `${base} bg-blue-500/10 text-blue-700 dark:text-blue-400`;
    case "added": return `${base} bg-emerald-500/10 text-emerald-700 dark:text-emerald-400`;
    default: return `${base} bg-muted text-muted-foreground`;
  }
}
