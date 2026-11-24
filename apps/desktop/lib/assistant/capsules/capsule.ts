import { baseName } from "@/lib/files/file-name";

/**
 * 输入框胶囊的文本语法（唯一事实源，前后端各有一份实现，用 `contract/capsule-grammar-cases.json` 对齐）。
 *
 * 前三种胶囊是用户消息里的「指针」，模型直接读它去取正文，不把正文搬进上下文；摘录自带原文：
 *
 * - `<file path="/abs/notes.md" />`：本机文件附件（绝对路径；目录以 `/` 结尾）。
 * - `<ref path="src/a.py" lines="12-14" />`：工作区引用（相对路径；PDF 用 `page`）。
 * - `<skill name="review-code" path="/abs/…/SKILL.md" />`：用户为本条消息选择的 Skill。
 * - `<quote path="src/a.py" lines="12-14" text="…" note="…" />`：用户选中的一段原文与对它的批注（对话回复、
 *   工作区文件的选区都产出它，见 `components/text-selection`）。原文随胶囊走——回复读不回来（可能被撤回、来自
 *   别的会话），文件之后可能变；来自工作区文件时另带位置（规则同 `ref`），模型可按位置读上下文。批注与原文是
 *   一个整体，同在一个胶囊里（输入框与气泡把它们画在一起，见 `quote-cards`）。后端展开成 `<quote>` 块给模型看。
 *
 * 属性值按实体转义（见 `ATTRIBUTE_ESCAPES`）；标签只在代码块/行内代码之外生效（见 `parse-capsules`）。
 */

export type FileCapsule = { kind: "file"; path: string };
/** 工作区里的位置：行范围或 PDF 页码至多一个（`ref` 与来自文件的 `quote` 共用）。 */
export type WorkspaceLocator = { lines?: string; page?: string };
export type RefCapsule = { kind: "ref"; path: string } & WorkspaceLocator;
export type SkillCapsule = { kind: "skill"; name: string; path?: string };
export type QuoteCapsule = { kind: "quote"; text: string; note?: string; path?: string } & WorkspaceLocator;
export type Capsule = FileCapsule | RefCapsule | SkillCapsule | QuoteCapsule;

/** 摘录原文的长度上限（与后端 `QuoteCapsule.MAX_TEXT_LENGTH` 同值，跨语言用例 `limits` 守着）。 */
export const QUOTE_TEXT_MAX = 8000;

/** 解析后的一段消息：普通文本或一个胶囊。 */
export type CapsuleSegment =
  | { type: "text"; text: string }
  | { type: "capsule"; capsule: Capsule };

/** 一个胶囊标签的完整匹配：`<tag key="value" … />`。属性值内部不出现裸双引号。 */
export const CAPSULE_TAG_PATTERN =
  /<(file|ref|skill|quote)\s+([a-z]+="[^"]*"(?:\s+[a-z]+="[^"]*")*)\s*\/>/g;

const ATTRIBUTE_PATTERN = /([a-z]+)="([^"]*)"/g;
const LINES_PATTERN = /^(\d+)(?:-(\d+))?$/;
const PAGE_PATTERN = /^[1-9]\d*$/;

const ALLOWED_ATTRIBUTES: Record<Capsule["kind"], { required: readonly string[]; optional: readonly string[] }> = {
  file: { required: ["path"], optional: [] },
  ref: { required: ["path"], optional: ["lines", "page"] },
  skill: { required: ["name"], optional: ["path"] },
  quote: { required: ["text"], optional: ["path", "lines", "page", "note"] },
};

/**
 * 属性值的转义表（与后端 `_ATTRIBUTE_ESCAPES` 同序）。除 XML 的四个实体外，换行、制表、反引号与 `~` 也转成
 * 数字实体：胶囊外的 Markdown 预扫描（`splitMarkdownTokens`）看不懂标签，属性值里的这些字符会和标签外的文本
 * 配对成围栏、缩进代码或行内代码，把标签切断。转义后任何属性值都是单行、不含代码标记，摘录这样携带整段
 * Markdown 的胶囊才能安全放进正文。`&` 必须最先转义、最后还原。
 */
const ATTRIBUTE_ESCAPES: ReadonlyArray<readonly [string, string]> = [
  ["&", "&amp;"],
  ["<", "&lt;"],
  [">", "&gt;"],
  ['"', "&quot;"],
  ["\n", "&#10;"],
  ["\r", "&#13;"],
  ["\t", "&#9;"],
  ["`", "&#96;"],
  ["~", "&#126;"],
];

/** 按 `ATTRIBUTE_ESCAPES` 转义要放进属性的值。 */
export function escapeAttribute(value: string): string {
  return ATTRIBUTE_ESCAPES.reduce((text, [raw, entity]) => text.replaceAll(raw, entity), value);
}

/** 还原 `escapeAttribute`：逆序替换，`&amp;` 最后还原，避免把 `&amp;lt;` 解成 `<`。 */
export function unescapeAttribute(value: string): string {
  return ATTRIBUTE_ESCAPES.reduceRight((text, [raw, entity]) => text.replaceAll(entity, raw), value);
}

