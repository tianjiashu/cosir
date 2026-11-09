import { splitMarkdownTokens, type MarkdownTokenPart } from "@/lib/assistant/composer/markdown-literals";

/**
 * 输入框里选中的 Skill：`[[cosir-skill:<URL 编码的名称>]]`。编辑器、发送正文与后端快照用的是同一个
 * token——后端按它校验 Skill 并把正文固化进模型输入，展示时 token 留在文本里，由这里切出胶囊。
 * 手打的其他写法（如 `【Skill: x】`）只是普通文字。
 */
const SKILL_MENTION_TOKEN = /\[\[cosir-skill:([^\]]+)\]\]/g;
const SKILL_IDENTIFIER = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export function createSkillMentionToken(name: string): string {
  return `[[cosir-skill:${encodeURIComponent(name)}]]`;
}

/**
 * 把已发送正文切成文本与 Skill 名称，供消息气泡按胶囊渲染；代码片段里的 token 与不合法的名称按原文保留。
 */
export function splitSkillMentions(text: string): MarkdownTokenPart<string>[] {
  return splitMarkdownTokens(text, SKILL_MENTION_TOKEN, (match) => {
    const name = parseSkillMentionName(match[1] ?? "");
    return SKILL_IDENTIFIER.test(name) ? name : null;
  });
}

/** 纯文本场景（侧栏标题等）里把 Skill token 换成 Skill 名称。 */
export function replaceSkillMentionsWithNames(text: string): string {
  return text.replace(SKILL_MENTION_TOKEN, (_token, encodedName: string) => parseSkillMentionName(encodedName));
}

/** 解析 token 中的 Skill 名称；无效编码按原始名称保留，避免丢失草稿文本。 */
export function parseSkillMentionName(encodedName: string): string {
  try {
    return decodeURIComponent(encodedName);
  } catch {
    return encodedName;
  }
}
