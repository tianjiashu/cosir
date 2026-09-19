"use client";

import { useMemo, useState } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData } from "react-diff-view";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  PanelRightCloseIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  SaveIcon,
} from "lucide-react";

import type { ChangeNetDiff, TaskChangeResult, TaskFileChange } from "@/lib/api/changes";
import { Button } from "@/components/ui/button";
import { useTaskChanges } from "@/components/task-changes/use-task-changes";

type TaskChangesPanelProps = {
  taskId: number;
  isRunActive: boolean;
};

type ParsedNetDiff =
  | { kind: "ready"; files: FileData[] }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: "truncated" | "missing" | "invalid" };

export function parseTaskNetDiff(diff: ChangeNetDiff | null | undefined): ParsedNetDiff {
  if (!diff || diff.state !== "verified") return { kind: "unavailable", reason: "missing" };
  if (diff.truncated) return { kind: "unavailable", reason: "truncated" };
  if (!diff.patch) {
    return diff.additions === 0 && diff.deletions === 0
      ? { kind: "empty" }
      : { kind: "unavailable", reason: "missing" };
  }
  const hasUnifiedHeaders = diff.patch.includes("\n--- ") && diff.patch.includes("\n+++ ");
  const renameOnly = diff.patch.includes("\nrename from ")
    && diff.patch.includes("\nrename to ")
    && !hasUnifiedHeaders;
  if (renameOnly) return { kind: "empty" };
  if (!diff.patch.startsWith("diff --git ") || !hasUnifiedHeaders) {
    return { kind: "unavailable", reason: "invalid" };
  }
  try {
    const files = parseDiff(diff.patch).filter((file) => file.hunks.length > 0);
    return files.length > 0 ? { kind: "ready", files } : { kind: "empty" };
  } catch {
    return { kind: "unavailable", reason: "invalid" };
  }
}

function statusLabel(status: string): string {
  switch (status) {
    case "pending": return "待处理";
    case "kept": return "已保留";
    case "reverted": return "已回退到基线";
    case "conflict": return "冲突";
    default: return status;
  }
}

function actionLabel(action: string): string {
  switch (action) {
    case "added": return "新增";
    case "modified": return "修改";
    case "deleted": return "删除";
    case "moved": return "移动";
    case "unchanged": return "无净变化";
    default: return action;
  }
}

function resultMessage(result: TaskChangeResult | undefined): string | null {
  if (!result) return null;
  if (result.reason_code === "operation_busy") return "任务仍在运行，请完成后再操作";
  switch (result.outcome) {
    case "kept": return "已将当前状态设为新基线";
    case "already_kept": return "当前状态已是基线";
    case "reverted": return "已回退到基线";
    case "already_reverted": return "该变更已回退";
    case "stale_change_id": return "变更记录已更新，请刷新后重试";
    case "conflict": return "文件状态与变更记录不一致，本次未回退";
    case "snapshot_unverifiable": return "旧快照信息不足以安全验证，本次未回退";
    case "failed": return "文件组操作失败，请刷新后检查状态";
    default: return result.message ?? "文件组操作未完成";
  }
}

export function NetDiff({
  diff,
  contentSuppressed = false,
}: {
  diff: ChangeNetDiff | null | undefined;
  /** 删除类变更：后端不再提供内容 Diff，面板只呈现「已删除」摘要。 */
  contentSuppressed?: boolean;
}) {
  const parsed = useMemo(() => parseTaskNetDiff(diff), [diff]);
  if (diff?.state === "conflict") {
    return <p className="text-destructive px-3 py-3 text-xs">文件状态与变更链不一致，无法安全生成净 Diff；本次未回退。</p>;
  }
  if (diff?.state === "unverifiable") {
    return <p className="text-amber-700 px-3 py-3 text-xs dark:text-amber-300">旧快照信息不足以安全验证，无法显示净 Diff。</p>;
  }
  if (contentSuppressed) {
    return <p className="text-muted-foreground px-3 py-3 text-xs">该文件已删除，不展示删除内容；回退到基线可恢复。</p>;
  }
  if (parsed.kind === "empty") {
    return diff?.has_unrendered_changes
      ? <p className="text-muted-foreground px-3 py-3 text-xs">文件相对基线有变化，但没有可显示的文本行 Diff（例如二进制内容、目录、符号链接或纯移动）。</p>
      : <p className="text-muted-foreground px-3 py-3 text-xs">与最近基线无差异 · 净变化 0</p>;
  }
  if (parsed.kind === "unavailable") {
    const message = parsed.reason === "truncated"
      ? "最终 Diff 超出展示范围，未显示不完整补丁。"
      : parsed.reason === "invalid"
        ? "最终 Diff 格式无法识别。"
        : "最终 Diff 暂不可用。";
    return <p className="text-muted-foreground px-3 py-3 text-xs">{message}</p>;
  }
  return (
    <>
      {diff?.has_unrendered_changes && (
        <p className="text-muted-foreground px-3 py-2 text-xs">部分文件变化没有可显示的文本行 Diff（例如二进制内容、目录、符号链接或纯移动）。</p>
      )}
      <div className="aui-diff-view max-h-[45vh] space-y-3 overflow-auto text-xs">
        {parsed.files.map((file) => (
          <Diff key={`${file.oldPath}:${file.newPath}`} viewType="unified" diffType={file.type} hunks={file.hunks}>
            {(hunks) => hunks.map((hunk, index) => (
              <Hunk key={`${hunk.oldStart}:${hunk.newStart}:${index}`} hunk={hunk} />
            ))}
          </Diff>
        ))}
      </div>
    </>
  );
}

