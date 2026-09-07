import { useMemo } from "react";
import { createTwoFilesPatch } from "diff";
import { Diff, Hunk, parseDiff } from "react-diff-view";
import { ChevronDownIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

type Change = {
  path: string;
  new_path?: string | null;
  status?: string;
  before?: string;
  after?: string;
  insertions?: number;
  deletions?: number;
};

function readChanges(value: unknown): Change[] {
  if (!Array.isArray(value)) return [];
  return value.map(asRecord).filter((change) => typeof change.path === "string").map((change) => ({
    path: String(change.path),
    new_path: typeof change.new_path === "string" ? change.new_path : null,
    status: typeof change.status === "string" ? change.status : undefined,
    before: typeof change.before === "string" ? change.before : "",
    after: typeof change.after === "string" ? change.after : "",
    insertions: typeof change.insertions === "number" ? change.insertions : 0,
    deletions: typeof change.deletions === "number" ? change.deletions : 0,
  }));
}

function FileDiff({ change }: { change: Change }) {
  const file = useMemo(() => {
    const patch = createTwoFilesPatch(change.path, change.new_path ?? change.path, change.before ?? "", change.after ?? "", "", "", { context: 3 });
    return parseDiff(patch)[0];
  }, [change]);

  if (!file || file.hunks.length === 0) {
    return <p className="text-muted-foreground px-3 py-2 text-xs">没有文本差异</p>;
  }

  return (
    <Diff viewType="split" diffType={file.type} hunks={file.hunks} className="aui-diff-view overflow-auto text-xs">
      {(hunks) => hunks.map((hunk) => <Hunk key={hunk.content} hunk={hunk} />)}
    </Diff>
  );
}

export function DiffTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const changes = readChanges(data.changes);
  const stats = asRecord(data.diff_stats);
  const totalFiles = typeof stats.total_files === "number" ? stats.total_files : changes.length;
  const insertions = typeof stats.total_insertions === "number" ? stats.total_insertions : changes.reduce((sum, item) => sum + (item.insertions ?? 0), 0);
  const deletions = typeof stats.total_deletions === "number" ? stats.total_deletions : changes.reduce((sum, item) => sum + (item.deletions ?? 0), 0);
  const title = artifact.presentation.verb ?? toolName;
  const defaultOpen = artifact.backendStatus === "running" || artifact.presentation.default_open === true;

  return (
    <Collapsible defaultOpen={defaultOpen} className="group/tool-call overflow-hidden">
      <CollapsibleTrigger className="flex w-full items-center gap-2 py-1.5 text-left text-muted-foreground transition-colors hover:text-foreground">
        <ChevronDownIcon className="size-4 shrink-0 transition-transform duration-200 group-data-open/tool-call:rotate-0 group-data-[state=closed]/tool-call:-rotate-90" aria-hidden="true" />
        <span className="text-foreground truncate text-sm font-medium">{title}</span>
        <span className="text-muted-foreground text-xs">{totalFiles} 个文件</span>
        <span className="text-emerald-600 text-xs">+{insertions}</span>
        <span className="text-destructive text-xs">−{deletions}</span>
        <span className="ml-auto"><ToolStatus status={artifact.backendStatus} /></span>
      </CollapsibleTrigger>
      <CollapsibleContent className="ml-6 space-y-2 pl-2 pt-2">
        {artifact.error && <p className="text-destructive px-2 text-xs">{artifact.error}</p>}
        {changes.length === 0 && !artifact.error && <p className="text-muted-foreground px-2 text-xs">后端没有返回可展示的 Diff。</p>}
        {changes.map((change) => (
          <section key={`${change.path}:${change.new_path ?? ""}`} className="overflow-hidden rounded-md border">
            <header className="flex items-center justify-between gap-2 bg-muted/40 px-2.5 py-1.5 text-xs">
              <span className="truncate font-medium">{change.new_path ?? change.path}</span>
              <span className="shrink-0"><span className="text-emerald-600">+{change.insertions ?? 0}</span>{" "}<span className="text-destructive">−{change.deletions ?? 0}</span></span>
            </header>
            <FileDiff change={change} />
          </section>
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}
