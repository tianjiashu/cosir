import { ChevronDownIcon, FileIcon, FolderIcon, LinkIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { asRecord, displayValue, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";

function toolTitle(toolName: string, verb?: string): string {
  return verb ?? toolName;
}

function ListEntries({ data }: { data: Record<string, unknown> }) {
  const entries = Array.isArray(data.entries) ? data.entries : [];
  const files = Array.isArray(data.files) ? data.files : [];
  if (entries.length === 0 && files.length === 0) {
    return <p className="text-muted-foreground text-xs">未找到匹配</p>;
  }
  return (
    <ul className="divide-border/60 divide-y rounded-lg border text-xs">
      {entries.map((entry, index) => {
        const item = asRecord(entry);
        const type = item.type;
        const Icon = type === "dir" ? FolderIcon : type === "link" ? LinkIcon : FileIcon;
        return (
          <li key={`${String(item.path ?? item.name ?? index)}-${index}`} className="flex items-center gap-2 px-2.5 py-1.5">
            <Icon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{String(item.name ?? item.path ?? "未知条目")}</span>
            {typeof type === "string" && <span className="text-muted-foreground ml-auto">{type}</span>}
          </li>
        );
      })}
      {files.map((entry, index) => {
        const item = asRecord(entry);
        const path = String(item.path ?? "未知文件");
        return <li key={`${path}-${index}`} className="truncate px-2.5 py-1.5">{path}</li>;
      })}
    </ul>
  );
}

export function DetailsTool({ toolName, argsText, result, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const presentation = artifact.presentation;
  const path = typeof data.path === "string" ? data.path : undefined;
  const pattern = typeof data.pattern === "string" ? data.pattern : undefined;
  const title = toolTitle(toolName, presentation.verb);
  const summary = path ?? (pattern ? `${pattern}${typeof data.target === "string" ? ` · ${data.target}` : ""}` : "");
  const expandable = presentation.expandable !== false;
  const defaultOpen = artifact.backendStatus === "running" || presentation.default_open === true;
  const isList = presentation.expand_layout === "list" || data.kind === "directory-list" || data.kind === "file-list";

  const body = (
    <div className="space-y-2 px-3 pb-3 text-sm">
      {isList ? <ListEntries data={data} /> : data.kind === "read-file-meta" ? (
        <p className="text-muted-foreground text-xs">
          {data.offset !== undefined && data.limit !== undefined ? `第 ${String(data.offset)}–${String(Number(data.offset) + Number(data.limit) - 1)} 行` : "读取范围由后端提供"}
          {data.total_lines !== undefined ? ` · 共 ${String(data.total_lines)} 行` : ""}
          {data.next_offset !== null && data.next_offset !== undefined ? " · 还有更多内容" : ""}
        </p>
      ) : (
        <>
          {artifact.error && <p className="text-destructive text-xs">{artifact.error}</p>}
          {!artifact.error && result !== undefined && <pre className="max-h-64 overflow-auto rounded-lg bg-muted/50 p-2 text-xs">{displayValue(result)}</pre>}
          {argsText && <pre className="max-h-48 overflow-auto rounded-lg bg-muted/50 p-2 text-xs">{argsText}</pre>}
        </>
      )}
    </div>
  );

  const trigger = (
    <div className="flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left">
      <span className="truncate text-sm font-medium">{title}</span>
      {summary && <span className="text-muted-foreground min-w-0 truncate text-xs">{summary}</span>}
      <span className="ml-auto shrink-0"><ToolStatus status={artifact.backendStatus} /></span>
    </div>
  );

  if (!expandable) {
    return <div className={cn("flex min-w-0 items-center", artifact.backendStatus === "failed" && "text-destructive")}>{trigger}</div>;
  }

  return (
    <Collapsible defaultOpen={defaultOpen} className="group/tool-call overflow-hidden">
      <CollapsibleTrigger className="flex w-full items-center text-left text-muted-foreground transition-colors hover:text-foreground">
        <ChevronDownIcon className="size-4 shrink-0 transition-transform duration-200 group-data-open/tool-call:rotate-0 group-data-[state=closed]/tool-call:-rotate-90" aria-hidden="true" />
        {trigger}
      </CollapsibleTrigger>
      <CollapsibleContent className="ml-6 pl-2">{body}</CollapsibleContent>
    </Collapsible>
  );
}
