import type { ConfigurationScope } from "@/lib/api/configuration-scope";
import type { SkillEntry, SkillGroup } from "@/lib/api/extensions";

export type SearchedSkillGroup = { group: SkillGroup; skills: SkillEntry[] };

/**
 * 按搜索词（名称与描述，不分大小写）过滤分组目录。没有搜索词时原样返回全部分组，含空分组：它们告诉
 * 用户 Skill 该放在哪；有搜索词时只保留命中的条目，没有命中的分组整组隐藏。
 *
 * 只有搜索、没有按状态筛选：启停看每行的开关，不合标准的原因写在行内，加载不了的目录列在分组底部，
 * 仅手动与含脚本是徽标，几十个 Skill 用搜索就够。
 */
export function searchSkillGroups(groups: SkillGroup[], query: string): SearchedSkillGroup[] {
  const needle = query.trim().toLocaleLowerCase();
  if (needle === "") return groups.map((group) => ({ group, skills: group.skills }));
  return groups.flatMap((group) => {
    const skills = group.skills.filter((entry) => `${entry.name}\n${entry.description}`.toLocaleLowerCase().includes(needle));
    return skills.length > 0 ? [{ group, skills }] : [];
  });
}

/**
 * 批量启停能作用到的条目：搜索结果里本作用域这一层生效的条目（继承来的只读，被覆盖的改不到它自己），
 * 按当前状态分成「可停用」与「可启用」两组名称。
 */
export function bulkToggleTargets(searched: SearchedSkillGroup[], pageScope: ConfigurationScope): { enabled: string[]; disabled: string[] } {
  const targets = searched
    .flatMap(({ skills }) => skills)
    .filter((entry) => entry.standing === "effective" && entry.scope === pageScope);
  return {
    enabled: targets.filter((entry) => entry.enabled).map((entry) => entry.name),
    disabled: targets.filter((entry) => !entry.enabled).map((entry) => entry.name),
  };
}