/** 把标签匹配解析成胶囊；语法不合法时返回 null，原文按普通文本保留。 */
export function capsuleFromTag(tag: string, attributesText: string): Capsule | null {
  const spec = ALLOWED_ATTRIBUTES[tag as Capsule["kind"]];
  if (!spec) return null;
  const attributes = new Map<string, string>();
  for (const match of attributesText.matchAll(ATTRIBUTE_PATTERN)) {
    const key = match[1];
    if (!spec.required.includes(key) && !spec.optional.includes(key)) return null;
    if (attributes.has(key)) return null;
    attributes.set(key, unescapeAttribute(match[2]));
  }
  for (const key of spec.required) if (!attributes.has(key)) return null;
  const raw = (key: string) => attributes.get(key) ?? "";

  if (tag === "file") {
    const path = raw("path");
    return path ? { kind: "file", path } : null;
  }
  const locator = locatorFrom(attributes);
  if (locator === null) return null;
  if (tag === "ref") return { kind: "ref", path: raw("path"), ...locator };
  if (tag === "quote") {
    const text = raw("text");
    const path = attributes.get("path");
    // 位置只对文件有意义：有行号或页码就得有路径；来自对话回复的摘录没有路径。
    if (!text.trim() || path === "" || (path === undefined && Object.keys(locator).length)) return null;
    const note = attributes.get("note");
    return { kind: "quote", ...(path !== undefined ? { path, ...locator } : {}), text, ...(note ? { note } : {}) };
  }
  const name = raw("name");
  if (!name) return null;
  const path = attributes.get("path");
  if (path !== undefined && !path) return null;
  return { kind: "skill", name, ...(path !== undefined ? { path } : {}) };
}

/** 行范围或页码（至多一个、格式合法）；不合法时为 null。 */
function locatorFrom(attributes: ReadonlyMap<string, string>): WorkspaceLocator | null {
  const lines = attributes.get("lines");
  const page = attributes.get("page");
  if (lines !== undefined && page !== undefined) return null;
  if (lines !== undefined && !validLineRange(lines)) return null;
  if (page !== undefined && !PAGE_PATTERN.test(page)) return null;
  return lines !== undefined ? { lines } : page !== undefined ? { page } : {};
}

function locatorAttribute(locator: WorkspaceLocator): string {
  return locator.lines !== undefined
    ? ` lines="${escapeAttribute(locator.lines)}"`
    : locator.page !== undefined
      ? ` page="${escapeAttribute(locator.page)}"`
      : "";
}

function validLineRange(lines: string): boolean {
  const match = LINES_PATTERN.exec(lines);
  if (!match) return false;
  return match[2] === undefined || Number(match[1]) <= Number(match[2]);
}

/** 序列化为规范形态（固定属性顺序、单空格、自闭合）；所有写入口只经这里产出标签。 */
export function serializeCapsule(capsule: Capsule): string {
  if (capsule.kind === "file")
    return `<file path="${escapeAttribute(capsule.path)}" />`;
  if (capsule.kind === "ref")
    return `<ref path="${escapeAttribute(capsule.path)}"${locatorAttribute(capsule)} />`;
  if (capsule.kind === "quote") {
    const source = capsule.path !== undefined ? ` path="${escapeAttribute(capsule.path)}"${locatorAttribute(capsule)}` : "";
    const note = capsule.note ? ` note="${escapeAttribute(capsule.note)}"` : "";
    return `<quote${source} text="${escapeAttribute(capsule.text)}"${note} />`;
  }
  const path = capsule.path !== undefined ? ` path="${escapeAttribute(capsule.path)}"` : "";
  return `<skill name="${escapeAttribute(capsule.name)}"${path} />`;
}

/** 展示/标题用的纯文本：去掉标签，保留可读名称（不修改正文、不用于落库）。 */
export function capsulePlainText(capsule: Capsule): string {
  if (capsule.kind === "file") return baseName(capsule.path);
  if (capsule.kind === "ref") {
    const locator =
      capsule.lines !== undefined ? `:${capsule.lines}` : capsule.page !== undefined ? ` (p.${capsule.page})` : "";
    return `${capsule.path || "工作区"}${locator}`;
  }
  if (capsule.kind === "quote") return `「${quoteHeadline(capsule.text)}」`;
  return capsule.name;
}

/** 路径是否指向目录：空路径（工作区根）或带尾分隔符。 */
export function isDirectoryPath(path: string): boolean {
  return path === "" || /[\\/]$/.test(path);
}


/**
 * 摘录原文的一行预览（摘录条、纯文本投影用）：首个有字的行，去掉块级与行内的 Markdown 标记（标题、引用、列表、
 * 表格竖线、强调与代码记号），跳过围栏与表格分隔行。不截断，长度由展示处的省略样式处理。
 */
export function quoteHeadline(text: string): string {
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || /^(```|~~~)/.test(line) || /^\|?[\s:|-]+\|?$/.test(line)) continue;
    const plain = line
      .replace(/^(#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+)/, "")
      .replace(/^\||\|$/g, "")
      .split(/\s*\|\s*/)
      .join(" ")
      .replace(/\*\*|__|`/g, "")
      .trim();
    if (plain) return plain;
  }
  return "";
}
