import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import { formatByteSize } from "@/lib/format/byte-size";
import { asRecord } from "../display-utils";
import type { CallView } from "../call-view";

function formatFileSize(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "文件大小未知";
  return formatByteSize(value);
}

/** 读完整个文件是常态，不写范围；只有部分读取才值得说明。 */
function lineRangeLabel(data: Record<string, unknown>): string | null {
  if (data.file_size === 0) return "空文件";
  const lineRange = asRecord(data.line_range);
  if (lineRange.start === 1 && lineRange.end === "END") return null;
  if (typeof lineRange.start === "number" && lineRange.end === "END") return `L${String(lineRange.start)}-END`;
  if (typeof lineRange.start === "number" && typeof lineRange.end === "number") {
    return `L${String(lineRange.start)}-L${String(lineRange.end)}`;
  }
  // `end: null` 表示指定范围没有返回内容，不能拼成 `Lnull`。
  return lineRange.end === null ? "指定范围没有返回内容" : "读取范围由后端提供";
}

/** 打开文件时定位到本次读取的起始行；从第 1 行读起时不定位，与直接打开一致。 */
function readStartLine(data: Record<string, unknown>): number | undefined {
  const start = asRecord(data.line_range).start;
  return typeof start === "number" && start > 1 ? start : undefined;
}

/**
 * `read-file-meta`：读取的是元信息而非正文（正文只进模型上下文），一行摘要就是全部，
 * 截断提示也并入摘要，不提供展开内容——因此行是静态的，路径可以直接点开。
 */
export const readFileView: CallView = (data) => {
  const parts = [lineRangeLabel(data), formatFileSize(data.file_size)].filter((part) => part !== null);
  if (data.truncated === true) parts.push(typeof data.status_hint === "string" ? data.status_hint : "文件过大，已截断");
  return {
    target: typeof data.path === "string" ? <WorkspacePathLink path={data.path} line={readStartLine(data)} plain /> : undefined,
    meta: parts.join(" · "),
  };
};
