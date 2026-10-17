import {
  markdownSegments,
  mapMarkdownText,
} from "@/lib/assistant/markdown-literals";
import {
  decodeReferences,
  encodeReference,
  type WorkspaceReference,
} from "@/lib/workspace-references/codec";
import type { JSONContent } from "@tiptap/core";
import { inlineAttachmentKey } from "@/lib/assistant/attachments/local-file-token";
import {
  createSkillMentionToken,
  parseSkillMentionName,
} from "@/lib/assistant/skill-mention-token";

export const FILE_ATTACHMENT_TOKEN_PREFIX = "[[cosir-file:";
export const FILE_ATTACHMENT_TOKEN_SUFFIX = "]]";

const COMPOSER_TOKEN = /\[\[cosir-(file|image|skill):([^\]]+)\]\]/g;
const HIDDEN_ATTACHMENT_TOKEN =
  /<!--\s*(\[\[cosir-(?:file|image):[^\]]+\]\])\s*-->/g;

export type InlineComposerAttachment = {
  id: string;
  name: string;
  kind?: "file" | "image";
  tokenId?: string;
};

export type InlineComposerSkill = {
  name: string;
};

export type ComposerInsertionCommand =
  | { type: "insert-text"; text: string }
  | {
      type: "insert-attachments";
      attachments: readonly InlineComposerAttachment[];
    }
  | { type: "insert-references"; references: readonly WorkspaceReference[] }
  | { type: "insert-skill"; skill: InlineComposerSkill };

export type ComposerAttachmentReconciliation = {
  attachmentKeys: string[];
  tokenKeys: string[];
  unmatchedTokenKeys: string[];
  tokenlessAttachmentKeys: string[];
};

/**
 * 将持久化/受控文本转换为编辑器文档。附件、Skill token 在编辑期间是原子节点，
 * 普通文本里的相似字符串只在外部值重新装载时解析，避免每次击键都重建选区。
 */
export function parseComposerDocument(value: string): JSONContent {
  const text = mapMarkdownText(value, (part) =>
    part.replace(HIDDEN_ATTACHMENT_TOKEN, "$1"),
  );
  const paragraphs: JSONContent[][] = [[]];
  const appendText = (part: string) => {
    const lines = part.split("\n");
    lines.forEach((line, index) => {
      if (line)
        paragraphs[paragraphs.length - 1]?.push({ type: "text", text: line });
      if (index < lines.length - 1) paragraphs.push([]);
    });
  };

  for (const part of decodeReferences(text)) {
    if (part.type === "reference") {
      paragraphs[paragraphs.length - 1]?.push({
        type: "workspaceReference",
        attrs: { reference: part.reference },
      });
      continue;
    }
    for (const segment of markdownSegments(part.text)) {
      if (segment.literal) {
        appendText(segment.text);
        continue;
      }
      const plain = segment.text;
      let cursor = 0;
      for (const match of plain.matchAll(COMPOSER_TOKEN)) {
        const token = match[0];
        const offset = match.index ?? cursor;
        appendText(plain.slice(cursor, offset));
        cursor = offset + token.length;
        const kind = match[1];
        const tokenId = match[2] ?? "";
        if (kind === "skill") {
          paragraphs[paragraphs.length - 1]?.push({
            type: "skillToken",
            attrs: { name: parseSkillMentionName(tokenId) },
          });
        } else {
          paragraphs[paragraphs.length - 1]?.push({
            type: "attachmentToken",
            attrs: { kind, tokenId },
          });
        }
      }
      appendText(plain.slice(cursor));
    }
  }

  return {
    type: "doc",
    content: paragraphs.map((content) => ({
      type: "paragraph",
      ...(content.length ? { content } : {}),
    })),
  };
}

/** 把编辑器文档投影回现有的纯文本 token 契约，作为父组件与发送边界的值。 */
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
  if (command.type === "insert-text") {
    return command.text
      .split("\n")
      .flatMap((line, index, lines) => [
        ...(line ? [{ type: "text", text: line } satisfies JSONContent] : []),
        ...(index < lines.length - 1
          ? [{ type: "hardBreak" } satisfies JSONContent]
          : []),
      ]);
  }
  if (command.type === "insert-skill") {
    return [{ type: "skillToken", attrs: { name: command.skill.name } }];
  }
  if (command.type === "insert-references")
    return command.references.map((reference) => ({
      type: "workspaceReference",
      attrs: { reference },
    }));
  return command.attachments.map((attachment) => ({
    type: "attachmentToken",
    attrs: {
      kind: attachment.kind ?? "file",
      tokenId: attachment.tokenId ?? attachment.id,
    },
  }));
}

/** 比较文本中的附件 token 和当前附件集合，供 UI 呈现状态并记录诊断。 */
export function reconcileComposerAttachments(
  value: string,
  attachments: readonly InlineComposerAttachment[],
): ComposerAttachmentReconciliation {
  const attachmentKeys = attachments.map((attachment) =>
    inlineAttachmentKey(
      attachment.kind ?? "file",
      attachment.tokenId ?? attachment.id,
    ),
  );
  const tokenKeys = extractAttachmentTokenKeys(value);
  const attachmentKeySet = new Set(attachmentKeys);
  const tokenKeySet = new Set(tokenKeys);
  return {
    attachmentKeys,
    tokenKeys,
    unmatchedTokenKeys: tokenKeys.filter((key) => !attachmentKeySet.has(key)),
    tokenlessAttachmentKeys: attachmentKeys.filter(
      (key) => !tokenKeySet.has(key),
    ),
  };
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

function extractAttachmentTokenKeys(value: string): string[] {
  const text = mapMarkdownText(value, (part) =>
    part.replace(HIDDEN_ATTACHMENT_TOKEN, "$1"),
  );
  return markdownSegments(text)
    .filter((part) => !part.literal)
    .flatMap((part) => [...part.text.matchAll(COMPOSER_TOKEN)])
    .filter((match) => match[1] === "file" || match[1] === "image")
    .map((match) =>
      inlineAttachmentKey(
        match[1] === "image" ? "image" : "file",
        match[2] ?? "",
      ),
    );
}

function serializeInlineNode(node: JSONContent): string {
  if (node.type === "text") return node.text ?? "";
  if (node.type === "hardBreak") return "\n";
  if (node.type === "attachmentToken") {
    const kind = node.attrs?.kind === "image" ? "image" : "file";
    return `[[cosir-${kind}:${String(node.attrs?.tokenId ?? "")}]]`;
  }
  if (node.type === "workspaceReference")
    return encodeReference(node.attrs?.reference as WorkspaceReference);
  if (node.type === "skillToken") {
    return createSkillMentionToken(String(node.attrs?.name ?? ""));
  }
  return (node.content ?? []).map(serializeInlineNode).join("");
}
