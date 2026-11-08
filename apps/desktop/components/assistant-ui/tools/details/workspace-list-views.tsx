import { LinkIcon } from "lucide-react";
import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { cn } from "@/lib/utils";
import type { CallView } from "../call-view";
import { readArray, readRecords, readString } from "../display-utils";
import { DETAIL_ROW_CLASS, listDetail } from "./detail-view";
import { HighlightedMatch } from "./highlighted-match";

/** `file-list`：按 glob 找到的文件路径，可在文件面板打开。 */
export const fileListView: CallView = (data) => {
  const count = typeof data.match_count === "number" ? data.match_count : readArray(data.files).length;
  return {
    meta: `${count} 个文件`,
    detail: listDetail(data, () => readRecords(data.files), "未找到匹配", (item, index) => {
      const path = readString(item.path, "未知文件");
      return <li key={`${path}-${index}`} className={DETAIL_ROW_CLASS}><WorkspacePathLink path={path} /></li>;
    }),
  };
};

/**
 * `content-search-results`：命中行与上下文行（`is_match: false`），`match_count` 是总命中数，
 * 列表可能只是其中一页。路径可点开，命中片段按搜索模式高亮，上下文行降低对比度。
 */
export const contentSearchView: CallView = (data) => {
  const pattern = readString(data.pattern, "");
  return {
    meta: `${typeof data.match_count === "number" ? data.match_count : 0} 个命中`,
    detail: listDetail(data, () => readRecords(data.matches), "未找到匹配", (item, index) => {
      const path = readString(item.path, "未知文件");
      const line = typeof item.line === "number" ? `:${item.line}` : "";
      const isContext = item.is_match === false;
      return (
        <li key={`${path}${line}-${index}`} className={cn(DETAIL_ROW_CLASS, isContext && "text-muted-foreground")}>
          <WorkspacePathLink path={path} line={typeof item.line === "number" ? item.line : undefined} className="max-w-[45%] shrink-0">{path}{line}</WorkspacePathLink>
          <span className="min-w-0 truncate font-mono">
            {isContext ? readString(item.content, "") : <HighlightedMatch text={readString(item.content, "")} pattern={pattern} />}
          </span>
        </li>
      );
    }),
  };
};

/** `directory-list`：分页列出的目录条目（`path` 为绝对路径）；空目录与「当前页为空」文案不同。 */
export const directoryListView: CallView = (data) => {
  return {
    meta: `${typeof data.total_entries === "number" ? data.total_entries : readArray(data.entries).length} 个条目`,
    detail: listDetail(data, () => readRecords(data.entries), data.total_entries === 0 ? "目录为空" : "当前页没有条目", (item, index) => {
      const type = item.type;
      const label = String(item.name ?? item.path ?? "未知条目");
      // 符号链接不跟随打开：目标可能在工作区外，类型也未知。
      const openable = typeof item.path === "string" && (type === "file" || type === "dir");
      return (
        <li key={`${String(item.path ?? item.name ?? index)}-${index}`} className={DETAIL_ROW_CLASS}>
          {type === "link" && <LinkIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />}
          {openable
            ? <WorkspacePathLink path={item.path as string} directory={type === "dir"}>{label}</WorkspacePathLink>
            : <span className="truncate">{label}</span>}
        </li>
      );
    }),
  };
};
