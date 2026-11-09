import { splitMarkdownTokens } from "@/lib/assistant/composer/markdown-literals";

import {
  CAPSULE_TAG_PATTERN,
  capsuleFromTag,
  capsulePlainText,
  serializeCapsule,
  type Capsule,
  type CapsuleSegment,
} from "./capsule";

/**
 * 把用户消息正文切成「普通文本 / 胶囊」片段。
 *
 * 复用 composer 的 markdown 分段：围栏代码块与行内代码里的同形标签保持字面量，不激活胶囊。
 * 语法不合法的标签按普通文本保留，用户普通输入不会被误判。
 */
export function parseCapsules(text: string): CapsuleSegment[] {
  return splitMarkdownTokens<Capsule>(text, CAPSULE_TAG_PATTERN, (match) =>
    capsuleFromTag(match[1] ?? "", match[2] ?? ""),
  ).map((part) =>
    part.type === "token"
      ? { type: "capsule", capsule: part.token }
      : { type: "text", text: part.text },
  );
}

/** 是否有可解析的胶囊（用于决定渲染路径，不扫描代码内的字面量）。 */
export function hasCapsules(text: string): boolean {
  return parseCapsules(text).some((segment) => segment.type === "capsule");
}

/** 展示/标题用的纯文本投影：胶囊换成可读名称，文本原样。 */
export function capsuleTextToPlainText(text: string): string {
  return parseCapsules(text)
    .map((segment) =>
      segment.type === "capsule" ? capsulePlainText(segment.capsule) : segment.text,
    )
    .join("");
}

/** 规范化正文里的标签（同一组胶囊只产出一套写法）；普通文本原样。 */
export function normalizeCapsuleText(text: string): string {
  return parseCapsules(text)
    .map((segment) =>
      segment.type === "capsule" ? serializeCapsule(segment.capsule) : segment.text,
    )
    .join("");
}
