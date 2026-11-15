import type { SkillCatalogView } from "@/lib/api/extensions";

/**
 * 两次扫描之间多出与少了几个 Skill，给「重新扫描」的反馈用：点了没变化时用户要知道是真的没变，
 * 而不是没扫。按「作用域 + 身份 + 名称」计，不同根目录里的同名 Skill 各算一条。
 */
export function diffSkillCatalogs(before: SkillCatalogView, after: SkillCatalogView): { added: number; removed: number } {
  const keys = (view: SkillCatalogView) => new Set(view.groups.flatMap((group) => group.skills.map((skill) => `${group.scope}:${group.kind}:${skill.name}`)));
  const previous = keys(before);
  const next = keys(after);
  return {
    added: [...next].filter((key) => !previous.has(key)).length,
    removed: [...previous].filter((key) => !next.has(key)).length,
  };
}
