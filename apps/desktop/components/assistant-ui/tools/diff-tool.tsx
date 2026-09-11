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
          <span className="ml-2 text-emerald-600">+{insertions}</span>
          <span className="ml-2 text-destructive">−{deletions}</span>
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
              <span className="truncate font-medium">{change.new_path ?? change.path}</span>
              <span className="shrink-0"><span className="text-emerald-600">+{change.insertions ?? 0}</span>{" "}<span className="text-destructive">−{change.deletions ?? 0}</span></span>
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
