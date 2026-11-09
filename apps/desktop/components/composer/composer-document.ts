import {
  parseCapsules,
  serializeCapsule,
  type Capsule,
  type CapsuleSegment,
} from "@/lib/assistant/capsules";
import type { JSONContent } from "@tiptap/core";

/** 编辑器里唯一的外部插入命令；胶囊统一经 `insert-capsules` 由调用方给出语义对象。 */
export type ComposerInsertionCommand =
  | { type: "insert-text"; text: string }
  | { type: "insert-capsules"; capsules: readonly Capsule[] };

/**
 * 将持久化/受控文本转换为编辑器文档：胶囊标签在编辑期间是原子节点，代码块/行内代码里的同形文本
 * 保持原文。普通文本里的相似字符串只在外部值重新装载时解析，避免每次击键都重建选区。
 */
export function parseComposerDocument(text: string): JSONContent {
  const paragraphs: JSONContent[][] = [[]];
  const appendText = (part: string) => {
    const lines = part.split("\n");
    lines.forEach((line, index) => {
      if (line)
        paragraphs[paragraphs.length - 1]?.push({ type: "text", text: line });
      if (index < lines.length - 1) paragraphs.push([]);
    });
  };
  const appendSegment = (segment: CapsuleSegment) => {
    if (segment.type === "text") appendText(segment.text);
    else paragraphs[paragraphs.length - 1]?.push(capsuleNode(segment.capsule));
  };

  for (const segment of parseCapsules(text)) appendSegment(segment);

  return {
    type: "doc",
    content: paragraphs.map((content) => ({
      type: "paragraph",
      ...(content.length ? { content } : {}),
    })),
  };
}

/** 把编辑器文档投影回正文（胶囊序列化为规范标签），作为父组件与发送边界的值。 */
export function serializeComposerDocument(document: JSONContent): string {
  if (document.type !== "doc") return serializeInlineNode(document);
  const nodes = document.content ?? [];
  // 剪贴板 Slice 可以只包含行内节点，不能按完整文档过滤掉单独复制的胶囊。
  if (nodes.every((node) => node.type !== "paragraph"))
    return nodes.map(serializeInlineNode).join("");
  return nodes
    .map((node) =>
      node.type === "paragraph"
        ? (node.content ?? []).map(serializeInlineNode).join("")
        : serializeInlineNode(node),
    )
    .join("\n");
}

/** 将外部插入命令转换为 ProseMirror 可插入的行内节点。 */
export function insertionCommandContent(
  command: ComposerInsertionCommand,
): JSONContent[] {
  if (command.type === "insert-text") return textContent(command.text);
  return command.capsules.map(capsuleNode);
}

/** 粘贴带胶囊的文本时使用的行内内容：标签解析为节点，其余文本原样（换行变 hardBreak）。 */
export function inlineComposerContent(text: string): JSONContent[] {
  return parseCapsules(text).flatMap((segment) =>
    segment.type === "capsule" ? [capsuleNode(segment.capsule)] : textContent(segment.text),
  );
}

/** 旧受控值尚未跟上内部编辑时，让编辑器文档暂时保留最新输入。 */
export function shouldPreservePendingEditorValue(
  propValue: string,
  lastPropValue: string,
  pendingInternalValue: string | null,
): boolean {
  return (
    pendingInternalValue !== null &&
    propValue === lastPropValue &&
    propValue !== pendingInternalValue
  );
}

function capsuleNode(capsule: Capsule): JSONContent {
  return { type: "capsuleToken", attrs: { capsule } };
}

function textContent(text: string): JSONContent[] {
  return text
    .split("\n")
    .flatMap((line, index, lines) => [
      ...(line ? [{ type: "text", text: line } satisfies JSONContent] : []),
      ...(index < lines.length - 1
        ? [{ type: "hardBreak" } satisfies JSONContent]
        : []),
    ]);
}

function serializeInlineNode(node: JSONContent): string {
  if (node.type === "text") return node.text ?? "";
  if (node.type === "hardBreak") return "\n";
  if (node.type === "capsuleToken") {
    const capsule = node.attrs?.capsule as Capsule | null | undefined;
    return capsule ? serializeCapsule(capsule) : "";
  }
  return (node.content ?? []).map(serializeInlineNode).join("");
}