export function TaskChangeRow({
  change,
  isRunActive,
  actionBusy,
  result,
  onKeep,
  onRevert,
}: {
  change: TaskFileChange;
  isRunActive: boolean;
  actionBusy: boolean;
  result?: TaskChangeResult;
  onKeep: () => void;
  onRevert: () => void;
}) {
  const [diffOpen, setDiffOpen] = useState(false);
  const pending = change.status === "pending";
  const actionDisabled = isRunActive || actionBusy || !pending;
  const resultCopy = resultMessage(result);
  const displayPath = change.paths.length > 1
    ? change.action === "moved" && change.paths.length === 2
      ? change.paths.join(" → ")
      : `${change.paths[0]}（共 ${change.paths.length} 个路径）`
    : change.paths[0] ?? "未知路径";
  const netDiff = change.net_diff;

  return (
    <article className="overflow-hidden rounded-lg border bg-background">
      <div className="flex min-w-0 items-start gap-2 px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <p className="break-all font-mono text-xs leading-5">{displayPath}</p>
          <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
            <span>{statusLabel(change.status)}</span>
            <span aria-hidden="true">·</span>
            <span>{actionLabel(change.action)} · {change.operation_count} 次操作</span>
            {change.last_run_id !== null && <span>· Run #{change.last_run_id}</span>}
            {pending && netDiff?.state === "verified" && (
              <span className="inline-flex gap-1">
                {netDiff.has_unrendered_changes && netDiff.additions === 0 && netDiff.deletions === 0
                  ? <span>有文件变化</span>
                  : <><span className="text-emerald-600">+{netDiff.additions}</span><span className="text-destructive">−{netDiff.deletions}</span></>}
              </span>
            )}
          </div>
        </div>
        {pending && (
          <span className="flex shrink-0 items-center gap-1">
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              aria-label={`展开 ${displayPath} 的最终净 Diff`}
              aria-expanded={diffOpen}
              aria-controls={`task-change-diff-${change.change_id}`}
              onClick={() => setDiffOpen((open) => !open)}
            >
              {diffOpen ? <ChevronDownIcon /> : <ChevronRightIcon />}
            </Button>
          </span>
        )}
      </div>

      {pending && diffOpen && (
        <div id={`task-change-diff-${change.change_id}`} className="border-t">
          <div className="text-muted-foreground flex items-center justify-between gap-2 px-3 py-2 text-[11px]">
            <span>最终净 Diff · 基线 → 当前</span>
            {netDiff?.state === "verified" && (
              <span className="shrink-0">
                {netDiff.has_unrendered_changes && netDiff.additions === 0 && netDiff.deletions === 0
                  ? "有文件变化"
                  : <><span className="text-emerald-600">+{netDiff.additions}</span> <span className="text-destructive">−{netDiff.deletions}</span></>}
              </span>
            )}
          </div>
          <NetDiff diff={netDiff} contentSuppressed={change.action === "deleted"} />
        </div>
      )}

      {resultCopy && <p className="border-t px-3 py-2 text-xs text-muted-foreground">{resultCopy}</p>}
      {pending && (
        <div className="flex flex-wrap gap-2 border-t bg-muted/20 px-3 py-2">
          <Button type="button" size="sm" variant="outline" disabled={actionDisabled} onClick={onKeep}>
            <SaveIcon aria-hidden="true" />保留当前状态
          </Button>
          <Button type="button" size="sm" variant="destructive" disabled={actionDisabled} onClick={onRevert}>
            <RotateCcwIcon aria-hidden="true" />回退到基线
          </Button>
        </div>
      )}
    </article>
  );
}

export function TaskChangesPanel({ taskId, isRunActive }: TaskChangesPanelProps) {
  const {
    changeSet,
    loading,
    loadError,
    actionError,
    refreshing,
    actionBusy,
    resultsByChangeId,
    refresh,
    keep,
    revert,
  } = useTaskChanges(taskId, isRunActive);
  const [mobileOpen, setMobileOpen] = useState(true);
  const files = changeSet?.files ?? [];
  const pendingFiles = files.filter((file) => file.status === "pending");
  const visibleChangeIds = new Set(files.map((file) => file.change_id));
  const orphanedResults = [...resultsByChangeId.values()].filter((result) => !visibleChangeIds.has(result.change_id));

  const revertAll = () => {
    if (pendingFiles.length === 0 || isRunActive || actionBusy) return;
    const confirmed = window.confirm(`将 ${pendingFiles.length} 个待处理文件组统一回退到各自最近的基线。继续吗？`);
    if (confirmed) void revert(pendingFiles.map((file) => file.change_id));
  };

  return (
    <>
      <Button
        type="button"
        variant="outline"
        className="absolute right-3 top-3 z-10 hidden max-[900px]:inline-flex"
        onClick={() => setMobileOpen(true)}
      >
        文件变更{pendingFiles.length > 0 ? ` · ${pendingFiles.length}` : ""}
      </Button>
      <aside
        aria-label="任务文件变更"
        className={`flex h-full min-h-0 w-[min(40vw,30rem)] min-w-[21rem] shrink-0 flex-col border-l bg-background max-[900px]:absolute max-[900px]:inset-0 max-[900px]:z-20 max-[900px]:w-full max-[900px]:min-w-0 ${mobileOpen ? "max-[900px]:flex" : "max-[900px]:hidden"}`}
      >
        <header className="flex shrink-0 items-center justify-between gap-2 border-b px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">文件变更</h2>
            <p className="text-muted-foreground text-xs">
              {pendingFiles.length} 个待处理文件组
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {pendingFiles.length > 0 && (
              <Button
                type="button"
                size="sm"
                variant="destructive"
                disabled={isRunActive || actionBusy}
                onClick={revertAll}
              >
                <RotateCcwIcon aria-hidden="true" />全部回退到基线
              </Button>
            )}
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              aria-label="刷新文件变更"
              disabled={loading || refreshing || actionBusy}
              onClick={() => void refresh()}
            >
              <RefreshCwIcon className={refreshing ? "animate-spin" : ""} />
            </Button>
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              className="hidden max-[900px]:inline-flex"
              aria-label="关闭文件变更面板"
              onClick={() => setMobileOpen(false)}
            >
              <PanelRightCloseIcon />
            </Button>
          </div>
        </header>

        <p className="shrink-0 border-b bg-amber-500/5 px-4 py-2.5 text-xs leading-5 text-amber-900 dark:text-amber-200">
          当前追踪文件工具修改；终端命令可能产生未记录的文件变更。
        </p>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          {isRunActive && (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
              任务运行中，可以查看变更；Keep 和回退操作暂不可用。
            </p>
          )}
          {actionError && <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{actionError}</p>}
          {orphanedResults.length > 0 && (
            <div role="status" aria-live="polite" className="rounded-md border bg-muted/30 px-3 py-2 text-xs">
              <p className="font-medium">操作结果</p>
              <ul className="mt-1 space-y-1 text-muted-foreground">
                {orphanedResults.map((result) => <li key={result.change_id}><span className="font-mono">{result.change_id}</span>：{resultMessage(result)}</li>)}
              </ul>
            </div>
          )}
          {loading && !changeSet && <p className="px-2 py-4 text-center text-sm text-muted-foreground">正在加载文件变更…</p>}
          {loadError && (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm">
              <p className="text-destructive" role="alert">{loadError}</p>
              <Button type="button" size="sm" variant="outline" className="mt-2" disabled={refreshing} onClick={() => void refresh()}>重试</Button>
            </div>
          )}
          {!loading && !loadError && files.length === 0 && (
            <p className="px-2 py-8 text-center text-sm text-muted-foreground">当前任务没有可显示的文件变更。</p>
          )}
          {files.map((change) => (
            <TaskChangeRow
              key={change.change_id}
              change={change}
              isRunActive={isRunActive}
              actionBusy={actionBusy}
              result={resultsByChangeId.get(change.change_id)}
              onKeep={() => void keep([change.change_id])}
              onRevert={() => void revert([change.change_id])}
            />
          ))}
        </div>
      </aside>
    </>
  );
}
