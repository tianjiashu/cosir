import { fromMarkdown } from "mdast-util-from-markdown";
import { toMarkdown } from "mdast-util-to-markdown";
import type { Link, RootContent } from "mdast";

/** 引用只存在于前端文档和普通消息正文，不含持久化 ID 或文件版本。 */
export type WorkspaceReference = {
  kind: "file" | "directory" | "snippet" | "excerpt";
  path: string;
  startLine?: number;
  endLine?: number;
  content?: string;
  language?: string;
};
export type ReferencePart =
  | { type: "text"; text: string }
  | { type: "reference"; reference: WorkspaceReference };
const schemes = {
  file: "cosir-file",
  directory: "cosir-dir",
  snippet: "cosir-snippet",
  excerpt: "cosir-excerpt",
} as const;
export const referenceName = (ref: WorkspaceReference) =>
  `${ref.path.split("/").at(-1) || "工作区"}${ref.kind === "directory" ? "/" : ref.kind === "excerpt" ? " · 摘录" : ref.startLine ? ` · L${ref.startLine}–${ref.endLine}` : ""}`;
const normalize = (content: string) => content.replace(/\r\n?/g, "\n");

/** 标准 Markdown 序列化负责链接标签转义和选择足够长的代码围栏。 */
export function encodeReference(ref: WorkspaceReference): string {
  const path = (ref.path || ".")
    .split("/")
    .map((part) =>
      encodeURIComponent(part).replace(
        /[!'()*]/g,
        (char) => `%${char.charCodeAt(0).toString(16)}`,
      ),
    )
    .join("/");
  const hash = ref.startLine ? `#L${ref.startLine}-L${ref.endLine}` : "";
  const link: Link = {
    type: "link",
    url: `${schemes[ref.kind]}:${path}${hash}`,
    children: [{ type: "text", value: ref.path || "." }],
  };
  if (ref.kind === "file" || ref.kind === "directory")
    return toMarkdown(link).trimEnd();
  const value = toMarkdown(
    {
      type: "root",
      children: [
        { type: "paragraph", children: [link] },
        {
          type: "code",
          lang: ref.language || null,
          value: normalize(ref.content ?? ""),
        },
      ],
    },
    { fences: true },
  ).trimEnd();
  return `\n\n${value}\n\n`;
}
function decodeLink(link: Link): WorkspaceReference | null {
  const match =
    /^(cosir-file|cosir-dir|cosir-snippet|cosir-excerpt):([^#]*)(?:#L([1-9]\d*)-L([1-9]\d*))?$/.exec(
      link.url,
    );
  if (!match) return null;
  try {
    const path = decodeURIComponent(match[2]);
    if (
      !path ||
      path.startsWith("/") ||
      path.split("/").includes("..") ||
      [...path].some((char) => char.charCodeAt(0) < 32)
    )
      return null;
    const kind = (Object.keys(schemes) as Array<keyof typeof schemes>).find(
      (key) => schemes[key] === match[1],
    )!;
    const startLine = match[3] ? Number(match[3]) : undefined;
    const endLine = match[4] ? Number(match[4]) : undefined;
    if (
      startLine &&
      (!Number.isSafeInteger(endLine) ||
        endLine! < startLine ||
        kind !== "snippet")
    )
      return null;
    return {
      kind,
      path: path === "." ? "" : kind === "directory" ? path.replace(/\/+$/, "") : path,
      ...(startLine ? { startLine, endLine } : {}),
    };
  } catch {
    return null;
  }
}

/** 仅转换明确的引用链接；片段必须由独立来源段落及完整围栏组成，示例代码内部不递归解析。 */
export function decodeReferences(text: string): ReferencePart[] {
  if (!/cosir-(?:file|dir|snippet|excerpt):/.test(text))
    return [{ type: "text", text }];
  const nodes = fromMarkdown(text).children;
  const ranges: Array<{
    start: number;
    end: number;
    reference: WorkspaceReference;
  }> = [];
  const visit = (node: RootContent) => {
    if (node.type !== "paragraph") return;
    for (const child of node.children) {
      if (child.type !== "link") continue;
      const reference = decodeLink(child);
      if (
        reference &&
        (reference.kind === "file" || reference.kind === "directory")
      )
        ranges.push({
          start: child.position!.start.offset!,
          end: child.position!.end.offset!,
          reference,
        });
    }
  };
  for (let index = 0; index < nodes.length; index++) {
    const node = nodes[index];
    const next = nodes[index + 1];
    if (
      node.type === "paragraph" &&
      node.children.length === 1 &&
      node.children[0].type === "link" &&
      next?.type === "code"
    ) {
      const reference = decodeLink(node.children[0]);
      const source = text.slice(
        next.position!.start.offset,
        next.position!.end.offset,
      );
      const opener = /^ {0,3}(`{3,}|~{3,})[^\n]*\n/.exec(source);
      const closer = /\n {0,3}(`{3,}|~{3,})[ \t]*$/.exec(source);
      if (
        reference &&
        ["snippet", "excerpt"].includes(reference.kind) &&
        opener &&
        closer &&
        closer[1][0] === opener[1][0] &&
        closer[1].length >= opener[1].length
      ) {
        let start = node.position!.start.offset!;
        let end = next.position!.end.offset!;
        // 胶囊展开时产生的两侧分段空行随节点折叠，避免来回转换不断增生空行。
        const previousEnd = ranges.at(-1)?.end ?? 0;
        if (start - 2 >= previousEnd && text.slice(start - 2, start) === "\n\n")
          start -= 2;
        if (text.slice(end, end + 2) === "\n\n") end += 2;
        ranges.push({
          start,
          end,
          reference: {
            ...reference,
            content: next.value,
            ...(next.lang ? { language: next.lang } : {}),
          },
        });
        index++;
        continue;
      }
    }
    visit(node);
  }
  const parts: ReferencePart[] = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start > cursor)
      parts.push({ type: "text", text: text.slice(cursor, range.start) });
    parts.push({ type: "reference", reference: range.reference });
    cursor = range.end;
  }
  if (cursor < text.length)
    parts.push({ type: "text", text: text.slice(cursor) });
  return parts;
}
export const referenceIdentity = encodeReference;
