import { describe, expect, it } from "vitest";

import { bulkToggleTargets, searchSkillGroups } from "@/lib/skills/skill-catalog-search";
import { skillEntry as entry, skillGroup as group } from "./skill-catalog-fixtures";

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
