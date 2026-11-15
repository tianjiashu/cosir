import { describe, expect, it } from "vitest";

import { effectiveRootLabels, skillRootLabel } from "@/lib/skills/skill-directory";
import { skillEntry, skillGroup } from "./skill-catalog-fixtures";

describe("skill directory labels", () => {
  it("names a root by identity, adding the scope only on a workspace page", () => {
    expect(skillRootLabel("system", "cosir", "system")).toBe("cosir 专属");
    expect(skillRootLabel("workspace", "shared", "workspace")).toBe("本工作区 · 共享");
    expect(skillRootLabel("system", "cosir", "workspace")).toBe("系统级 · cosir 专属");
  });

  it("points each name at the root holding its effective entry", () => {
    const labels = effectiveRootLabels([
      skillGroup([skillEntry("review", { standing: "effective" })], { scope: "workspace", kind: "cosir" }),
      skillGroup([skillEntry("review", { standing: "shadowed" }), skillEntry("lint", { standing: "effective" })], { scope: "system", kind: "shared" }),
    ], "workspace");

    expect([...labels]).toEqual([["review", "本工作区 · cosir 专属"], ["lint", "系统级 · 共享"]]);
  });
});
