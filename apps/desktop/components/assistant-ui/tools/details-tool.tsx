import { FileIcon, FolderIcon, LinkIcon } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { DisclosureRow, DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { asRecord, readToolArtifact, safeExternalUrl } from "./types";
import { ToolStatus } from "./tool-status";
import { ToolIcon } from "./tool-icons";
import { useToolDisclosure } from "./tool-disclosure";
import { childAgentStatusLabel, interruptionLabel, readChildAgentResultDisplay, readChildAgentWaitDisplay } from "./child-agent-display";
import { TOOL_DETAIL_ROW_CLASS, TOOL_DETAIL_TEXT_ROW_CLASS } from "../elements/tool-layout-tokens";

function toolTitle(toolName: string, verb?: string): string {
  return verb ?? toolName;
}

function formatFileSize(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "文件大小未知";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatToolStatus(status: string): string {
  switch (status) {
    case "pending": return "等待中";
    case "running": return "执行中";
    case "completed": return "已完成";
    case "failed": return "失败";
    case "cancelled": return "已取消";
    default: return "未知状态";
  }
}

function formatUrlSite(value: unknown): string {
  if (typeof value !== "string" || !value) return "网站未知";
  const safeUrl = safeExternalUrl(value);
  if (!safeUrl) return value;
  try {
    return new URL(safeUrl).hostname;
  } catch {
    return value;
  }
}

function readFileSummary(data: Record<string, unknown>): string {
  const path = typeof data.path === "string" ? data.path : "文件未知";
  const lineRange = asRecord(data.line_range);
  const range = data.file_size === 0
    ? "空文件"
    : typeof lineRange.start === "number" && lineRange.end === "END"
      ? `L${String(lineRange.start)}-END`
      : typeof lineRange.start === "number" && typeof lineRange.end === "number"
        ? `L${String(lineRange.start)}-L${String(lineRange.end)}`
        : lineRange.end === null
          ? "指定范围没有返回内容"
          : "读取范围由后端提供";
  return `${path} · ${range} · ${formatFileSize(data.file_size)}`;
}

function displaySummary(data: Record<string, unknown>, backendStatus: string): string {
  switch (data.kind) {
    case "read-file-meta":
      return readFileSummary(data);
    case "file-list": {
      const pattern = typeof data.pattern === "string" ? data.pattern : "搜索";
      const count = typeof data.match_count === "number"
        ? data.match_count
        : Array.isArray(data.files) ? data.files.length : 0;
      return `${pattern} · ${count} 个文件`;
    }
    case "content-search-results": {
      const pattern = typeof data.pattern === "string" ? data.pattern : "搜索";
      const count = typeof data.match_count === "number" ? data.match_count : 0;
      return `${pattern} · ${count} 个命中`;
    }
    case "directory-list": {
      const path = typeof data.path === "string" ? data.path : "目录未知";
      const count = typeof data.total_entries === "number"
        ? data.total_entries
        : Array.isArray(data.entries) ? data.entries.length : 0;
      return `${path} · ${count} 个条目`;
    }
    case "web-search-results": {
      const query = typeof data.query === "string" ? data.query : "搜索";
      const count = Array.isArray(data.results) ? data.results.length : 0;
      return `${query} · ${count} 个结果`;
    }
    case "web-extract-urls": {
      const urls = Array.isArray(data.urls) ? data.urls : [];
      const firstUrl = urls.length > 0 ? asRecord(urls[0]).url : undefined;
      const target = urls.length > 1 ? `${formatUrlSite(firstUrl)} 等 ${urls.length} 个网站` : formatUrlSite(firstUrl);
      const overallStatus = typeof data.status_hint === "string" ? data.status_hint : formatToolStatus(backendStatus);
      return `${target} · ${overallStatus}`;
    }
    case "child-agent-wait-result": {
      const messages = Array.isArray(data.messages) ? data.messages.length : 0;
      const pending = Array.isArray(data.pending) ? data.pending.length : 0;
      if (data.interrupted_by === "parent_cancelled" || data.interrupted_by === "session_closed" || data.interrupted_by === "shutdown") {
        return `${messages} 条结果 · ${interruptionLabel(data.interrupted_by)}`;
      }
      return data.timed_out === true ? `${messages} 条结果 · ${pending} 个等待超时` : `${messages} 条结果 · ${pending} 个等待中`;
    }
    case "child-agent-result": {
      const status = typeof data.status === "string" ? data.status : backendStatus;
      const agent = typeof data.agent_name === "string" ? data.agent_name : "子 Agent";
      return `${agent} · 子 Agent：${formatToolStatus(status)}`;
    }
    default:
      return typeof data.path === "string" ? data.path : typeof data.pattern === "string" ? data.pattern : "";
  }
}

function ListEntries({ data }: { data: Record<string, unknown> }) {
  const isWebSearch = data.kind === "web-search-results";
  const isWebExtract = data.kind === "web-extract-urls";
  const isContentSearch = data.kind === "content-search-results";
  const entries = !isWebSearch && !isWebExtract && Array.isArray(data.entries) ? data.entries : [];
  const files = !isWebSearch && !isWebExtract && Array.isArray(data.files) ? data.files : [];
  const matches = isContentSearch && Array.isArray(data.matches) ? data.matches : [];
  const results = isWebSearch && Array.isArray(data.results) ? data.results : [];
  const urls = Array.isArray(data.urls) ? data.urls : [];
  if (entries.length === 0 && files.length === 0 && matches.length === 0 && results.length === 0 && urls.length === 0) {
    const emptyMessage = data.kind === "directory-list"
      ? data.total_entries === 0 ? "目录为空" : "当前页没有条目"
      : "未找到匹配";
    return <p className="text-muted-foreground text-xs">{emptyMessage}</p>;
  }
  return (
    <div className="space-y-2">
      {typeof data.status_hint === "string" && <p className="text-muted-foreground text-xs">{data.status_hint}</p>}
      <ul className="text-xs">
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
          <li key={`${String(item.path ?? item.name ?? index)}-${index}`} className={TOOL_DETAIL_ROW_CLASS}>
            <Icon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
            {labelNode}
            {typeof type === "string" && <span className="text-muted-foreground ml-auto">{type}</span>}
          </li>
        );
      })}
      {files.map((entry, index) => {
        const item = asRecord(entry);
        const path = String(item.path ?? "未知文件");
        return <li key={`${path}-${index}`} className={TOOL_DETAIL_TEXT_ROW_CLASS}>{path}</li>;
      })}
      {matches.map((entry, index) => {
        const item = asRecord(entry);
        const path = String(item.path ?? "未知文件");
        const line = typeof item.line === "number" ? `:${item.line}` : "";
        const content = typeof item.content === "string" ? item.content : "";
        return <li key={`${path}-${line}-${index}`} className={TOOL_DETAIL_TEXT_ROW_CLASS}>{path}{line} {content}</li>;
      })}
      {results.map((entry, index) => {
        const item = asRecord(entry);
        const rawUrl = typeof item.url === "string" ? item.url : "";
        const url = safeExternalUrl(rawUrl);
        const title = typeof item.title === "string" && item.title ? item.title : rawUrl;
        if (!rawUrl) return null;
        return (
          <li key={`${url}-${index}`} className={TOOL_DETAIL_ROW_CLASS}>
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
          <li key={`${url}-${index}`} className={TOOL_DETAIL_ROW_CLASS}>
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

function ChildAgentWaitResult({ data }: { data: Record<string, unknown> }) {
  const result = readChildAgentWaitDisplay(data);
  if (!result) return <p className="text-muted-foreground text-xs">子 Agent 等待结果无效</p>;
  return (
    <div className="space-y-2 text-xs">
      {result.interruptedBy && <p className="text-amber-300">{interruptionLabel(result.interruptedBy)}</p>}
      {result.timedOut && <p className="text-amber-300">等待已超时，未完成的子 Agent 未被取消。</p>}
      {result.messages.map((message) => (
        <div key={`${message.childTaskId}:${message.childRunId}`} className="rounded border border-white/10 px-2.5 py-2">
          <div className="flex items-center gap-2 text-zinc-300">
            <span>task {message.childTaskId}</span>
            <span aria-hidden="true">·</span>
            <span>run {message.childRunId}</span>
            <span className="ml-auto">{childAgentStatusLabel(message.status)}</span>
          </div>
          {message.endReason && <p className="mt-1 text-amber-300">结束原因：{message.endReason}</p>}
          {message.status === "completed" && message.finalOutput && <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{message.finalOutput}</p>}
        </div>
      ))}
      {result.pending.map((item) => (
        <p key={item.childTaskId} className="text-muted-foreground">task {item.childTaskId} · {item.status ? childAgentStatusLabel(item.status) : "等待中"}</p>
      ))}
      {result.messages.length === 0 && result.pending.length === 0 && !result.interruptedBy && !result.timedOut && <p className="text-muted-foreground">没有新的子 Agent 结果</p>}
    </div>
  );
}

function ChildAgentResult({ data }: { data: Record<string, unknown> }) {
  const result = readChildAgentResultDisplay(data);
  if (!result) return <p className="text-muted-foreground text-xs">子 Agent 结果无效</p>;
  return (
    <div className="space-y-1 text-xs">
      <p className="text-muted-foreground">
        {result.agentName ?? "子 Agent"} · task {result.childTaskId} · run {result.childRunId} · {childAgentStatusLabel(result.status)}
      </p>
      {result.endReason && <p className="text-amber-300">{result.endReason}</p>}
      {result.finalOutput && <p className="whitespace-pre-wrap break-words text-muted-foreground">{result.finalOutput}</p>}
    </div>
  );
}

export function DetailsTool({ toolName, artifact: rawArtifact }: ToolCallMessagePartProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = artifact.display_data ?? {};
  const presentation = artifact.presentation;
  const title = toolTitle(toolName, presentation.verb);
  const summary = displaySummary(data, artifact.backendStatus);
  const hasDisplayData = typeof data.kind === "string";
  const expandable = presentation.expandable !== false && hasDisplayData;
  const defaultOpen = hasDisplayData && presentation.default_open === true;
  const isList = presentation.expand_layout === "list" || data.kind === "directory-list" || data.kind === "file-list" || data.kind === "content-search-results" || data.kind === "web-search-results" || data.kind === "web-extract-urls" || Array.isArray(data.entries);
  const isTerminalState = artifact.backendStatus === "failed" || artifact.backendStatus === "cancelled";

  const body = (
    <div className="space-y-2 px-3 pb-3 text-sm">
      {isTerminalState ? (
        <p className="text-destructive text-xs">{artifact.backendStatus === "cancelled" ? "已取消" : artifact.error ?? "执行失败"}</p>
      ) : isList ? <ListEntries data={data} /> : data.kind === "read-file-meta" ? (
        <p className="text-muted-foreground text-xs">
          {readFileSummary(data)}
          {data.truncated === true ? ` · ${String(data.status_hint ?? "文件过大，已截断")}` : ""}
        </p>
      ) : data.kind === "delegation-result" ? (
        <p className="text-muted-foreground text-xs">
          {typeof data.child_agent_id === "string" ? data.child_agent_id : "子 Agent"}
          {typeof data.title === "string" && data.title ? ` · ${data.title}` : ""}
          </p>
      ) : data.kind === "child-agent-wait-result" ? (
        <ChildAgentWaitResult data={data} />
      ) : data.kind === "child-agent-result" ? (
        <ChildAgentResult data={data} />
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
    trailing: <ToolStatus status={artifact.backendStatus} prefix={data.kind === "child-agent-result" || data.kind === "child-agent-wait-result" ? "工具" : undefined} />,
  };

  const [open, setOpen] = useToolDisclosure(artifact.backendStatus, defaultOpen, { openWhileRunning: false });

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
