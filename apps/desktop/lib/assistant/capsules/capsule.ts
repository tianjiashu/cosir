/**
 * 输入框胶囊的文本语法（唯一事实源，前后端各有一份实现，用 `contract/capsule-grammar-cases.json` 对齐）。
 *
 * 胶囊是用户消息里的「指针」，模型直接读它去取正文，不把正文搬进上下文：
 *
 * - `<file path="/abs/notes.md" />`：本机文件附件（绝对路径；目录以 `/` 结尾）。
 * - `<ref path="src/a.py" lines="12-14" />`：工作区引用（相对路径；PDF 用 `page`）。
 * - `<skill name="review-code" path="/abs/…/SKILL.md" />`：用户为本条消息选择的 Skill。
 *
 * 属性值按 XML 实体转义；标签只在代码块/行内代码之外生效（见 `parse-capsules`）。
 */

export type FileCapsule = { kind: "file"; path: string };
export type RefCapsule = { kind: "ref"; path: string; lines?: string; page?: string };
export type SkillCapsule = { kind: "skill"; name: string; path?: string };
export type Capsule = FileCapsule | RefCapsule | SkillCapsule;

/** 解析后的一段消息：普通文本或一个胶囊。 */
export type CapsuleSegment =
  | { type: "text"; text: string }
  | { type: "capsule"; capsule: Capsule };

/** 一个胶囊标签的完整匹配：`<tag key="value" … />`。属性值内部不出现裸双引号。 */
export const CAPSULE_TAG_PATTERN =
  /<(file|ref|skill)\s+([a-z]+="[^"]*"(?:\s+[a-z]+="[^"]*")*)\s*\/>/g;

const ATTRIBUTE_PATTERN = /([a-z]+)="([^"]*)"/g;
const LINES_PATTERN = /^(\d+)(?:-(\d+))?$/;
const PAGE_PATTERN = /^[1-9]\d*$/;

const ALLOWED_ATTRIBUTES: Record<Capsule["kind"], { required: readonly string[]; optional: readonly string[] }> = {
  file: { required: ["path"], optional: [] },
  ref: { required: ["path"], optional: ["lines", "page"] },
  skill: { required: ["name"], optional: ["path"] },
};

/** 转义要放进 XML 属性的值；`&` 必须最先替换，避免把后续实体二次转义。 */
export function escapeAttribute(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 还原 `escapeAttribute` 的转义；`&amp;` 最后还原，避免把 `&amp;lt;` 解成 `<`。 */
export function unescapeAttribute(value: string): string {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
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
  if (tag === "ref") {
    const lines = attributes.get("lines");
    const page = attributes.get("page");
    if (lines !== undefined && page !== undefined) return null;
    if (lines !== undefined && !validLineRange(lines)) return null;
    if (page !== undefined && !PAGE_PATTERN.test(page)) return null;
    return {
      kind: "ref",
      path: raw("path"),
      ...(lines !== undefined ? { lines } : page !== undefined ? { page } : {}),
    };
  }
  const name = raw("name");
  if (!name) return null;
  const path = attributes.get("path");
  if (path !== undefined && !path) return null;
  return { kind: "skill", name, ...(path !== undefined ? { path } : {}) };
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
  if (capsule.kind === "ref") {
    const locator =
      capsule.lines !== undefined
        ? ` lines="${escapeAttribute(capsule.lines)}"`
        : capsule.page !== undefined
          ? ` page="${escapeAttribute(capsule.page)}"`
          : "";
    return `<ref path="${escapeAttribute(capsule.path)}"${locator} />`;
  }
  const path = capsule.path !== undefined ? ` path="${escapeAttribute(capsule.path)}"` : "";
  return `<skill name="${escapeAttribute(capsule.name)}"${path} />`;
}

/** 展示/标题用的纯文本：去掉标签，保留可读名称（不修改正文、不用于落库）。 */
export function capsulePlainText(capsule: Capsule): string {
  if (capsule.kind === "file") return basename(capsule.path);
  if (capsule.kind === "ref") {
    const locator =
      capsule.lines !== undefined ? `:${capsule.lines}` : capsule.page !== undefined ? ` (p.${capsule.page})` : "";
    return `${capsule.path || "工作区"}${locator}`;
  }
  return capsule.name;
}

/** 路径的最后一段；兼容 Windows 反斜杠与目录尾斜杠。 */
export function basename(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).at(-1) || trimmed || path;
}
