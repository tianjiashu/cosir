import { useMemo } from "react";
import { Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData } from "react-diff-view";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import { ToolIcon } from "./tool-icons";
import { DisclosureRow, DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { useToolDisclosure } from "./tool-disclosure";

type Change = {
  path: string;
  new_path?: string | null;
  status?: string;
  patch?: string | null;
  truncated?: boolean;
  insertions?: number;
  deletions?: number;
};

type ParsedFileDiff =
  | { kind: "ready"; file: FileData }
  | { kind: "empty" }
  | { kind: "unavailable"; reason: "truncated" | "invalid" | "missing" };

function readChanges(value: unknown): Change[] {
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

export function parseFileDiff(change: Change): ParsedFileDiff {
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

function FileDiff({ change }: { change: Change }) {
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

  // 删除只展示目标与状态：被删内容属于回退事实（变更集 before-image），不进入工具展示。
  // 即使历史载荷里仍带有旧的删除 Diff，这里也不再解析渲染，保证展示口径一致。
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

  const { file } = parsed;

  return (
    <Diff viewType="split" diffType={file.type} hunks={file.hunks} className="aui-diff-view overflow-auto text-xs">
      {(hunks) => hunks.map((hunk, index) => <Hunk key={`${hunk.oldStart}:${hunk.newStart}:${index}`} hunk={hunk} />)}
    </Diff>
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
  const defaultOpen = hasDisplayData && artifact.presentation.default_open === true;
  const isTerminalState = artifact.backendStatus === "failed" || artifact.backendStatus === "cancelled";
  const [open, setOpen] = useToolDisclosure(artifact.backendStatus, defaultOpen, { openWhileRunning: false });

  const rowProps = {
      leading: <ToolIcon name={artifact.presentation.icon} aria-hidden="true" />,
      label: <span className="text-foreground text-sm font-medium">{title}</span>,
      meta: isTerminalState ? (
        <span className="text-destructive">{artifact.error ?? "执行失败"}</span>
      ) : hasDisplayData ? (
        <>
          <span className="text-muted-foreground">{totalFiles} 个文件</span>
          {showInsertions && <span className="ml-2 text-emerald-600">+{insertions}</span>}
          {showDeletions && <span className="ml-2 text-destructive">−{deletions}</span>}
        </>
      ) : undefined,
      trailing: <ToolStatus status={artifact.backendStatus} />,
  };

  if (!hasDisplayData || artifact.presentation.expandable === false) {
    return <DisclosureRowStatic {...rowProps} className="group/tool-call" />;
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="group/tool-call">
      <DisclosureRow {...rowProps} />
      <CollapsibleContent className="ml-6 space-y-2 pl-2 pt-2">
        {isTerminalState ? (
          <p className="text-destructive px-2 text-xs">{artifact.error ?? "执行失败"}</p>
        ) : (
          <>
            {changes.length === 0 && <p className="text-muted-foreground px-2 text-xs">后端没有返回可展示的 Diff。</p>}
            {changes.map((change) => (
          <section key={`${change.path}:${change.new_path ?? ""}`} className="overflow-hidden">
            <header className="flex items-center justify-between gap-2 bg-muted/30 px-2.5 py-1.5 text-xs">
              <span className="flex min-w-0 items-center gap-2">
                <span className={statusBadgeClass(change.status)}>{statusLabel(change.status)}</span>
                {change.status === "moved" ? (
                  <span className="truncate font-medium" title={`${change.path} → ${change.new_path ?? ""}`}>
                    {change.path}<span className="mx-1 text-muted-foreground" aria-hidden="true">→</span>{change.new_path ?? "目标路径不可用"}
                  </span>
                ) : (
                  <span className="truncate font-medium">{change.path}</span>
                )}
              </span>
              {change.status !== "deleted" && (Boolean(change.insertions) || Boolean(change.deletions)) && (
                <span className="shrink-0">
                  {Boolean(change.insertions) && <span className="text-emerald-600">+{change.insertions}</span>}
                  {Boolean(change.insertions) && Boolean(change.deletions) && " "}
                  {Boolean(change.deletions) && <span className="text-destructive">−{change.deletions}</span>}
                </span>
              )}
            </header>
            <FileDiff change={change} />
          </section>
            ))}
          </>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

function statusLabel(status: string | undefined): string {
  switch (status) {
    case "added": return "新增";
    case "deleted": return "已删除";
    case "moved": return "已移动";
    case "modified": return "修改";
    default: return "文件变更";
  }
}

function statusBadgeClass(status: string | undefined): string {
  const base = "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium";
  switch (status) {
    case "added": return `${base} bg-emerald-500/10 text-emerald-700 dark:text-emerald-400`;
    case "deleted": return `${base} bg-destructive/10 text-destructive`;
    case "moved": return `${base} bg-blue-500/10 text-blue-700 dark:text-blue-400`;
    default: return `${base} bg-muted text-muted-foreground`;
  }
}
