import { FileIcon, FolderIcon, LinkIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { DisclosureRow, DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { asRecord, readToolArtifact, safeExternalUrl } from "./types";
import { ToolStatus } from "./tool-status";
import { ToolIcon } from "./tool-icons";
import { useToolDisclosure } from "./tool-disclosure";

function toolTitle(toolName: string, verb?: string): string {
  return verb ?? toolName;
}

function ListEntries({ data }: { data: Record<string, unknown> }) {
  const isWebSearch = data.kind === "web-search-results";
  const isWebExtract = data.kind === "web-extract-urls";
  const entries = !isWebSearch && !isWebExtract && Array.isArray(data.entries) ? data.entries : [];
  const files = !isWebSearch && !isWebExtract && Array.isArray(data.files) ? data.files : [];
  const results = isWebSearch && Array.isArray(data.results) ? data.results : [];
  const urls = Array.isArray(data.urls) ? data.urls : [];
  if (entries.length === 0 && files.length === 0 && results.length === 0 && urls.length === 0) {
    const emptyMessage = data.kind === "directory-list"
      ? data.total_entries === 0 ? "目录为空" : "当前页没有条目"
      : "未找到匹配";
    return <p className="text-muted-foreground text-xs">{emptyMessage}</p>;
  }
  return (
    <div className="space-y-2">
      {typeof data.status_hint === "string" && <p className="text-muted-foreground text-xs">{data.status_hint}</p>}
      <ul className="space-y-0.5 text-xs">
      {entries.map((entry, index) => {
        const item = asRecord(entry);
        const type = item.type;
        const Icon = type === "dir" ? FolderIcon : type === "link" ? LinkIcon : FileIcon;
        const label = String(item.name ?? item.path ?? "未知条目");
        const target = type === "link" ? safeExternalUrl(item.path) : null;
        const labelNode = target ? (
          <a href={target} target="_blank" rel="noreferrer" className="truncate hover:text-foreground">
            {label}
          </a>
        ) : (
          <span className="truncate">{label}</span>
        );
        return (
          <li key={`${String(item.path ?? item.name ?? index)}-${index}`} className="flex items-center gap-2 px-2.5 py-1.5">
            <Icon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
            {labelNode}
            {typeof type === "string" && <span className="text-muted-foreground ml-auto">{type}</span>}
          </li>
        );
      })}
      {files.map((entry, index) => {
        const item = asRecord(entry);
        const path = String(item.path ?? "未知文件");
        return <li key={`${path}-${index}`} className="truncate px-2.5 py-1.5">{path}</li>;
      })}
      {results.map((entry, index) => {
        const item = asRecord(entry);
        const rawUrl = typeof item.url === "string" ? item.url : "";
        const url = safeExternalUrl(rawUrl);
        const title = typeof item.title === "string" && item.title ? item.title : rawUrl;
        if (!rawUrl) return null;
        return (
          <li key={`${url}-${index}`} className="flex items-center gap-2 px-2.5 py-1.5">
            <LinkIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
            {url ? <a href={url} target="_blank" rel="noreferrer" className="truncate hover:text-foreground" title={url}>{title}</a> : <span className="truncate" title={rawUrl}>{title}</span>}
          </li>
        );
      })}
      {urls.map((entry, index) => {
        const item = asRecord(entry);
        const rawUrl = typeof item.url === "string" ? item.url : "";
        const url = safeExternalUrl(rawUrl);
        if (!rawUrl) return null;
        let title = rawUrl;
        let favicon = "";
        if (url) {
          const parsed = new URL(url);
          title = parsed.hostname;
          favicon = `${parsed.origin}/favicon.ico`;
        }
        return (
          <li key={`${url}-${index}`} className="flex items-center gap-2 px-2.5 py-1.5">
            {favicon ? (
              <img src={favicon} alt="" className="size-3.5 shrink-0" onError={(event) => { event.currentTarget.style.display = "none"; }} />
            ) : (
              <LinkIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
            )}
            {url ? <a href={url} target="_blank" rel="noreferrer" className="truncate hover:text-foreground" title={url}>{title}</a> : <span className="truncate" title={rawUrl}>{title}</span>}
          </li>
        );
      })}
      </ul>
    </div>
  );
}

export function DetailsTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.data ?? {};
  const presentation = artifact.presentation;
  const path = typeof data.path === "string" ? data.path : undefined;
  const pattern = typeof data.pattern === "string" ? data.pattern : undefined;
  const title = toolTitle(toolName, presentation.verb);
  const summary = path ?? (pattern ? `${pattern}${typeof data.target === "string" ? ` · ${data.target}` : ""}` : "");
  const expandable = presentation.expandable !== false;
  const defaultOpen = artifact.backendStatus === "running" || presentation.default_open === true;
  const isList = presentation.expand_layout === "list" || data.kind === "directory-list" || data.kind === "file-list" || data.kind === "web-search-results" || data.kind === "web-extract-urls" || Array.isArray(data.entries);
  const isTerminalState = artifact.backendStatus === "failed" || artifact.backendStatus === "cancelled";
  const lineRange = asRecord(data.line_range);
  const readFileRange = data.file_size === 0
    ? "空文件"
    : typeof lineRange.start === "number" && lineRange.end === "END"
      ? `L${String(lineRange.start)}-END`
      : typeof lineRange.start === "number" && typeof lineRange.end === "number"
        ? `L${String(lineRange.start)}-L${String(lineRange.end)}`
        : lineRange.end === null
          ? "指定范围没有返回内容"
          : "读取范围由后端提供";

  const body = (
    <div className="space-y-2 px-3 pb-3 text-sm">
      {isTerminalState ? (
        <p className="text-destructive text-xs">{artifact.backendStatus === "cancelled" ? "已取消" : artifact.error ?? "执行失败"}</p>
      ) : isList ? <ListEntries data={data} /> : data.kind === "read-file-meta" ? (
        <p className="text-muted-foreground text-xs">
          {readFileRange}
          {typeof data.file_size === "number" ? ` · ${String(data.file_size)} B` : ""}
          {data.truncated === true ? ` · ${String(data.status_hint ?? "文件过大，已截断")}` : ""}
        </p>
      ) : data.kind === "delegation-result" ? (
        <p className="text-muted-foreground text-xs">
          {typeof data.child_agent_id === "string" ? data.child_agent_id : "子 Agent"}
          {typeof data.title === "string" && data.title ? ` · ${data.title}` : ""}
        </p>
      ) : (
        <>
          {presentation.show_result !== false && <p className="text-muted-foreground text-xs">暂无展示数据</p>}
        </>
      )}
    </div>
  );

  const rowProps = {
    leading: <ToolIcon name={presentation.icon} aria-hidden="true" />,
    label: <span className="text-sm font-medium">{title}</span>,
    meta: <span className="text-muted-foreground max-w-[45%] truncate">{artifact.error ?? summary}</span>,
    trailing: <ToolStatus status={artifact.backendStatus} />,
  };

  const [open, setOpen] = useToolDisclosure(artifact.backendStatus, defaultOpen);

  if (!expandable) {
    return <DisclosureRowStatic {...rowProps} className={cn(artifact.backendStatus === "failed" && "text-destructive")} />;
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="group/tool-call">
      <DisclosureRow {...rowProps} />
      <CollapsibleContent className="ml-6 pl-2">{body}</CollapsibleContent>
    </Collapsible>
  );
}
