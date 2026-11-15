import { describe, expect, it } from "vitest";

import type { SkillImportConflict } from "@/lib/api/extensions";
import { HttpError } from "@/lib/http/http-error";
import { readSkillImportConflict, skillImportConflictDialog } from "@/lib/skills/skill-import-conflict";

const shared = { scope: "system", kind: "shared", location: "~/.agents/skills/review", description: "共享版本", same_content: false } as const;
const own = { scope: "system", kind: "cosir", location: "~/.cosir/skills/review", description: "旧版本", same_content: false } as const;

function conflict(relation: SkillImportConflict["relation"], existing: SkillImportConflict["existing"]): SkillImportConflict {
  return { name: "review", relation, description: "导入的版本", existing };
}

describe("skill import conflict", () => {
  it("reads only the import conflict out of an HTTP error", () => {
    const payload = conflict("override", [shared]);

    expect(readSkillImportConflict(new HttpError("冲突", { status: 409, code: "skill_import_conflict", detail: { conflict: payload } }))).toEqual(payload);
    expect(readSkillImportConflict(new HttpError("已存在", { status: 409, code: "configuration_conflict" }))).toBeNull();
    expect(readSkillImportConflict(new Error("boom"))).toBeNull();
  });

  it("only informs when the effective copy is already the same", () => {
    const dialog = skillImportConflictDialog(conflict("identical", [{ ...shared, same_content: true }]));

    expect(dialog.resolution).toBeNull();
    expect(dialog.confirmLabel).toBe("知道了");
    expect(dialog.description).toContain("~/.agents/skills/review");
  });

  it("offers replace for the cosir copy and override for others, showing both versions", () => {
    const replace = skillImportConflictDialog(conflict("replace", [own, shared]));
    const override = skillImportConflictDialog(conflict("override", [shared]));

    expect([replace.resolution, replace.confirmLabel]).toEqual(["replace", "替换"]);
    expect(replace.description).toContain("~/.cosir/skills/review");
    expect(replace.description).toContain("现有：旧版本\n导入：导入的版本");
    expect([override.resolution, override.confirmLabel]).toEqual(["override", "仍然导入"]);
    expect(override.description).toContain("Codex 等照旧用它");
  });
});
