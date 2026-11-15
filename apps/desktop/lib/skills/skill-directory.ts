import type { ConfigurationScope } from "@/lib/api/configuration-scope";
import type { SkillGroup, SkillRootKind } from "@/lib/api/extensions";

/**
 * Skill 目录两种身份的称呼与「谁能看到」的说明：卡片标题、被覆盖提示与详情页页头共用这一份文案。
 * 身份只对用户有意义（还有谁能看到、导入落在哪），对 Agent 两者平等。
 */
export const SKILL_ROOT_COPY: Record<SkillRootKind, { name: string; audience: string }> = {
  cosir: { name: "cosir 专属", audience: "只有 cosir 使用" },
  shared: { name: "共享", audience: "Codex、Cursor 等工具也读取；cosir 只启停，不改文件" },
};

/** 带作用域的称呼（本工作区 / 系统级 · 身份）：详情页页头与导入冲突弹窗不分页面一律带作用域。 */
export function scopedRootLabel(scope: ConfigurationScope, kind: SkillRootKind): string {
  return `${scope === "workspace" ? "本工作区" : "系统级"} · ${SKILL_ROOT_COPY[kind].name}`;
}

/** 一个根目录在当前页面上的称呼：工作区页要带上作用域，系统页只有身份。 */
export function skillRootLabel(scope: ConfigurationScope, kind: SkillRootKind, pageScope: ConfigurationScope): string {
  return pageScope === "system" ? SKILL_ROOT_COPY[kind].name : scopedRootLabel(scope, kind);
}

/**
 * 每个名称生效的那条所在目录的称呼，给被覆盖的行说明「被谁覆盖」。只在后端算好的分组里查找
 * `standing === "effective"` 的条目，不推断合并规则；要传完整目录，而不是搜索后的结果。
 */
export function effectiveRootLabels(groups: SkillGroup[], pageScope: ConfigurationScope): Map<string, string> {
  const labels = new Map<string, string>();
  for (const group of groups) {
    for (const skill of group.skills) {
      if (skill.standing === "effective") labels.set(skill.name, skillRootLabel(group.scope, group.kind, pageScope));
    }
  }
  return labels;
}
