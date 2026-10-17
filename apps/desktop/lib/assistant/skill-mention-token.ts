import { mapMarkdownText } from "@/lib/assistant/markdown-literals";
const SKILL_MENTION_TOKEN = /\[\[cosir-skill:([^\]]+)\]\]/g;
const SKILL_IDENTIFIER = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** 把 Skill 名称编码为仅供 composer 编辑器使用的行内标记。 */
export function createSkillMentionToken(name: string): string {
  return `[[cosir-skill:${encodeURIComponent(name)}]]`;
}

/** 将 composer 内部 token 投影成可读且可从编辑态恢复的正文标记。 */
export function materializeSkillMentions(text: string): string {
  return mapMarkdownText(text, (text) =>
    text.replace(SKILL_MENTION_TOKEN, (_token, encodedName: string) => {
      const name = parseSkillMentionName(encodedName);
      return `【Skill: ${encodeURIComponent(name)}】`;
    }),
  );
}

/** 把已发送正文中的可逆 Skill 标记重新投影为编辑器胶囊 token。 */
export function restoreSkillMentionTokens(text: string): string {
  return mapMarkdownText(text, (text) =>
    text.replace(/【Skill: ([^】]+)】/g, (_mention, encodedName: string) => {
      const name = parseSkillMentionName(encodedName);
      return SKILL_IDENTIFIER.test(name)
        ? createSkillMentionToken(name)
        : _mention;
    }),
  );
}

/** 解析编辑器 token 中的 Skill 名称；无效编码按原始名称保留，避免丢失草稿文本。 */
export function parseSkillMentionName(encodedName: string): string {
  try {
    return decodeURIComponent(encodedName);
  } catch {
    return encodedName;
  }
}
