import { FileIcon, FolderIcon, LinkIcon } from "lucide-react";
import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { cn } from "@/lib/utils";
import { TOOL_DETAIL_ROW_CLASS, TOOL_DETAIL_TEXT_ROW_CLASS } from "../../elements/tool-layout-tokens";
import { asRecord } from "../types";
import { DetailEmpty, DetailHint, readArray, readString, type DetailView } from "./detail-view";
import { HighlightedMatch } from "./highlighted-match";

/** `file-list`：按 glob 找到的文件路径，可在文件面板打开。 */
export const fileListView: DetailView = {
  summary: (data) => {
    const count = typeof data.match_count === "number" ? data.match_count : readArray(data.files).length;
    return `${readString(data.pattern, "搜索")} · ${count} 个文件`;
  },
  body: (data) => {
    const files = readArray(data.files).map(asRecord);
    if (files.length === 0) return <DetailEmpty>未找到匹配</DetailEmpty>;
    return (
      <div className="space-y-2">
        <DetailHint data={data} />
        <ul className="text-xs">
          {files.map((item, index) => {
            const path = readString(item.path, "未知文件");
            return (
              <li key={`${path}-${index}`} className={cn(TOOL_DETAIL_TEXT_ROW_CLASS, "flex")}>
                <WorkspacePathLink path={path} />
              </li>
            );
          })}
        </ul>
      </div>
    );
  },
};

/**
 * `content-search-results`：命中行与上下文行（`is_match: false`），`match_count` 是总命中数，
 * 列表可能只是其中一页。路径可点开，命中片段按搜索模式高亮，上下文行降低对比度。
 */
export const contentSearchView: DetailView = {
  summary: (data) => {
    const count = typeof data.match_count === "number" ? data.match_count : 0;
    return `${readString(data.pattern, "搜索")} · ${count} 个命中`;
  },
  body: (data) => {
    const matches = readArray(data.matches).map(asRecord);
    if (matches.length === 0) return <DetailEmpty>未找到匹配</DetailEmpty>;
    const pattern = readString(data.pattern, "");
    return (
      <div className="space-y-2">
        <DetailHint data={data} />
        <ul className="text-xs">
          {matches.map((item, index) => {
            const path = readString(item.path, "未知文件");
            const line = typeof item.line === "number" ? `:${item.line}` : "";
            const isContext = item.is_match === false;
            return (
              <li
                key={`${path}${line}-${index}`}
                className={cn(TOOL_DETAIL_ROW_CLASS, isContext && "text-muted-foreground")}
              >
                <WorkspacePathLink path={path} className="max-w-[45%] shrink-0">{path}{line}</WorkspacePathLink>
                <span className="min-w-0 truncate font-mono">
                  {isContext ? readString(item.content, "") : <HighlightedMatch text={readString(item.content, "")} pattern={pattern} />}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    );
  },
};

/** `directory-list`：分页列出的目录条目（`path` 为绝对路径）；空目录与「当前页为空」文案不同。 */
export const directoryListView: DetailView = {
  summary: (data) => {
    const count = typeof data.total_entries === "number" ? data.total_entries : readArray(data.entries).length;
    return `${readString(data.path, "目录未知")} · ${count} 个条目`;
  },
  body: (data) => {
    const entries = readArray(data.entries).map(asRecord);
    if (entries.length === 0) return <DetailEmpty>{data.total_entries === 0 ? "目录为空" : "当前页没有条目"}</DetailEmpty>;
    return (
      <div className="space-y-2">
        <DetailHint data={data} />
        <ul className="text-xs">
          {entries.map((item, index) => {
            const type = item.type;
            const Icon = type === "dir" ? FolderIcon : type === "link" ? LinkIcon : FileIcon;
            const label = String(item.name ?? item.path ?? "未知条目");
            // 符号链接不跟随打开：目标可能在工作区外，类型也未知。
            const openable = typeof item.path === "string" && (type === "file" || type === "dir");
            return (
              <li key={`${String(item.path ?? item.name ?? index)}-${index}`} className={TOOL_DETAIL_ROW_CLASS}>
                <Icon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
                {openable
                  ? <WorkspacePathLink path={item.path as string} directory={type === "dir"}>{label}</WorkspacePathLink>
                  : <span className="truncate">{label}</span>}
                {typeof type === "string" && <span className="text-muted-foreground ml-auto">{type}</span>}
              </li>
            );
          })}
        </ul>
      </div>
    );
  },
};
