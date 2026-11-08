import { useMemo, useState } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData } from "react-diff-view";

import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { cn } from "@/lib/utils";
import type { CallView } from "../call-view";
import { readFileChanges, type FileChange } from "@/lib/assistant/file-changes";
import { asRecord } from "../display-data-readers";

type ParsedFileDiff =
  | { kind: "ready"; file: FileData; }
  | { kind: "empty"; }
  | { kind: "unavailable"; reason: "invalid" | "missing"; };

export function parseFileDiff(change: FileChange): ParsedFileDiff {
  if (!change.patch) return { kind: "unavailable", reason: "missing" };
  const isRenamePatch = change.patch.includes("\nsimilarity index ")
    && change.patch.includes("\nrename from ")
    && change.patch.includes("\nrename to ");
  if (!change.patch.startsWith("diff --git ")) {
    return { kind: "unavailable", reason: "invalid" };
  }
  if (isRenamePatch) return { kind: "empty" };
  if (!change.patch.includes("\n--- ") || !change.patch.includes("\n+++ ")) {
    return { kind: "unavailable", reason: "invalid" };
  }

  try {
    const file = parseDiff(change.patch)[0];
    if (!file || file.hunks.length === 0) return { kind: "empty" };
    return { kind: "ready", file };
  } catch {
    return { kind: "unavailable", reason: "invalid" };
  }
}

/** 删除与移动没有文本差异可看：被删内容不进入工具展示，行上的目标已说清移动去向。 */
export function hasTextDiff(change: FileChange): boolean {
  return change.status !== "deleted" && change.status !== "moved";
}

/** 一个文件的 diff 正文；调用行展开体与「本轮改动」卡点开文件时共用。 */
export function FileDiff({ change }: { change: FileChange; }) {
  const parsed = useMemo(() => parseFileDiff(change), [change]);
  if (parsed.kind === "empty") return <p className="text-muted-foreground px-3 py-2 text-xs">没有文本差异</p>;
  if (parsed.kind === "unavailable") {
    return <p className="text-muted-foreground px-3 py-2 text-xs">{parsed.reason === "missing" ? "Diff 数据不可用" : "Diff 解析失败"}</p>;
  }
  return <ParsedDiff file={parsed.file} />;
}

/** 超过这个变更行数的 diff 默认限高，内部滚动，可一键展开全部。 */
const COLLAPSED_DIFF_LINE_LIMIT = 24;

/**
 * 上下统一视图：对话列宽约 700px，左右分栏每侧只剩一半，长行几乎都要横向滚动。
 * 长 diff 默认限高，避免一次写入把整条回复撑得很长。
 */
function ParsedDiff({ file }: { file: FileData; }) {
  const lineCount = useMemo(() => file.hunks.reduce((sum, hunk) => sum + hunk.changes.length, 0), [file]);
  const collapsible = lineCount > COLLAPSED_DIFF_LINE_LIMIT;
  const [expanded, setExpanded] = useState(false);
  return (
    <div>
      <div className={cn("overflow-auto", collapsible && !expanded && "max-h-96")}>
        <Diff viewType="unified" diffType={file.type} hunks={file.hunks} className="aui-diff-view text-xs">
          {(hunks) => hunks.map((hunk, index) => <Hunk key={`${hunk.oldStart}:${hunk.newStart}:${index}`} hunk={hunk} />)}
        </Diff>
      </div>
      {collapsible && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="text-muted-foreground hover:text-foreground w-full px-2.5 py-1 text-left text-xs"
        >
          {expanded ? "收起" : `展开全部（${lineCount} 行）`}
        </button>
      )}
    </div>
  );
}

const STATUS_LABELS: Record<string, string> = { added: "新建", deleted: "删除", moved: "移动" };

/** 新建 / 删除 / 移动的灰字标签；修改是常态，不标。 */
export function fileChangeTag(status: string | undefined): string | null {
  return status === undefined ? null : STATUS_LABELS[status] ?? null;
}

/** 行内 `+a −b`；只有删除时不显示计数（被删内容不进展示）。 */
export function DiffStat({ insertions, deletions, className }: { insertions: number; deletions: number; className?: string; }) {
  if (insertions <= 0 && deletions <= 0) return null;
  return (
    <span className={cn("shrink-0 font-mono text-xs tabular-nums", className)}>
      {insertions > 0 && <span className="text-success">+{insertions}</span>}
      {insertions > 0 && deletions > 0 && " "}
      {deletions > 0 && <span className="text-destructive">−{deletions}</span>}
    </span>
  );
}

function FileChangeSection({ change, single }: { change: FileChange; single: boolean; }) {
  const tag = fileChangeTag(change.status);
  return (
    <section>
      <header className="flex items-center justify-between gap-2 py-1.5 text-xs">
        <span className="flex min-w-0 items-center gap-2">
          <WorkspacePathLink path={change.path} className="font-mono" />
          {tag && <span className="text-muted-foreground shrink-0 text-2xs">{tag}</span>}
        </span>
        {!single && <DiffStat insertions={change.insertions} deletions={change.deletions} />}
      </header>
      <FileDiff change={change} />
    </section>
  );
}

/**
 * `file-changes`（`write_file` / `patch_write` / `apply_patch` / `delete_file` / `move_file`）：元信息是 `+a −b`
 * 或文件数，展开体逐文件给 diff。回合结束后同一批改动还会按文件聚合进「本轮改动」卡，这里是过程视角。
 */
export const fileChangesView: CallView = (data) => {
  const changes = readFileChanges(data.changes);
  const textual = changes.filter(hasTextDiff);
  const stats = asRecord(data.diff_stats);
  const insertions = typeof stats.total_insertions === "number" ? stats.total_insertions : changes.reduce((sum, item) => sum + item.insertions, 0);
  const deletions = typeof stats.total_deletions === "number" ? stats.total_deletions : changes.reduce((sum, item) => sum + item.deletions, 0);
  // 全是删除 / 移动时没有增删可数，改用文件数与动作标签。
  const onlyOperations = changes.length > 0 && changes.every((change) => change.status === "deleted" || change.status === "moved");
  const count = typeof stats.total_files === "number" ? stats.total_files : changes.length;
  const meta = onlyOperations || (insertions <= 0 && deletions <= 0)
    ? count > 1 ? `${count} 个文件` : fileChangeTag(changes[0]?.status) ?? null
    : <DiffStat insertions={insertions} deletions={deletions} />;
  return {
    meta,
    detail: textual.length > 0 ? {
      render: () => (
        <div className="space-y-1 py-1">
          {textual.map((change) => <FileChangeSection key={change.index} change={change} single={textual.length === 1} />)}
        </div>
      ),
    } : undefined,
  };
};
