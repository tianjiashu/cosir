import { describe, expect, it } from "vitest";

import type { SkillEntry, SkillGroup } from "@/lib/api/extensions";
import { bulkToggleTargets, searchSkillGroups } from "@/lib/skills/skill-catalog-search";

function entry(name: string, overrides: Partial<SkillEntry> = {}): SkillEntry {
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

function group(skills: SkillEntry[], overrides: Partial<SkillGroup> = {}): SkillGroup {
  return { scope: "system", kind: "shared", location: "~/.agents/skills", skipped: [], skills, ...overrides };
}

describe("skill catalog search", () => {
  it("returns every group untouched, empty ones included, when the query is blank", () => {
    const groups = [group([entry("a")], { scope: "workspace", location: ".agents/skills" }), group([])];

    expect(searchSkillGroups(groups, "  ").map(({ skills }) => skills.length)).toEqual([1, 0]);
  });

  it("searches name and description case-insensitively and hides groups without matches", () => {
    const groups = [
      group([entry("lark-mail"), entry("review", { description: "Review LARK changes" })]),
      group([entry("deploy")], { scope: "workspace", location: ".agents/skills" }),
    ];

    expect(searchSkillGroups(groups, "Lark").map(({ group: item, skills }) => [item.scope, skills.map((skill) => skill.name)])).toEqual([
      ["system", ["lark-mail", "review"]],
    ]);
  });

  it("offers bulk toggles only for effective entries of the page's own scope", () => {
    const searched = searchSkillGroups([
      group([entry("own-on"), entry("own-off", { enabled: false })], { scope: "workspace" }),
      group([entry("shadowed", { standing: "shadowed" })]),
      group([entry("inherited")]),
    ].map((item) => ({ ...item, skills: item.skills.map((skill) => ({ ...skill, scope: item.scope })) })), "o");

    expect(bulkToggleTargets(searched, "workspace")).toEqual({ enabled: ["own-on"], disabled: ["own-off"] });
  });
});
