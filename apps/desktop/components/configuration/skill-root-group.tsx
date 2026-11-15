import { SkillRow } from "@/components/configuration/skill-row";
import { Card } from "@/components/ui/card";
import type { ExtensionScopeId, SkillEntry, SkillGroup } from "@/lib/api/extensions";
import { SKILL_ROOT_COPY } from "@/lib/skills/skill-directory";

/**
 * 一个 Skill 根目录（作用域 × 身份）：组头写明身份、目录在哪、有几个，第二行说明谁能看到；组内是其中的 Skill。
 *
 * 身份只写在组头，行上不重复。本层的空目录照样列出，按身份告诉用户往哪放；搜索时只显示命中的条目。
 * 继承来的系统级分组只读。
 */
export function SkillRootGroup({ group, skills, workspaceId, editable, searching, busy, effectiveLabels, onToggleSkill }: {
  group: SkillGroup;
  /** 本组要显示的条目（搜索后）。 */
  skills: SkillEntry[];
  workspaceId: ExtensionScopeId;
  /** 本组属于页面自己这一层（继承来的系统级分组为 `false`）。 */
  editable: boolean;
  searching: boolean;
  busy: boolean;
  /** 每个名称生效的那条所在目录的称呼，给被覆盖的行用。 */
  effectiveLabels: Map<string, string>;
  onToggleSkill: (entry: SkillEntry) => void;
}) {
  const copy = SKILL_ROOT_COPY[group.kind];

  return (
    <Card className="overflow-hidden p-0">
      <div className="space-y-0.5 px-3 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 text-sm font-medium">{copy.name}</span>
          <code className="text-muted-foreground truncate text-xs">{group.location}</code>
          <span className="text-muted-foreground shrink-0 text-xs">
            {group.skills.length} 个
            {group.skipped.length > 0 && <span className="text-warning"> · {group.skipped.length} 个无法加载</span>}
          </span>
        </div>
        <p className="text-muted-foreground text-xs">
          {copy.audience}{group.kind === "cosir" && editable && "；「导入」的 Skill 放在这里"}
        </p>
      </div>
      <div className="border-t">
        {skills.length > 0 ? (
          <ul className="divide-y">
            {skills.map((entry) => (
              <SkillRow
                key={entry.name}
                entry={entry}
                workspaceId={workspaceId}
                editable={editable}
                busy={busy}
                shadowedBy={entry.standing === "shadowed" ? effectiveLabels.get(entry.name) : undefined}
                onToggle={() => onToggleSkill(entry)}
              />
            ))}
          </ul>
        ) : !searching && (
          <p className="text-muted-foreground px-4 py-3 text-xs">
            {group.kind === "cosir"
              ? "还没有。用右上角「导入」放进来，只有 cosir 使用。"
              : "还没有。用 npx skills 或 Codex 等工具装到这里，装好后点「重新扫描」。"}
          </p>
        )}
        {group.skipped.length > 0 && (
          <details className="border-t px-4 py-2 text-xs">
            <summary className="text-warning cursor-pointer select-none">{group.skipped.length} 个目录无法加载</summary>
            <ul className="text-muted-foreground mt-1 space-y-0.5">
              {group.skipped.map((item) => (
                <li key={item.directory}><code>{item.directory}</code>：{item.reason}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </Card>
  );
}
