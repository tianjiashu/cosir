import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmTable } from "micromark-extension-gfm-table";
import { gfmTableFromMarkdown } from "mdast-util-gfm-table";
import type { RootContent } from "mdast";

/** A short original-text excerpt, never an AI summary; destinations and block-code noise stay in the full message. */
export function messagePreview(markdown: string): string {
  const text = (node: RootContent): string => {
    if (node.type === "table" || node.type === "code" || node.type === "image" || node.type === "imageReference" || node.type === "definition") return "";
    if (node.type === "text" || node.type === "inlineCode") return node.value;
    return "children" in node ? node.children.map((child) => text(child as RootContent)).join("") : "";
  };
  // Parse only a bounded excerpt; the full Markdown renderer remains responsible for the original body.
  return fromMarkdown(markdown.slice(0, 4000), { extensions: [gfmTable()], mdastExtensions: [gfmTableFromMarkdown()] }).children.map(text).filter(Boolean).join(" ").replace(/\s+/g, " ").trim().slice(0, 180);
}
