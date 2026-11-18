import { useMemo } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData } from "react-diff-view";

import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { cn } from "@/lib/utils";
import type { CallView } from "../call-view";
import { readFileChanges, type FileChange } from "@/lib/assistant/tools/file-changes";
import { DetailBudget } from "../detail-budget";

type ParsedFileDiff =
  | { kind: "ready"; file: FileData; }
  | { kind: "empty"; }
  | { kind: "unavailable"; reason: "invalid" | "omitted"; };

export function parseFileDiff(change: FileChange): ParsedFileDiff {
  // null：超出展示上限、后端没有下发；空串：内容没变（如写入相同内容）。
  if (change.patch === null) return { kind: "unavailable", reason: "omitted" };
  if (!change.patch) return { kind: "empty" };
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

/** 一个文件的 diff 正文；调用行展开体与「本轮改动」卡点开文件时共用。 */
export function FileDiff({ change }: { change: FileChange; }) {
  const parsed = useMemo(() => parseFileDiff(change), [change]);
  if (parsed.kind === "empty") return <p className="text-muted-foreground px-3 py-2 text-xs">没有文本差异</p>;
  if (parsed.kind === "unavailable") {
    return <p className="text-muted-foreground px-3 py-2 text-xs">{parsed.reason === "omitted" ? "改动过大，卡片里不保留 diff，可在文件面板查看" : "Diff 解析失败"}</p>;
  }
  return <ParsedDiff file={parsed.file} />;
}

/** 上下统一视图：对话列宽约 700px，左右分栏每侧只剩一半，长行几乎都要横向滚动。高度上限归 `DetailBudget`。 */
function ParsedDiff({ file }: { file: FileData; }) {
  return (
    <DetailBudget>
      <Diff viewType="unified" diffType={file.type} hunks={file.hunks} className="aui-diff-view text-xs">
        {(hunks) => hunks.map((hunk, index) => <Hunk key={`${hunk.oldStart}:${hunk.newStart}:${index}`} hunk={hunk} />)}
      </Diff>
    </DetailBudget>
  );
}

/** 新建的灰字标签；修改是常态，不标。删除与移动走终端，不进文件变更卡片。 */
export function fileChangeTag(status: string | undefined): string | null {
  return status === "added" ? "新建" : null;
}

/** 行内 `+a −b`；没有增删时不显示。 */
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
 * `file-changes`（`write_file` / `edit_file` / `apply_patch`）：元信息是 `+a −b`（单个新建文件前加「新建」，
 * 修改是常态不标），没有增删可数时改用文件数或动作标签；单个文件时目标是可点开的路径。展开体逐文件给 diff。
 * 回合落定后同一批改动还会按文件聚合进产出区的改动卡，这里是过程视角。补丁部分写入后失败时，错误观察也带着
 * 已写入的这批改动（契约第 4 节），同样按这里显示。
 */
export const fileChangesView: CallView = (data) => {
  const changes = readFileChanges(data.changes);
  // 总数只由逐文件计数相加（后端不另下发汇总）。
  const insertions = changes.reduce((sum, item) => sum + item.insertions, 0);
  const deletions = changes.reduce((sum, item) => sum + item.deletions, 0);
  const tag = changes.length === 1 ? fileChangeTag(changes[0].status) : null;
  const meta = insertions <= 0 && deletions <= 0
    ? changes.length > 1 ? `${changes.length} 个文件` : tag
    : <>{tag}<DiffStat insertions={insertions} deletions={deletions} /></>;
  return {
    target: changes.length === 1 ? <WorkspacePathLink path={changes[0].path} plain /> : undefined,
    meta,
    detail: changes.length > 0 ? {
      render: () => (
        <div className="space-y-1 py-1">
          {changes.map((change) => <FileChangeSection key={change.index} change={change} single={changes.length === 1} />)}
        </div>
      ),
    } : undefined,
  };
};
