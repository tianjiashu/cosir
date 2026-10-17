import { getText } from "@/lib/api/workspace-files";
import type { WorkspaceReference } from "./codec";

export type ReferenceInput = {
  path: string;
  kind: WorkspaceReference["kind"];
  encoding?: string;
  start_line?: number;
  end_line?: number;
  content?: string;
};
const MAX_FRAGMENT_BYTES = 256 * 1024;
/** 只在用户明确添加跨分页行范围时读取文件；普通路径和已选文字完全来自前端，不检查来源变化。 */
export async function prepareReference(
  workspace: number,
  input: ReferenceInput,
  signal: AbortSignal,
): Promise<WorkspaceReference> {
  const { kind, path, start_line: startLine, end_line: endLine } = input;
  if (kind === "file" || kind === "directory") return { kind, path };
  let content = input.content;
  if (content === undefined) {
    if (!startLine || !endLine || endLine < startLine)
      throw new Error("请选择有效行范围");
    let offset = 0,
      line = 1,
      bytes = 0,
      lastReadLine = 0;
    let previousCR = false;
    const pieces: string[] = [];
    while (line <= endLine) {
      signal.throwIfAborted();
      const page = await getText(
        workspace,
        path,
        offset,
        input.encoding ?? "utf-8",
        "",
        false,
        undefined,
        signal,
      );
      let text = page.text;
      if (previousCR && text.startsWith("\n")) text = text.slice(1);
      previousCR = text.endsWith("\r");
      text = text.replace(/\r\n?/g, "\n");
      const rows = text.split("\n");
      for (let index = 0; index < rows.length && line <= endLine; index++) {
        const terminated = index < rows.length - 1;
        const value = rows[index] + (terminated ? "\n" : "");
        if (value) lastReadLine = line;
        if (line >= startLine) {
          bytes += new TextEncoder().encode(value).length;
          if (bytes > MAX_FRAGMENT_BYTES)
            throw new Error("片段超过 256 KiB，请缩小范围或添加文件引用");
          pieces.push(value);
        }
        if (terminated) line++;
      }
      if (page.next_offset === null) break;
      offset = page.next_offset;
    }
    if (lastReadLine < endLine) throw new Error("行号超出文件范围");
    content = pieces.join("");
  }
  if (!content || new TextEncoder().encode(content).length > MAX_FRAGMENT_BYTES)
    throw new Error("选区为空或超过 256 KiB，请缩小范围");
  return {
    kind,
    path,
    content: content.replace(/\r\n?/g, "\n").replace(/\n$/, ""),
    ...(startLine ? { startLine, endLine } : {}),
  };
}
