"use client";

import { AlertTriangleIcon, CheckCircle2Icon, CircleSlashIcon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { AgentTeamGraphIssue } from "@/lib/api/agent-teams";
import type { TeamGraphIssues } from "@/lib/agent-team-editor/use-graph-issues";
import { cn } from "@/lib/utils";

/**
 * 编辑器顶栏的结构检查状态：无问题显示「结构完整」，有问题时点开列出全部问题。
 * `onSelect` 由画布提供，点问题即选中对应节点或连线；表单视图不传，只作列表。
 */
export function TeamGraphIssuesIndicator({ graphIssues, onSelect }: {
  graphIssues: TeamGraphIssues;
  onSelect?: (issue: AgentTeamGraphIssue) => void;
}) {
  const { issues, failed } = graphIssues;
  if (failed && issues.length === 0) {
    return <span className="text-muted-foreground inline-flex items-center gap-1.5 text-xs" title="结构检查请求失败，保存时仍会由后端校验"><CircleSlashIcon className="size-3.5" />无法检查</span>;
  }
  if (issues.length === 0) {
    return <span className="text-success inline-flex items-center gap-1.5 text-xs"><CheckCircle2Icon className="size-3.5" />结构完整</span>;
  }
  return (
    <Popover>
      <PopoverTrigger render={<button type="button" className="focus-ring bg-warning/10 text-warning hover:bg-warning/15 inline-flex h-7 items-center gap-1.5 rounded-full px-2.5 text-xs font-medium transition-colors" />}>
        <AlertTriangleIcon className="size-3.5" />{issues.length} 个问题
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80">
        <p className="text-xs font-semibold">保存前需要处理</p>
        <ul className="max-h-72 space-y-1 overflow-y-auto">
          {issues.map((issue, index) => {
            const selectable = Boolean(onSelect) && (issue.node_id !== null || issue.transition_index !== null);
            return (
              <li key={`${issue.code}:${index}`}>
                <button
                  type="button"
                  disabled={!selectable}
                  onClick={() => onSelect?.(issue)}
                  className={cn("w-full rounded-md px-2 py-1.5 text-left text-xs leading-5", selectable ? "hover:bg-muted" : "cursor-default")}
                >
                  {issue.message}
                </button>
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

/** 节点卡片、转移行或属性面板里的问题明细。 */
export function TeamGraphIssueList({ issues, className }: { issues: AgentTeamGraphIssue[]; className?: string }) {
  if (issues.length === 0) return null;
  return (
    <ul className={cn("text-warning space-y-0.5 text-xs leading-5", className)} aria-label="结构问题">
      {issues.map((issue, index) => <li key={`${issue.code}:${index}`} className="flex gap-1.5"><AlertTriangleIcon aria-hidden="true" className="mt-1 size-3 shrink-0" />{issue.message}</li>)}
    </ul>
  );
}
