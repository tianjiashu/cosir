import { describe, expect, it } from "vitest";

import type { SkillGroup } from "@/lib/api/extensions";
import { diffSkillCatalogs } from "@/lib/skills/skill-catalog-diff";
import { skillEntry as entry, skillGroup as group } from "./skill-catalog-fixtures";

describe("skill catalog diff", () => {
  it("counts rescanned skills per scope, so a same-name workspace skill is its own entry", () => {
    const view = (groups: SkillGroup[]) => ({ agent_skills: 0, agent_tokens: 0, groups });
    const before = view([group([entry("review"), entry("gone")])]);
    const after = view([
      group([entry("review")], { scope: "workspace", location: ".agents/skills" }),
      group([entry("review"), entry("fresh")]),
    ]);

    expect(diffSkillCatalogs(before, after)).toEqual({ added: 2, removed: 1 });
    expect(diffSkillCatalogs(after, after)).toEqual({ added: 0, removed: 0 });
  });

  it("counts same-name skills in the two roots of one scope separately", () => {
    const view = (groups: SkillGroup[]) => ({ agent_skills: 0, agent_tokens: 0, groups });
    const before = view([group([entry("review")])]);
    const after = view([group([entry("review")], { kind: "cosir", location: "~/.cosir/skills" }), group([entry("review")])]);

    expect(diffSkillCatalogs(before, after)).toEqual({ added: 1, removed: 0 });
  });
});
