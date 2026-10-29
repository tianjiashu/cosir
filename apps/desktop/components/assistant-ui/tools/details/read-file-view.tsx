import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { asRecord } from "../types";
import type { DetailView } from "./detail-view";

function formatFileSize(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "文件大小未知";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function lineRangeLabel(data: Record<string, unknown>): string {
  if (data.file_size === 0) return "空文件";
  const lineRange = asRecord(data.line_range);
  if (typeof lineRange.start === "number" && lineRange.end === "END") return `L${String(lineRange.start)}-END`;
  if (typeof lineRange.start === "number" && typeof lineRange.end === "number") {
    return `L${String(lineRange.start)}-L${String(lineRange.end)}`;
  }
  // `end: null` 表示指定范围没有返回内容，不能拼成 `Lnull`。
  return lineRange.end === null ? "指定范围没有返回内容" : "读取范围由后端提供";
}

/**
 * `read-file-meta`：读取的是元信息而非正文（正文只进模型上下文），一行摘要就是全部，
 * 截断提示也并入摘要，不提供展开内容——因此行是静态的，路径可以直接点开。
 */
export const readFileView: DetailView = {
  summary: (data) => {
    const parts = [lineRangeLabel(data), formatFileSize(data.file_size)];
    if (data.truncated === true) parts.push(typeof data.status_hint === "string" ? data.status_hint : "文件过大，已截断");
    const rest = ` · ${parts.join(" · ")}`;
    return typeof data.path === "string"
      ? <><WorkspacePathLink path={data.path} />{rest}</>
      : `文件未知${rest}`;
  },
};
