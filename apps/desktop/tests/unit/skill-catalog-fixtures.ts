import type { SkillEntry, SkillGroup } from "@/lib/api/extensions";

/** Skill 目录单测共用的条目与分组夹具：默认是系统级共享目录里一条生效、已启用的 Skill。 */
export function skillEntry(name: string, overrides: Partial<SkillEntry> = {}): SkillEntry {
  return {
    name,
    description: `${name} 的说明`,
    enabled: true,
    scope: "system",
    kind: "shared",
    overrides: false,
    warnings: [],
    manual_only: false,
    has_scripts: false,
    standing: "effective",
    ...overrides,
  };
}

export function skillGroup(skills: SkillEntry[], overrides: Partial<SkillGroup> = {}): SkillGroup {
  return { scope: "system", kind: "shared", location: "~/.agents/skills", skipped: [], skills, ...overrides };
}
