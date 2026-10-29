import { useMemo, useState } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData } from "react-diff-view";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { cn } from "@/lib/utils";
import { asRecord, readToolArtifact } from "./types";
import { ToolCardShell } from "./tool-card-shell";
import {
  FileOperationCard,
  FileChangeStatusBadge,
  type FileChange,
} from "./file-operation-card";

type ParsedFileDiff =
  | { kind: "ready"; file: FileData }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: "truncated" | "invalid" | "missing" };

function readChanges(value: unknown): FileChange[] {
  if (!Array.isArray(value)) return [];
  return value.map(asRecord).filter((change) => typeof change.path === "string").map((change) => ({
    path: String(change.path),
    new_path: typeof change.new_path === "string" ? change.new_path : null,
    status: typeof change.status === "string" ? change.status : undefined,
    patch: typeof change.patch === "string" ? change.patch : null,
    truncated: change.truncated === true,
    insertions: typeof change.insertions === "number" ? change.insertions : 0,
    deletions: typeof change.deletions === "number" ? change.deletions : 0,
  }));
}

export function parseFileDiff(change: FileChange): ParsedFileDiff {
  if (change.truncated) return { kind: "unavailable", reason: "truncated" };
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

function FileDiff({ change }: { change: FileChange }) {
  const parsed = useMemo(() => parseFileDiff(change), [change]);
  if (change.status === "moved") {
    return (
      <p className="px-3 py-2 text-xs text-muted-foreground">
        <span className="font-medium">已移动：</span>
        <span className="break-all">{change.path}</span>
        <span className="mx-1" aria-hidden="true">→</span>
        <span className="break-all">{change.new_path ?? "目标路径不可用"}</span>
      </p>
    );
  }

  // 删除只展示目标与状态，被删内容不进入工具展示。
  // 即使载荷里带有删除 Diff，这里也不解析渲染，保证展示口径一致。
  if (change.status === "deleted") {
    return (
      <p className="px-3 py-2 text-xs text-muted-foreground">
        <span className="font-medium">已删除：</span>
        <span className="break-all">{change.path}</span>
      </p>
    );
  }

  if (parsed.kind === "empty") {
    return <p className="text-muted-foreground px-3 py-2 text-xs">没有文本差异</p>;
  }
  if (parsed.kind === "unavailable") {
    const message = parsed.reason === "truncated"
      ? "Diff 内容过大，无法完整展示"
      : parsed.reason === "missing"
        ? "Diff 数据不可用"
        : "Diff 解析失败";
    return <p className="text-muted-foreground px-3 py-2 text-xs">{message}</p>;
  }

  return <ParsedDiff file={parsed.file} />;
}

/** 超过这个变更行数的 Diff 默认限高，内部滚动，可一键展开全部。 */
const COLLAPSED_DIFF_LINE_LIMIT = 24;

/**
 * 上下统一视图：对话列宽约 700px，左右分栏每侧只剩一半，长行几乎都要横向滚动。
 * 长 Diff 默认限高，避免一次写入把整条回复撑得很长。
 */
function ParsedDiff({ file }: { file: FileData }) {
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
          className="text-muted-foreground hover:text-foreground w-full border-t px-2.5 py-1 text-left text-xs"
        >
          {expanded ? "收起" : `展开全部（${lineCount} 行）`}
        </button>
      )}
    </div>
  );
}

export function DiffTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.display_data ?? {};
  const changes = readChanges(data.changes);
  const stats = asRecord(data.diff_stats);
  const totalFiles = typeof stats.total_files === "number" ? stats.total_files : changes.length;
  const insertions = typeof stats.total_insertions === "number" ? stats.total_insertions : changes.reduce((sum, item) => sum + (item.insertions ?? 0), 0);
  const deletions = typeof stats.total_deletions === "number" ? stats.total_deletions : changes.reduce((sum, item) => sum + (item.deletions ?? 0), 0);
  // 删除只展示目标与状态：载荷全部是删除时连增删统计也不显示（历史载荷可能仍带计数）。
  const onlyDeletedChanges = changes.length > 0 && changes.every((change) => change.status === "deleted");
  const showInsertions = insertions > 0 && !onlyDeletedChanges;
  const showDeletions = deletions > 0 && !onlyDeletedChanges;
  const title = artifact.presentation.verb ?? toolName;
  const hasDisplayData = data.kind === "file-changes";

  const isCompactFileOperation = hasDisplayData
    && changes.length > 0
    && changes.every((change) => change.status === "deleted" || change.status === "moved");

  if (isCompactFileOperation) {
    return (
      <FileOperationCard
        title={title}
        icon={artifact.presentation.icon}
        changes={changes}
        totalFiles={totalFiles}
        status={artifact.backendStatus}
        error={artifact.error}
      />
    );
  }

  return (
    <ToolCardShell
      artifact={artifact}
      title={title}
      summary={hasDisplayData ? (
        <>
          <span>{totalFiles} 个文件</span>
          {showInsertions && <span className="text-success ml-2">+{insertions}</span>}
          {showDeletions && <span className="ml-2 text-destructive">−{deletions}</span>}
        </>
      ) : undefined}
      defaultOpen={hasDisplayData && artifact.presentation.default_open === true}
      body={hasDisplayData ? (
        <div className="space-y-2 pt-2">
          {changes.length === 0 && <p className="text-muted-foreground px-2 text-xs">后端没有返回可展示的 Diff。</p>}
          {changes.map((change) => <FileChangeSection key={`${change.path}:${change.new_path ?? ""}`} change={change} />)}
        </div>
      ) : undefined}
    />
  );
}

function FileChangeSection({ change }: { change: FileChange }) {
  return (
    <section className="overflow-hidden">
      <header className="flex items-center justify-between gap-2 bg-muted/30 px-2.5 py-1.5 text-xs">
        <span className="flex min-w-0 items-center gap-2">
          <FileChangeStatusBadge status={change.status} />
          {change.status === "moved" ? (
            <span className="truncate font-medium" title={`${change.path} → ${change.new_path ?? ""}`}>
              {change.path}<span className="mx-1 text-muted-foreground" aria-hidden="true">→</span>{change.new_path ?? "目标路径不可用"}
            </span>
          ) : (
            <WorkspacePathLink path={change.path} className="font-medium" />
          )}
        </span>
        {change.status !== "deleted" && (Boolean(change.insertions) || Boolean(change.deletions)) && (
          <span className="shrink-0">
            {Boolean(change.insertions) && <span className="text-success">+{change.insertions}</span>}
            {Boolean(change.insertions) && Boolean(change.deletions) && " "}
            {Boolean(change.deletions) && <span className="text-destructive">−{change.deletions}</span>}
          </span>
        )}
      </header>
      <FileDiff change={change} />
    </section>
  );
}
